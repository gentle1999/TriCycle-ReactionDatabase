"""Rebuild calculation-derived data from the current RustFS artifact objects.

RustFS is inventoried first and is the source of which objects currently
exist.  The preserved PostgreSQL ``ArtifactFile`` catalogue supplies the
project owner for each object; the same object may therefore be parsed once
per owning project while its bytes remain globally cached.  A JSONL checkpoint
is tied to the inventory digest so an old run cannot be mistaken for a fresh
rebuild.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from tricycle_reaction_db.application.services.artifact_uploads import (
    ArtifactUploadPayload,
    ArtifactUploadService,
    close_molop_process_pool,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import ArtifactFile, ArtifactIngestion
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    StorageStatus,
)
from tricycle_reaction_db.storage.rustfs import (
    ListedObject,
    RustFSObjectStore,
    RustFSSettings,
)


@dataclass(frozen=True, slots=True)
class ArtifactObject:
    id: UUID
    project_id: UUID
    bucket: str
    object_key: str
    version_id: str | None
    content_sha256: str
    size_bytes: int
    original_filename: str
    media_type: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reimport available calculation artifacts from RustFS without creating files"
    )
    parser.add_argument(
        "--project-id",
        action="append",
        type=UUID,
        dest="project_ids",
        help="restrict the rebuild to one project; repeat for multiple projects",
    )
    parser.add_argument(
        "--user-id",
        type=UUID,
        help="user used for project upload authorization (defaults to development user)",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(".tmp/artifact-object-reimport-fresh.jsonl"),
        help="append-only resumability checkpoint",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=4,
        help="number of RustFS objects staged and parsed per bounded batch",
    )
    parser.add_argument(
        "--fetch-concurrency",
        type=int,
        default=2,
        help="maximum concurrent RustFS downloads inside one batch",
    )
    parser.add_argument(
        "--list-concurrency",
        type=int,
        default=16,
        help="maximum concurrent RustFS inventory shard listings",
    )
    parser.add_argument(
        "--inventory-only",
        action="store_true",
        help="verify the current RustFS inventory and project mapping without parsing",
    )
    parser.add_argument(
        "--allow-unowned-object",
        action="store_true",
        help=(
            "allow current RustFS objects without an ArtifactFile owner to remain as "
            "unassigned cache objects; never imports them"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="process at most this many not-yet-checkpointed artifacts",
    )
    parser.add_argument(
        "--filename-contains",
        type=str,
        default=None,
        help="restrict the rebuild to ArtifactFile names containing this text",
    )
    return parser


def _load_completed(path: Path, *, inventory_digest: str) -> set[tuple[UUID, str]]:
    completed: set[tuple[UUID, str]] = set()
    if not path.exists():
        return completed
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if (
                record.get("status") not in {"succeeded", "filtered"}
                or record.get("inventory_digest") != inventory_digest
            ):
                continue
            try:
                completed.add((UUID(record["artifact_id"]), str(record["content_sha256"])))
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    f"invalid completed artifact checkpoint at line {line_number}"
                ) from error
    return completed


def _append_checkpoint(
    path: Path,
    *,
    artifact: ArtifactObject,
    status: str,
    inventory_digest: str,
    **details: object,
) -> None:
    record: dict[str, object] = {
        "artifact_id": str(artifact.id),
        "project_id": str(artifact.project_id),
        "content_sha256": artifact.content_sha256,
        "inventory_digest": inventory_digest,
        "filename": artifact.original_filename,
        "status": status,
        **details,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()


async def _load_artifacts(
    project_ids: list[UUID] | None,
    *,
    filename_contains: str | None = None,
) -> list[ArtifactObject]:
    statement = (
        select(
            ArtifactFile.id,
            ArtifactFile.project_id,
            ArtifactFile.bucket,
            ArtifactFile.object_key,
            ArtifactFile.version_id,
            ArtifactFile.content_sha256,
            ArtifactFile.size_bytes,
            ArtifactFile.original_filename,
            ArtifactFile.media_type,
        )
        .where(
            ArtifactFile.artifact_kind == ArtifactKind.CALCULATION_OUTPUT,
            ArtifactFile.storage_status == StorageStatus.AVAILABLE,
        )
        .order_by(ArtifactFile.project_id, ArtifactFile.id)
    )
    if project_ids:
        statement = statement.where(ArtifactFile.project_id.in_(project_ids))
    if filename_contains:
        statement = statement.where(ArtifactFile.original_filename.contains(filename_contains))
    async with session_factory() as session:
        rows = (await session.exec(statement)).all()
    artifacts: list[ArtifactObject] = []
    for row in rows:
        (
            artifact_id,
            project_id,
            bucket,
            object_key,
            version_id,
            digest,
            size,
            filename,
            media_type,
        ) = row
        if not isinstance(artifact_id, UUID) or not isinstance(project_id, UUID):
            raise RuntimeError("artifact catalogue contains a row without UUID identity")
        artifacts.append(
            ArtifactObject(
                id=artifact_id,
                project_id=project_id,
                bucket=str(bucket),
                object_key=str(object_key),
                version_id=str(version_id) if version_id is not None else None,
                content_sha256=str(digest),
                size_bytes=int(size),
                original_filename=str(filename),
                media_type=str(media_type),
            )
        )
    return artifacts


async def _load_known_object_keys() -> set[tuple[str, str]]:
    statement = select(ArtifactFile.bucket, ArtifactFile.content_sha256)
    async with session_factory() as session:
        rows = (await session.exec(statement)).all()
    return {(str(bucket), str(content_sha256).lower()) for bucket, content_sha256 in rows}


async def _load_partial_artifact_ids(artifact_ids: Sequence[UUID]) -> set[UUID]:
    """Exclude partial ingestions from a prior checkpoint so they are retried."""

    if not artifact_ids:
        return set()
    statement = select(ArtifactIngestion.artifact_file_id).where(
        ArtifactIngestion.artifact_file_id.in_(artifact_ids),
        ArtifactIngestion.status == ArtifactIngestionStatus.PARTIAL,
    )
    async with session_factory() as session:
        return {
            artifact_id
            for artifact_id in (await session.exec(statement)).all()
            if isinstance(artifact_id, UUID)
        }


def _content_sha256_from_object_key(object_key: str) -> str | None:
    digest = object_key.rsplit("/", 1)[-1].lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return None
    return digest


def _directory_prefixes(
    store: RustFSObjectStore,
    prefix: str,
) -> tuple[set[str], bool]:
    """Return child directory prefixes and whether objects exist directly below."""

    common_prefixes: set[str] = set()
    has_direct_objects = False
    continuation_token: str | None = None
    while True:
        request: dict[str, object] = {
            "Bucket": store.settings.bucket,
            "Delimiter": "/",
            "MaxKeys": 1000,
        }
        if prefix:
            request["Prefix"] = prefix
        if continuation_token is not None:
            request["ContinuationToken"] = continuation_token
        response = store._client.list_objects_v2(**request)
        common_prefixes.update(
            str(item["Prefix"])
            for item in response.get("CommonPrefixes", [])
            if item.get("Prefix")
        )
        has_direct_objects = has_direct_objects or bool(response.get("Contents"))
        if not response.get("IsTruncated"):
            break
        next_token = response.get("NextContinuationToken")
        if not next_token:
            raise RuntimeError(
                "RustFS directory listing is truncated without a continuation token: "
                f"{prefix}"
            )
        continuation_token = str(next_token)
    return common_prefixes, has_direct_objects


def _discover_leaf_prefixes(settings: RustFSSettings) -> tuple[str, ...]:
    """Discover every leaf prefix without issuing one large bucket listing."""

    with RustFSObjectStore(settings) as store:
        pending = [""]
        leaves: set[str] = set()
        while pending:
            prefix = pending.pop()
            children, has_direct_objects = _directory_prefixes(store, prefix)
            # The upload key layout ends at the two-digit SHA-256 fan-out
            # directory.  There is no need to issue another directory request
            # for each individual shard; each child is already a listable leaf.
            if prefix.rstrip("/").endswith("/sha256"):
                leaves.update(child.rstrip("/") for child in children)
                continue
            pending.extend(sorted(children, reverse=True))
            if has_direct_objects:
                clean_prefix = prefix.rstrip("/")
                if not clean_prefix:
                    raise RuntimeError("RustFS inventory contains an unsupported root-level object")
                leaves.add(clean_prefix)
    return tuple(sorted(leaves))


def _list_leaf_objects(
    settings: RustFSSettings,
    prefixes: Sequence[str],
) -> tuple[ListedObject, ...]:
    with RustFSObjectStore(settings) as store:
        return tuple(
            listed
            for prefix in prefixes
            for listed in store.iter_objects(prefix=prefix, page_size=1000)
        )


async def _build_storage_inventory(
    buckets: set[str],
    *,
    list_concurrency: int,
) -> dict[tuple[str, str], ListedObject]:
    if list_concurrency < 1:
        raise ValueError("list-concurrency must be positive")
    inventory: dict[tuple[str, str], ListedObject] = {}
    for bucket in sorted(buckets):
        settings = RustFSSettings().model_copy(update={"bucket": bucket})
        leaf_prefixes = await asyncio.to_thread(_discover_leaf_prefixes, settings)
        semaphore = asyncio.Semaphore(list_concurrency)

        async def list_one(
            prefixes: tuple[str, ...],
            *,
            bucket_settings: RustFSSettings = settings,
            bucket_semaphore: asyncio.Semaphore = semaphore,
        ) -> tuple[ListedObject, ...]:
            async with bucket_semaphore:
                return await asyncio.to_thread(_list_leaf_objects, bucket_settings, prefixes)

        batch_size = max(1, list_concurrency * 4)
        for offset in range(0, len(leaf_prefixes), batch_size):
            prefix_batch = leaf_prefixes[offset : offset + batch_size]
            prefix_groups = tuple(
                tuple(prefix_batch[index : index + 4])
                for index in range(0, len(prefix_batch), 4)
            )
            pages = await asyncio.gather(*(list_one(prefixes) for prefixes in prefix_groups))
            for objects in pages:
                for listed in objects:
                    key = (listed.bucket, listed.key)
                    if key in inventory:
                        raise RuntimeError(f"RustFS inventory returned a duplicate object: {key}")
                    inventory[key] = listed
            print(
                f"RustFS inventory {bucket}: {min(offset + batch_size, len(leaf_prefixes))}/"
                f"{len(leaf_prefixes)} prefixes, {len(inventory)} objects",
                file=sys.stderr,
                flush=True,
            )
    return inventory


def _inventory_by_content_hash(
    inventory: Mapping[tuple[str, str], ListedObject],
) -> dict[tuple[str, str], tuple[ListedObject, ...]]:
    grouped: dict[tuple[str, str], list[ListedObject]] = {}
    for listed in inventory.values():
        digest = _content_sha256_from_object_key(listed.key)
        if digest is not None:
            grouped.setdefault((listed.bucket, digest), []).append(listed)
    return {
        key: tuple(sorted(objects, key=lambda item: (item.last_modified, item.key), reverse=True))
        for key, objects in grouped.items()
    }


def _bind_artifact_objects_to_inventory(
    artifacts: Sequence[ArtifactObject],
    *,
    inventory: Mapping[tuple[str, str], ListedObject],
) -> tuple[list[ArtifactObject], dict[str, int]]:
    by_content_hash = _inventory_by_content_hash(inventory)
    missing: list[tuple[str, str]] = []
    size_mismatches: list[tuple[UUID, int, int]] = []
    bound: list[ArtifactObject] = []
    duplicate_object_count = 0
    for artifact in artifacts:
        candidates = by_content_hash.get((artifact.bucket, artifact.content_sha256.lower()), ())
        if not candidates:
            missing.append((artifact.bucket, artifact.content_sha256))
            continue
        exact = inventory.get((artifact.bucket, artifact.object_key))
        selected = exact if exact is not None else candidates[0]
        if len(candidates) > 1:
            duplicate_object_count += len(candidates) - 1
        if selected.size != artifact.size_bytes:
            size_mismatches.append((artifact.id, artifact.size_bytes, selected.size))
            continue
        if exact is None and artifact.version_id is not None:
            raise RuntimeError(
                "a versioned ArtifactFile points to a missing object key; refusing to guess "
                f"a different version for {artifact.id}"
            )
        bound.append(
            ArtifactObject(
                id=artifact.id,
                project_id=artifact.project_id,
                bucket=artifact.bucket,
                object_key=selected.key,
                version_id=artifact.version_id if exact is not None else None,
                content_sha256=artifact.content_sha256,
                size_bytes=artifact.size_bytes,
                original_filename=artifact.original_filename,
                media_type=artifact.media_type,
            )
        )
    if missing:
        raise RuntimeError(
            f"{len(missing)} available calculation identities are missing from RustFS; "
            f"sample={missing[:3]}"
        )
    if size_mismatches:
        raise RuntimeError(
            f"{len(size_mismatches)} ArtifactFile/RustFS size mismatches; "
            f"sample={size_mismatches[:3]}"
        )
    return bound, {"duplicate_object_references": duplicate_object_count}


def _inventory_digest(inventory: Mapping[tuple[str, str], ListedObject]) -> str:
    records = [
        {
            "bucket": bucket,
            "key": key,
            "size": inventory[(bucket, key)].size,
            "etag": inventory[(bucket, key)].etag,
        }
        for bucket, key in sorted(inventory)
    ]
    return sha256(json.dumps(records, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


async def _download(
    artifact: ArtifactObject,
    destination: Path,
    store: RustFSObjectStore,
) -> None:
    payload = await asyncio.to_thread(
        store.get_bytes,
        artifact.object_key,
        version_id=artifact.version_id,
    )
    if len(payload) != artifact.size_bytes:
        raise ValueError(
            f"RustFS size mismatch for {artifact.id}: expected {artifact.size_bytes}, "
            f"got {len(payload)}"
        )
    digest = sha256(payload).hexdigest()
    if digest != artifact.content_sha256:
        raise ValueError(
            f"RustFS SHA-256 mismatch for {artifact.id}: expected "
            f"{artifact.content_sha256}, got {digest}"
        )
    await asyncio.to_thread(destination.write_bytes, payload)


async def _run(args: argparse.Namespace) -> int:
    if args.batch_size < 1 or args.fetch_concurrency < 1:
        raise ValueError("batch-size and fetch-concurrency must be positive")
    if args.limit is not None and args.limit < 1:
        raise ValueError("limit must be positive")

    settings = get_settings()
    user_id = args.user_id or settings.development_user_id
    state_file: Path = args.state_file
    artifacts = await _load_artifacts(
        args.project_ids,
        filename_contains=args.filename_contains,
    )
    known_content_hashes = await _load_known_object_keys()
    buckets = {bucket for bucket, _content_sha256 in known_content_hashes}
    if not buckets:
        buckets = {RustFSSettings().bucket}
    inventory = await _build_storage_inventory(
        buckets,
        list_concurrency=args.list_concurrency,
    )
    inventory_digest = _inventory_digest(inventory)
    inventory_by_hash = _inventory_by_content_hash(inventory)
    inventory_hashes = set(inventory_by_hash)
    unmapped = [
        (listed.bucket, listed.key)
        for listed in inventory.values()
        if (
            (digest := _content_sha256_from_object_key(listed.key)) is None
            or (listed.bucket, digest) not in known_content_hashes
        )
    ]
    if unmapped and not args.allow_unowned_object:
        sample = sorted(unmapped)[:3]
        raise RuntimeError(
            f"{len(unmapped)} current RustFS objects have no ArtifactFile project owner; "
            f"sample={sample}; pass --allow-unowned-object only after auditing them"
        )
    if unmapped:
        print(
            f"ignoring {len(unmapped)} unowned RustFS cache object(s); "
            f"sample={sorted(unmapped)[:3]}",
            file=sys.stderr,
            flush=True,
        )
    artifacts, binding_summary = _bind_artifact_objects_to_inventory(
        artifacts,
        inventory=inventory,
    )
    calculation_hashes = {
        (artifact.bucket, artifact.content_sha256.lower()) for artifact in artifacts
    }
    project_counts: dict[str, int] = {}
    for artifact in artifacts:
        project_key = str(artifact.project_id)
        project_counts[project_key] = project_counts.get(project_key, 0) + 1
    inventory_summary = {
        "inventory_digest": inventory_digest,
        "storage_objects": len(inventory),
        "storage_content_hashes": len(inventory_hashes),
        "duplicate_storage_objects": len(inventory) - len(inventory_hashes),
        "catalogued_calculation_objects": len(artifacts),
        "catalogued_project_counts": project_counts,
        "unowned_storage_objects": len(unmapped),
        "unowned_storage_object_sample": sorted(unmapped)[:3],
        **binding_summary,
        "catalogued_non_calculation_objects_present": len(
            (inventory_hashes & known_content_hashes) - calculation_hashes
        ),
    }
    if args.inventory_only:
        print(json.dumps(inventory_summary, ensure_ascii=False, sort_keys=True))
        return 0

    completed = _load_completed(state_file, inventory_digest=inventory_digest)
    partial_artifact_ids = await _load_partial_artifact_ids([artifact.id for artifact in artifacts])
    completed = {
        (artifact_id, content_sha256)
        for artifact_id, content_sha256 in completed
        if artifact_id not in partial_artifact_ids
    }
    pending = [
        artifact
        for artifact in artifacts
        if (artifact.id, artifact.content_sha256) not in completed
    ]
    if args.limit is not None:
        pending = pending[: args.limit]
    pending_by_project: dict[UUID, list[ArtifactObject]] = {}
    for artifact in pending:
        pending_by_project.setdefault(artifact.project_id, []).append(artifact)

    storage_settings = RustFSSettings()
    stores: dict[str, RustFSObjectStore] = {}
    totals = {
        "catalogued": len(artifacts),
        "storage_objects": len(inventory),
        "inventory_digest": inventory_digest,
        "catalogued_project_counts": project_counts,
        "checkpointed_before_run": len(completed),
        "pending": len(pending),
        "succeeded": 0,
        "filtered": 0,
        "partial": 0,
        "failed": 0,
        "source_frames": 0,
        "ts_frames": 0,
        "inferred_reactions": 0,
    }

    async def fetch_batch(batch: list[ArtifactObject], directory: Path) -> list[Path]:
        semaphore = asyncio.Semaphore(args.fetch_concurrency)

        async def fetch_one(artifact: ArtifactObject) -> Path:
            async with semaphore:
                store = stores.get(artifact.bucket)
                if store is None:
                    store = RustFSObjectStore(
                        storage_settings.model_copy(update={"bucket": artifact.bucket})
                    )
                    stores[artifact.bucket] = store
                destination = directory / str(artifact.id)
                await _download(artifact, destination, store)
                return destination

        return list(await asyncio.gather(*(fetch_one(artifact) for artifact in batch)))

    try:
        processed = 0
        for project_id, project_pending in pending_by_project.items():
            for offset in range(0, len(project_pending), args.batch_size):
                batch = project_pending[offset : offset + args.batch_size]
                try:
                    with tempfile.TemporaryDirectory(
                        prefix="tricycle-object-reimport-"
                    ) as temporary_directory:
                        directory = Path(temporary_directory)
                        paths = await fetch_batch(batch, directory)
                        payloads = [
                            ArtifactUploadPayload(
                                filename=artifact.original_filename,
                                media_type=artifact.media_type,
                                payload=None,
                                spool_path=path,
                            )
                            for artifact, path in zip(batch, paths, strict=True)
                        ]
                        result = await ArtifactUploadService.upload_batch(
                            files=payloads,
                            artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                            project_id=project_id,
                            user_id=user_id,
                            streaming=True,
                            persistence_batch_files=args.batch_size,
                            enforce_batch_file_limit=False,
                            reparse_failed_ingestions=True,
                        )
                        for artifact, item in zip(batch, result.items, strict=True):
                            ingestion_status = item.result.ingestion_status if item.result else None
                            if ingestion_status is ArtifactIngestionStatus.SUCCEEDED:
                                totals["succeeded"] += 1
                                if item.result is not None:
                                    totals["source_frames"] += item.result.source_frame_count or 0
                                    totals["ts_frames"] += (
                                        item.result.transition_state_frame_count or 0
                                    )
                                    totals["inferred_reactions"] += (
                                        item.result.inferred_reaction_count
                                    )
                                _append_checkpoint(
                                    state_file,
                                    artifact=artifact,
                                    status="succeeded",
                                    inventory_digest=inventory_digest,
                                    ingestion_id=(
                                        str(item.result.ingestion_id) if item.result else None
                                    ),
                                    parse_revision_id=(
                                        str(item.result.parse_revision_id) if item.result else None
                                    ),
                                    inferred_reaction_count=(
                                        item.result.inferred_reaction_count if item.result else 0
                                    ),
                                )
                            elif ingestion_status is ArtifactIngestionStatus.FILTERED:
                                # A valid object without QM calculation frames is a
                                # terminal filtered result, not a retryable failure.
                                totals["filtered"] += 1
                                _append_checkpoint(
                                    state_file,
                                    artifact=artifact,
                                    status="filtered",
                                    inventory_digest=inventory_digest,
                                    ingestion_id=(
                                        str(item.result.ingestion_id)
                                        if item.result.ingestion_id
                                        else None
                                    ),
                                    parse_revision_id=(
                                        str(item.result.parse_revision_id)
                                        if item.result.parse_revision_id
                                        else None
                                    ),
                                    inferred_reaction_count=item.result.inferred_reaction_count,
                                    error_code=item.error_code,
                                    error_message=item.error_message,
                                )
                            elif ingestion_status is ArtifactIngestionStatus.PARTIAL:
                                # Partial parse results are retryable: they may
                                # be the residue of the historical persistence
                                # bug, and must never be checkpointed as a
                                # successful import.
                                totals["partial"] += 1
                                _append_checkpoint(
                                    state_file,
                                    artifact=artifact,
                                    status="partial",
                                    inventory_digest=inventory_digest,
                                    ingestion_id=(
                                        str(item.result.ingestion_id)
                                        if item.result and item.result.ingestion_id
                                        else None
                                    ),
                                    parse_revision_id=(
                                        str(item.result.parse_revision_id)
                                        if item.result and item.result.parse_revision_id
                                        else None
                                    ),
                                    inferred_reaction_count=(
                                        item.result.inferred_reaction_count
                                        if item.result
                                        else 0
                                    ),
                                    error_code=item.error_code,
                                    error_message=item.error_message,
                                )
                            else:
                                totals["failed"] += 1
                                _append_checkpoint(
                                    state_file,
                                    artifact=artifact,
                                    status="failed",
                                    inventory_digest=inventory_digest,
                                    error_code=item.error_code,
                                    error_message=item.error_message,
                                )
                        print(
                            json.dumps(
                                {
                                    "project_id": str(project_id),
                                    "batch_end": processed + len(batch),
                                    "pending_total": len(pending),
                                    "succeeded": result.succeeded_count,
                                    "failed": result.failed_count,
                                    "source_frames": result.source_frame_count,
                                    "ts_frames": result.transition_state_frame_count,
                                    "inferred_reactions": result.inferred_reaction_count,
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            file=sys.stderr,
                            flush=True,
                        )
                except Exception as error:
                    totals["failed"] += len(batch)
                    for artifact in batch:
                        _append_checkpoint(
                            state_file,
                            artifact=artifact,
                            status="failed",
                            inventory_digest=inventory_digest,
                            error_code=type(error).__name__,
                            error_message=str(error) or type(error).__name__,
                        )
                    print(
                        f"reimport batch failed at {processed + 1}-{processed + len(batch)}: "
                        f"{error}",
                        file=sys.stderr,
                        flush=True,
                    )
                processed += len(batch)
    finally:
        for store in stores.values():
            store.close()
        await close_molop_process_pool()

    print(json.dumps(totals, ensure_ascii=False, sort_keys=True))
    return 1 if totals["failed"] or totals["partial"] else 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(_run(_parser().parse_args())))
    except (ValueError, OSError) as error:
        print(f"artifact object reimport failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
