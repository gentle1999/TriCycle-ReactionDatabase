from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tricycle_reaction_db.core.observability import STORAGE_FAILURES

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mypy_boto3_s3 import S3Client


_BUCKET_INITIALIZATION_LOCK = Lock()


class RustFSSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TRICYCLE_RUSTFS_",
        extra="ignore",
    )

    # Read the shared deployment mode even though storage-specific settings use
    # the TRICYCLE_RUSTFS_ prefix. This keeps scheduler-only commands fail-closed.
    environment: Literal["development", "test", "production"] = Field(
        default="development",
        validation_alias="TRICYCLE_ENVIRONMENT",
    )
    endpoint_url: str = "http://127.0.0.1:19000"
    access_key: str = "example-local-access"
    secret_key: str = "example-local-secret"
    bucket: str = "example-reaction-raw-files"
    region: str = "us-east-1"
    verify_tls: bool = True
    ca_bundle: str | None = None
    connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    read_timeout_seconds: int = Field(default=30, ge=1, le=300)

    @field_validator("endpoint_url")
    @classmethod
    def validate_endpoint_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("endpoint_url must be an absolute HTTP(S) URL")
        return value.rstrip("/")

    @field_validator("ca_bundle")
    @classmethod
    def require_absolute_ca_bundle(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or not Path(normalized).is_absolute():
            raise ValueError("ca_bundle must be an absolute PEM path")
        return normalized

    @model_validator(mode="after")
    def validate_production_transport(self) -> RustFSSettings:
        if self.environment == "production":
            if urlsplit(self.endpoint_url).scheme != "https":
                raise ValueError("production RustFS requires an HTTPS endpoint")
            if not self.verify_tls:
                raise ValueError("production RustFS requires TLS verification")
        return self


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    bucket: str
    key: str
    version_id: str | None
    size: int
    etag: str
    last_modified: datetime
    content_type: str | None
    sha256: str | None


@dataclass(frozen=True, slots=True)
class ListedObject:
    bucket: str
    key: str
    size: int
    etag: str
    last_modified: datetime


class ObjectIntegrityError(RuntimeError):
    """Raised when downloaded bytes do not match their persisted SHA-256."""


def content_addressed_key(payload: bytes, *, prefix: str = "raw/sha256") -> str:
    return content_addressed_key_for_sha256(sha256(payload).hexdigest(), prefix=prefix)


def content_addressed_key_for_sha256(content_sha256: str, *, prefix: str = "raw/sha256") -> str:
    """Build a fan-out object key from a precomputed content digest."""

    digest = content_sha256.lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("content_sha256 must be a lowercase or uppercase SHA-256 digest")
    clean_prefix = prefix.strip("/")
    if not clean_prefix:
        raise ValueError("prefix must not be empty")
    return f"{clean_prefix}/{digest[:2]}/{digest}"


def time_partitioned_content_addressed_key(
    payload: bytes,
    *,
    uploaded_at: datetime,
    prefix: str = "uploads",
) -> str:
    """Build a content-addressed key under an hourly UTC listing partition."""

    return time_partitioned_content_addressed_key_for_sha256(
        sha256(payload).hexdigest(),
        uploaded_at=uploaded_at,
        prefix=prefix,
    )


def time_partitioned_content_addressed_key_for_sha256(
    content_sha256: str,
    *,
    uploaded_at: datetime,
    prefix: str = "uploads",
) -> str:
    """Build an hourly content-addressed key from a precomputed digest."""

    if uploaded_at.tzinfo is None or uploaded_at.utcoffset() is None:
        raise ValueError("uploaded_at must be timezone-aware")
    clean_prefix = prefix.strip("/")
    if not clean_prefix:
        raise ValueError("prefix must not be empty")
    partition = uploaded_at.astimezone(UTC).strftime("%Y/%m/%d/%H")
    return content_addressed_key_for_sha256(
        content_sha256,
        prefix=f"{clean_prefix}/{partition}/sha256",
    )


class RustFSObjectStore:
    def __init__(
        self,
        settings: RustFSSettings,
        *,
        client: S3Client | None = None,
    ) -> None:
        self.settings = settings
        endpoint_host = urlsplit(settings.endpoint_url).hostname
        local_endpoint = endpoint_host in {
            "localhost",
            "127.0.0.1",
            "::1",
            "rustfs",
            "host.docker.internal",
        }
        self._client = client or boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            aws_access_key_id=settings.access_key,
            aws_secret_access_key=settings.secret_key,
            region_name=settings.region,
            verify=settings.ca_bundle or settings.verify_tls,
            config=Config(
                signature_version="s3v4",
                connect_timeout=settings.connect_timeout_seconds,
                read_timeout=settings.read_timeout_seconds,
                retries={"mode": "standard", "max_attempts": 3},
                s3={"addressing_style": "path"},
                **({"proxies": {}} if local_endpoint else {}),
            ),
        )

    def __enter__(self) -> RustFSObjectStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def ensure_bucket(self) -> None:
        # A batch upload opens one store per storage worker. Serialize the
        # first head/create sequence so a fresh RustFS volume does not receive
        # concurrent MakeBucket requests during initialization.
        with _BUCKET_INITIALIZATION_LOCK:
            try:
                self._client.head_bucket(Bucket=self.settings.bucket)
            except ClientError as error:
                if not self._is_not_found(error):
                    raise
                try:
                    self._client.create_bucket(Bucket=self.settings.bucket)
                except ClientError as create_error:
                    if not self._is_already_exists(create_error):
                        raise

    def bucket_versioning_status(self) -> str | None:
        response = self._client.get_bucket_versioning(Bucket=self.settings.bucket)
        status = response.get("Status")
        return str(status) if status is not None else None

    def put_bytes(
        self,
        *,
        key: str,
        payload: bytes,
        content_type: str = "application/octet-stream",
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectMetadata:
        self._validate_key(key)
        digest = sha256(payload).hexdigest()
        persisted_metadata = dict(metadata or {})
        persisted_metadata["sha256"] = digest
        response = self._client.put_object(
            Bucket=self.settings.bucket,
            Key=key,
            Body=payload,
            ContentType=content_type,
            Metadata=persisted_metadata,
        )
        return self.head(key, version_id=response.get("VersionId"))

    def put_file(
        self,
        *,
        key: str,
        path: Path,
        content_sha256: str,
        size_bytes: int,
        content_type: str = "application/octet-stream",
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectMetadata:
        """Stream one already-inspected local file into object storage."""

        self._validate_key(key)
        if size_bytes < 0:
            raise ValueError("size_bytes must not be negative")
        persisted_metadata = dict(metadata or {})
        persisted_metadata["sha256"] = content_sha256
        with path.open("rb") as stream:
            actual_size = stream.seek(0, 2)
            stream.seek(0)
            if actual_size != size_bytes:
                raise ObjectIntegrityError(
                    "local file size changed before upload: "
                    f"expected {size_bytes}, got {actual_size}"
                )
            response = self._client.put_object(
                Bucket=self.settings.bucket,
                Key=key,
                Body=stream,
                ContentLength=size_bytes,
                ContentType=content_type,
                Metadata=persisted_metadata,
            )
        return self.head(key, version_id=response.get("VersionId"))

    def head(self, key: str, *, version_id: str | None = None) -> ObjectMetadata:
        self._validate_key(key)
        try:
            response = (
                self._client.head_object(
                    Bucket=self.settings.bucket,
                    Key=key,
                    VersionId=version_id,
                )
                if version_id is not None
                else self._client.head_object(Bucket=self.settings.bucket, Key=key)
            )
        except ClientError as error:
            if self._is_not_found(error):
                STORAGE_FAILURES.labels(reason="missing").inc()
            raise
        return ObjectMetadata(
            bucket=self.settings.bucket,
            key=key,
            version_id=response.get("VersionId") or version_id,
            size=response["ContentLength"],
            etag=response["ETag"].strip('"'),
            last_modified=response["LastModified"],
            content_type=response.get("ContentType"),
            sha256=response.get("Metadata", {}).get("sha256"),
        )

    def get_bytes(self, key: str, *, version_id: str | None = None) -> bytes:
        self._validate_key(key)
        response = (
            self._client.get_object(
                Bucket=self.settings.bucket,
                Key=key,
                VersionId=version_id,
            )
            if version_id is not None
            else self._client.get_object(Bucket=self.settings.bucket, Key=key)
        )
        body = response["Body"]
        try:
            payload = body.read()
        finally:
            body.close()

        expected_digest = response.get("Metadata", {}).get("sha256")
        actual_digest = sha256(payload).hexdigest()
        if expected_digest is not None and actual_digest != expected_digest:
            STORAGE_FAILURES.labels(reason="corrupt").inc()
            raise ObjectIntegrityError(
                f"SHA-256 mismatch for s3://{self.settings.bucket}/{key}: "
                f"expected {expected_digest}, got {actual_digest}"
            )
        return payload

    def get_range(
        self,
        key: str,
        *,
        max_bytes: int,
        version_id: str | None = None,
    ) -> bytes:
        """Read at most the first ``max_bytes`` of an object."""

        self._validate_key(key)
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        byte_range = f"bytes=0-{max_bytes - 1}"
        response = (
            self._client.get_object(
                Bucket=self.settings.bucket,
                Key=key,
                Range=byte_range,
                VersionId=version_id,
            )
            if version_id is not None
            else self._client.get_object(
                Bucket=self.settings.bucket,
                Key=key,
                Range=byte_range,
            )
        )
        body = response["Body"]
        try:
            return body.read(max_bytes)
        finally:
            body.close()

    def iter_bytes(
        self,
        key: str,
        *,
        chunk_size: int = 64 * 1024,
        version_id: str | None = None,
    ) -> Iterator[bytes]:
        """Stream an object in bounded chunks."""

        self._validate_key(key)
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        response = (
            self._client.get_object(
                Bucket=self.settings.bucket,
                Key=key,
                VersionId=version_id,
            )
            if version_id is not None
            else self._client.get_object(Bucket=self.settings.bucket, Key=key)
        )
        body = response["Body"]
        try:
            for chunk in body.iter_chunks(chunk_size=chunk_size):
                if chunk:
                    yield chunk
        finally:
            body.close()

    def exists(self, key: str, *, version_id: str | None = None) -> bool:
        try:
            self.head(key, version_id=version_id)
        except ClientError as error:
            if self._is_not_found(error):
                return False
            raise
        return True

    def delete(self, key: str, *, version_id: str | None = None) -> None:
        self._validate_key(key)
        if version_id is None:
            self._client.delete_object(Bucket=self.settings.bucket, Key=key)
        else:
            self._client.delete_object(
                Bucket=self.settings.bucket,
                Key=key,
                VersionId=version_id,
            )

    def iter_objects(self, *, prefix: str) -> Iterator[ListedObject]:
        """Yield every object under ``prefix`` using ListObjectsV2 pagination."""

        clean_prefix = prefix.strip("/")
        if not clean_prefix:
            raise ValueError("prefix must not be empty")
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self.settings.bucket,
            Prefix=f"{clean_prefix}/",
        ):
            for item in page.get("Contents", []):
                key = item.get("Key")
                size = item.get("Size")
                etag = item.get("ETag")
                last_modified = item.get("LastModified")
                if key is None or size is None or etag is None or last_modified is None:
                    raise RuntimeError("ListObjectsV2 returned incomplete object metadata")
                yield ListedObject(
                    bucket=self.settings.bucket,
                    key=key,
                    size=size,
                    etag=etag.strip('"'),
                    last_modified=last_modified,
                )

    @staticmethod
    def _validate_key(key: str) -> None:
        if not key or key.startswith("/"):
            raise ValueError("object key must be non-empty and relative")

    @staticmethod
    def _is_not_found(error: ClientError) -> bool:
        response = error.response
        error_code = str(response.get("Error", {}).get("Code", ""))
        status_code = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return status_code == 404 or error_code in {"404", "NoSuchBucket", "NoSuchKey", "NotFound"}

    @staticmethod
    def _is_already_exists(error: ClientError) -> bool:
        response = error.response
        error_code = str(response.get("Error", {}).get("Code", ""))
        status_code = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return status_code == 409 or error_code in {
            "409",
            "BucketAlreadyExists",
            "BucketAlreadyOwnedByYou",
        }
