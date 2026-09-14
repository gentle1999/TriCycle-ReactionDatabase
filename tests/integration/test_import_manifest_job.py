import os
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlmodel import col, select

from tricycle_reaction_db.application.dtos import ProjectCreate
from tricycle_reaction_db.application.services import (
    AuthenticatedPrincipal,
    ImportJobConflictError,
    ImportJobService,
    ProjectManagementService,
)
from tricycle_reaction_db.core.config import Settings
from tricycle_reaction_db.db.models import (
    AuditEvent,
    Project,
    ProjectMembership,
    UploadBatch,
    UploadBatchItem,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import ArtifactKind
from tricycle_reaction_db.domain.identity import (
    DEVELOPMENT_IDENTITY_ISSUER,
    DEVELOPMENT_IDENTITY_SUBJECT,
    DEVELOPMENT_USER_ID,
    SYSTEM_ORGANIZATION_ID,
)
from tricycle_reaction_db.ingestion.manifest import ArtifactManifest, ArtifactManifestEntry

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run import manifest database tests",
    ),
]


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=DEVELOPMENT_USER_ID,
        display_name="Development User",
        primary_email="developer@localhost",
        is_service_account=False,
        issuer=DEVELOPMENT_IDENTITY_ISSUER,
        subject=DEVELOPMENT_IDENTITY_SUBJECT,
    )


def _manifest_entry(
    root: Path,
    relative_path: str,
    payload: bytes,
    *,
    selection_status: str = "selected",
) -> ArtifactManifestEntry:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return ArtifactManifestEntry(
        archive_sha256="a" * 64,
        relative_path=relative_path,
        staged_file_path=str(path),
        file_sha256=sha256(payload).hexdigest(),
        size_bytes=len(payload),
        media_type="text/plain",
        is_gaussian_log=relative_path.endswith(".log"),
        selection_status=selection_status,
    )


@pytest.mark.asyncio
async def test_manifest_job_is_idempotent_and_project_scoped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner = _principal()
    project_id = None
    settings = Settings(_env_file=None, import_staging_root=tmp_path)
    monkeypatch.setattr(
        "tricycle_reaction_db.application.services.import_jobs.get_settings",
        lambda: settings,
    )
    manifest = ArtifactManifest(
        archive_sha256="a" * 64,
        entries=[
            _manifest_entry(tmp_path, "run-a/result.log", b"first"),
            _manifest_entry(tmp_path, "run-b/result.log", b"second"),
            ArtifactManifestEntry(
                archive_sha256="a" * 64,
                relative_path="run-a/notes.txt",
                staged_file_path=str(tmp_path / "run-a/notes.txt"),
                file_sha256=sha256(b"notes").hexdigest(),
                size_bytes=5,
                media_type="text/plain",
                is_gaussian_log=False,
                selection_status="filtered",
            ),
        ],
    )
    (tmp_path / "run-a" / "notes.txt").write_bytes(b"notes")
    (tmp_path / "run-a" / "not-in-manifest.log").write_text("ignored", encoding="utf-8")

    try:
        project = await ProjectManagementService.create_project(
            ProjectCreate(
                organization_id=SYSTEM_ORGANIZATION_ID,
                slug=f"manifest-{uuid4().hex}",
                name="Manifest Import Integration",
            ),
            owner,
        )
        project_id = project.id

        first = await ImportJobService.register_manifest(
            manifest,
            project_id=project_id,
            user_id=owner.user_id,
            artifact_kind=ArtifactKind.AUXILIARY,
        )
        replay = await ImportJobService.register_manifest(
            manifest,
            project_id=project_id,
            user_id=owner.user_id,
            artifact_kind=ArtifactKind.AUXILIARY,
        )

        assert replay.id == first.id
        assert first.total_count == 3
        assert first.cancelled_count == 1

        async with session_factory() as session:
            rows = (
                await session.exec(
                    select(UploadBatchItem)
                    .where(col(UploadBatchItem.batch_id) == first.id)
                    .order_by(col(UploadBatchItem.position))
                )
            ).all()
        assert [row.relative_path for row in rows] == [
            "run-a/result.log",
            "run-b/result.log",
            "run-a/notes.txt",
        ]
        assert len({row.client_file_id for row in rows}) == 3
        assert rows[2].selection_status == "filtered"
        assert rows[2].artifact_file_id is None

        relocated_root = tmp_path / "relocated"
        relocated_manifest = ArtifactManifest(
            archive_sha256="a" * 64,
            entries=[
                _manifest_entry(relocated_root, "run-a/result.log", b"first"),
                _manifest_entry(relocated_root, "run-b/result.log", b"second"),
                _manifest_entry(
                    relocated_root,
                    "run-a/notes.txt",
                    b"notes",
                    selection_status="filtered",
                ),
            ],
        )
        relocated = await ImportJobService.register_manifest(
            relocated_manifest,
            project_id=project_id,
            user_id=owner.user_id,
            artifact_kind=ArtifactKind.AUXILIARY,
        )
        assert relocated.id == first.id

        async with session_factory() as session:
            relocated_rows = (
                await session.exec(
                    select(UploadBatchItem)
                    .where(col(UploadBatchItem.batch_id) == first.id)
                    .order_by(col(UploadBatchItem.position))
                )
            ).all()
        assert all(
            row.staged_file_path is not None
            and Path(row.staged_file_path).is_relative_to(relocated_root)
            for row in relocated_rows[:2]
        )

        (tmp_path / "run-a" / "result.log").write_bytes(b"changed")
        with pytest.raises(ImportJobConflictError, match="identity mismatch"):
            await ImportJobService.register_manifest(
                manifest,
                project_id=project_id,
                user_id=owner.user_id,
                artifact_kind=ArtifactKind.AUXILIARY,
            )
    finally:
        async with session_factory() as session:
            if project_id is not None:
                await session.exec(
                    delete(UploadBatch).where(col(UploadBatch.project_id) == project_id)
                )
                await session.exec(
                    delete(AuditEvent).where(col(AuditEvent.project_id) == project_id)
                )
                await session.exec(
                    delete(ProjectMembership).where(col(ProjectMembership.project_id) == project_id)
                )
                await session.exec(delete(Project).where(col(Project.id) == project_id))
            await session.commit()
