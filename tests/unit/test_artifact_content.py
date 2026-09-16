from pathlib import Path
from uuid import UUID
from zipfile import ZipFile

from tricycle_reaction_db.application.services import artifact_content
from tricycle_reaction_db.application.services.artifact_content import ArtifactDownload


def _download(artifact_id: str, filename: str) -> ArtifactDownload:
    return ArtifactDownload(
        id=UUID(artifact_id),
        original_filename=filename,
        media_type="text/plain",
        size_bytes=1,
        content_sha256="a" * 64,
        bucket="artifacts",
        object_key=f"raw/{artifact_id}",
        version_id=None,
    )


def test_write_artifact_archive_sanitizes_paths_and_duplicate_names(
    tmp_path: Path,
    monkeypatch,
) -> None:
    downloads = [
        _download("00000000-0000-7000-8000-000000000001", "../result.log"),
        _download("00000000-0000-7000-8000-000000000002", "result.log"),
    ]
    payloads = {
        download.id: f"payload-{index}".encode() for index, download in enumerate(downloads)
    }
    monkeypatch.setattr(
        artifact_content,
        "iter_artifact_download",
        lambda download: iter((payloads[download.id],)),
    )
    destination = tmp_path / "artifacts.zip"

    artifact_content.write_artifact_archive(downloads, destination)

    with ZipFile(destination) as archive:
        assert archive.namelist() == ["result.log", "result (2).log"]
        assert archive.read("result.log") == b"payload-0"
        assert archive.read("result (2).log") == b"payload-1"
