import os
from hashlib import sha256
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config

from tricycle_reaction_db.storage.rustfs import RustFSObjectStore, RustFSSettings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.rustfs,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_RUSTFS_TESTS") != "1",
        reason="set TRICYCLE_RUN_RUSTFS_TESTS=1 to run RustFS tests",
    ),
]


def test_rustfs_put_head_get_delete_round_trip() -> None:
    settings = RustFSSettings()
    payload = (
        b"Entering Gaussian System\nSCF Done: E(RB3LYP) = -123.456789\n"
        b"\x00binary-safe\nNormal termination\n"
    ) * 128
    assert len(payload) >= 4096
    digest = sha256(payload).hexdigest()
    key = f"integration/{uuid4()}.log"

    with RustFSObjectStore(settings) as store:
        store.ensure_bucket()
        try:
            stored = store.put_bytes(
                key=key,
                payload=payload,
                content_type="text/plain",
                metadata={"source": "integration-test"},
            )

            assert stored.key == key
            assert stored.size == len(payload)
            assert stored.sha256 == digest
            assert stored.content_type == "text/plain"
            assert stored.etag
            assert store.exists(key)
            assert store.head(key) == stored
            assert store.get_bytes(key) == payload
            assert store.get_range(key, max_bytes=12) == payload[:12]
            assert b"".join(store.iter_bytes(key, chunk_size=7)) == payload
        finally:
            store.delete(key)

        assert not store.exists(key)


def test_rustfs_versioned_reads_use_the_persisted_exact_version() -> None:
    default_settings = RustFSSettings()
    bucket = f"tricycle-version-contract-{uuid4().hex}"
    settings = default_settings.model_copy(update={"bucket": bucket})
    client = boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        region_name=settings.region,
        verify=settings.ca_bundle or settings.verify_tls,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    key = "version-contract/shared-key.log"
    first_payload = b"first immutable artifact version\n"
    second_payload = b"second immutable artifact version\n"

    client.create_bucket(Bucket=bucket)
    try:
        client.put_bucket_versioning(
            Bucket=bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )
        with RustFSObjectStore(settings) as store:
            assert store.bucket_versioning_status() == "Enabled"
            first = store.put_bytes(key=key, payload=first_payload)
            second = store.put_bytes(key=key, payload=second_payload)

            assert first.version_id is not None
            assert second.version_id is not None
            assert first.version_id != second.version_id
            assert store.get_bytes(key, version_id=first.version_id) == first_payload
            assert store.get_bytes(key, version_id=second.version_id) == second_payload
            store.delete(key, version_id=first.version_id)
            store.delete(key, version_id=second.version_id)
    finally:
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
