import os
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config

from tricycle_reaction_db.application.services.artifact_content import (
    ArtifactContentService,
    iter_artifact_download,
)
from tricycle_reaction_db.application.services.transition_state_uploads import ArtifactUploadService
from tricycle_reaction_db.db.models import ArtifactFile
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import ArtifactKind
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID, SYSTEM_PROJECT_ID
from tricycle_reaction_db.storage.rustfs import RustFSSettings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.rustfs,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1"
        or os.getenv("TRICYCLE_RUN_RUSTFS_TESTS") != "1",
        reason="set database and RustFS integration flags to run artifact versioning tests",
    ),
]


@pytest.mark.asyncio
async def test_versioned_upload_persists_and_downloads_the_exact_s3_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_settings = RustFSSettings()
    bucket = f"tricycle-artifact-version-{uuid4().hex}"
    client = boto3.client(
        "s3",
        endpoint_url=default_settings.endpoint_url,
        aws_access_key_id=default_settings.access_key,
        aws_secret_access_key=default_settings.secret_key,
        region_name=default_settings.region,
        verify=default_settings.ca_bundle or default_settings.verify_tls,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    payload = b"database-persisted versioned artifact\n"
    artifact_id = None

    client.create_bucket(Bucket=bucket)
    client.put_bucket_versioning(
        Bucket=bucket,
        VersioningConfiguration={"Status": "Enabled"},
    )
    monkeypatch.setenv("TRICYCLE_RUSTFS_BUCKET", bucket)
    try:
        result = await ArtifactUploadService.upload(
            payload=payload,
            filename="versioned-artifact.txt",
            media_type="text/plain",
            artifact_kind=ArtifactKind.AUXILIARY,
            project_id=SYSTEM_PROJECT_ID,
            user_id=DEVELOPMENT_USER_ID,
        )
        artifact_id = result.artifact_id
        async with session_factory() as session:
            artifact = await session.get(ArtifactFile, artifact_id)
            assert artifact is not None
            assert artifact.bucket == bucket
            assert artifact.version_id is not None
            version_id = artifact.version_id

        download = await ArtifactContentService.download(
            artifact_id,
            user_id=DEVELOPMENT_USER_ID,
        )
        assert download.bucket == bucket
        assert download.version_id == version_id
        assert b"".join(iter_artifact_download(download)) == payload
    finally:
        if artifact_id is not None:
            async with session_factory() as session:
                artifact = await session.get(ArtifactFile, artifact_id)
                if artifact is not None:
                    await session.delete(artifact)
                    await session.commit()
        response = client.list_object_versions(Bucket=bucket)
        versions = response.get("Versions", []) + response.get("DeleteMarkers", [])
        if versions:
            client.delete_objects(
                Bucket=bucket,
                Delete={
                    "Objects": [
                        {"Key": item["Key"], "VersionId": item["VersionId"]} for item in versions
                    ],
                    "Quiet": True,
                },
            )
        client.delete_bucket(Bucket=bucket)
        client.close()
