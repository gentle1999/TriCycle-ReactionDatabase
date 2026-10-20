"""Queue a frozen catalogue migration for the existing upload worker.

This command only submits durable reparse batches. The normal upload worker
owns parsing, leases, recovery and completion; the profile worker consumes the
normal derived-data refresh queue. Re-running with the same state directory
resumes submission without reparsing completed migration batches.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from uuid import UUID

from sqlalchemy import exists
from sqlmodel import col, select

from tricycle_reaction_db.application.services.upload_batches import UploadBatchService
from tricycle_reaction_db.db.models import ArtifactFile, ArtifactIngestion, ParseRevision
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    ParseStatus,
    StorageStatus,
)

MAPPING_VERSION = "molop-calculation-mapping-v3"


async def _manifest(directory: Path) -> list[UUID]:
    path = directory / "queue-manifest.json"
    if path.exists():
        record = json.loads(path.read_text())
        if (
            record["molop_version"] != version("molop")
            or record["mapping_version"] != MAPPING_VERSION
        ):
            raise ValueError("migration manifest belongs to a different parser or mapping version")
        return [UUID(value) for value in record["artifact_ids"]]
    current_revision = exists().where(
        col(ParseRevision.artifact_file_id) == col(ArtifactFile.id),
        col(ParseRevision.molop_version) == version("molop"),
        col(ParseRevision.parser_provenance)["tricycle_ingestion_mapping_version"].astext
        == MAPPING_VERSION,
        col(ParseRevision.status) == ParseStatus.SUCCEEDED,
    )
    successful_ingestion = exists().where(
        col(ArtifactIngestion.artifact_file_id) == col(ArtifactFile.id),
        col(ArtifactIngestion.status) == ArtifactIngestionStatus.SUCCEEDED,
    )
    async with session_factory() as session:
        identifiers = list(
            (
                await session.exec(
                    select(ArtifactFile.id)
                    .where(
                        col(ArtifactFile.artifact_kind) == ArtifactKind.CALCULATION_OUTPUT,
                        col(ArtifactFile.storage_status) == StorageStatus.AVAILABLE,
                        ~(current_revision & successful_ingestion),
                    )
                    .order_by(col(ArtifactFile.project_id), col(ArtifactFile.id))
                )
            ).all()
        )
    values = [value for value in identifiers if isinstance(value, UUID)]
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "molop_version": version("molop"),
                "mapping_version": MAPPING_VERSION,
                "artifact_ids": list(map(str, values)),
            }
        )
        + "\n"
    )
    temporary.replace(path)
    return values


async def _run(args: argparse.Namespace) -> int:
    directory: Path = args.state_directory
    directory.mkdir(parents=True, exist_ok=True)
    identifiers = await _manifest(directory)
    # Fixed pages keep the database idempotency keys stable across resumes.
    digest = sha256("\n".join(map(str, identifiers)).encode()).hexdigest()
    journal = directory / "queue-submissions.jsonl"
    submitted = set()
    if journal.exists():
        for line in journal.read_text().splitlines():
            record = json.loads(line)
            if record["manifest_digest"] != digest or record["user_id"] != str(args.user_id):
                raise ValueError("submission checkpoint does not match manifest/user")
            submitted.add(record["offset"])
    pages = [
        (offset, identifiers[offset : offset + 256]) for offset in range(0, len(identifiers), 256)
    ]
    for offset, page in pages:
        if offset in submitted:
            continue
        batches = await UploadBatchService.enqueue_reparse_batch(
            page, user_id=args.user_id, request_key=f"catalogue:{digest}"
        )
        record = {
            "manifest_digest": digest,
            "user_id": str(args.user_id),
            "offset": offset,
            "count": len(page),
            "batch_ids": [str(batch.id) for batch in batches],
            "queued_at_epoch": time.time(),
        }
        with journal.open("a") as output:
            output.write(json.dumps(record) + "\n")
            output.flush()
        submitted.add(offset)
        progress = {
            "phase": "queueing",
            "executor": "upload-worker",
            "manifest_total": len(identifiers),
            "submitted": sum(len(page) for offset, page in pages if offset in submitted),
            "updated_at_epoch": time.time(),
        }
        temporary = directory / "queue-progress.tmp"
        temporary.write_text(json.dumps(progress, indent=2) + "\n")
        temporary.replace(directory / "queue-progress.json")
        print(json.dumps(progress), flush=True)
    progress = {
        "phase": "submitted",
        "executor": "upload-worker",
        "manifest_total": len(identifiers),
        "submitted": len(identifiers),
        "updated_at_epoch": time.time(),
    }
    (directory / "queue-progress.json").write_text(json.dumps(progress, indent=2) + "\n")
    print(json.dumps(progress), flush=True)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=UUID, required=True)
    parser.add_argument("--state-directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        code = asyncio.run(_run(args))
    except Exception as error:
        path = args.state_directory / "queue-progress.json"
        progress = json.loads(path.read_text()) if path.exists() else {}
        progress.update(
            phase="submission_failed",
            error_code=type(error).__name__,
            updated_at_epoch=time.time(),
        )
        args.state_directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(progress, indent=2) + "\n")
        temporary.replace(path)
        raise
    raise SystemExit(code)


if __name__ == "__main__":
    main()
