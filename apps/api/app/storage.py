"""Object storage, behind a provider (ADR-01).

Cloudflare R2 in production, MinIO in development. Both speak the S3 API, so
one implementation covers both and the difference is four values in the
environment — which is the whole reason the abstraction exists.

Two rules shape everything here.

**The audio never passes through the API.** The desktop client uploads straight
to the bucket with presigned URLs and downloads the same way. A recording is
hundreds of megabytes; routing it through the API would turn a small server
into a file proxy and put meeting audio on its disk, which section 15.2 forbids
outright.

**A presigned URL is a bearer credential.** Anyone holding one can read or write
that object until it expires, without any token of ours. Hence a short life
(section 21.1), a private bucket, and a rule that these URLs are returned to a
caller who has already been authorised and are never written to a log.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import Settings
from app.logging import get_logger

logger = get_logger(__name__)

# S3 refuses any part below 5 MiB except the last one of an upload.
MIN_PART_SIZE_BYTES = 5 * 1024 * 1024
# S3's own ceiling. Reaching it means a recording of roughly 140 GB, which is
# not a meeting, so it is treated as a client error rather than accommodated.
MAX_PARTS = 10_000


class StorageError(Exception):
    """The object store refused or could not be reached."""


@dataclass(frozen=True)
class PartUpload:
    """One presigned slot the client uploads a chunk of audio into."""

    part_number: int
    url: str


@dataclass(frozen=True)
class MultipartUpload:
    """An upload the client is expected to finish, part by part."""

    key: str
    upload_id: str
    parts: list[PartUpload]
    part_size_bytes: int


@dataclass(frozen=True)
class StoredObject:
    """What the store says it holds, once the upload is complete."""

    key: str
    size_bytes: int
    etag: str


def audio_key(*, organization_id: uuid.UUID, meeting_id: uuid.UUID) -> str:
    """Where a meeting's audio lives.

    The organization is in the path, not only in the database. During an
    incident the owner of an object has to be readable from its name, and a
    bucket policy can later be scoped by prefix without a migration.
    """
    return f"org/{organization_id}/meetings/{meeting_id}/audio.ogg"


def part_count_for(size_bytes: int, *, part_size_bytes: int) -> int:
    """How many parts an upload of this size needs."""
    if size_bytes <= 0:
        message = "an upload must have a positive size"
        raise StorageError(message)
    count = -(-size_bytes // part_size_bytes)  # ceiling division
    if count > MAX_PARTS:
        message = f"an upload of {size_bytes} bytes would need more than {MAX_PARTS} parts"
        raise StorageError(message)
    return count


class StorageProvider(Protocol):
    """Stores and serves meeting audio."""

    async def start_multipart(
        self, *, key: str, size_bytes: int, content_type: str = "audio/ogg"
    ) -> MultipartUpload:
        """Open an upload and presign a slot for every part."""
        ...

    async def complete_multipart(
        self, *, key: str, upload_id: str, etags: Sequence[tuple[int, str]]
    ) -> StoredObject:
        """Close the upload; the store assembles the parts into one object."""
        ...

    async def abort_multipart(self, *, key: str, upload_id: str) -> None:
        """Give up on an upload and let the store discard its parts."""
        ...

    async def presign_get(self, *, key: str) -> str:
        """A temporary URL to read one object."""
        ...

    async def head(self, *, key: str) -> StoredObject | None:
        """What the store holds under this key, or None."""
        ...

    async def delete(self, *, key: str) -> None:
        """Remove one object. Idempotent."""
        ...


class S3StorageProvider:
    """Cloudflare R2, and MinIO in development."""

    def __init__(self, settings: Settings) -> None:
        access_key, secret_key = settings.require_storage_credentials()
        self._bucket = settings.r2_bucket_audio
        self._ttl = settings.r2_presign_ttl_seconds
        self._part_size = max(settings.r2_multipart_part_size_bytes, MIN_PART_SIZE_BYTES)
        self._session = aioboto3.Session()
        self._client_kwargs: dict[str, Any] = {
            "endpoint_url": settings.r2_endpoint,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "region_name": settings.r2_region,
            "config": Config(
                # R2 only implements SigV4, and MinIO accepts it.
                signature_version="s3v4",
                # The client retries a failed part on its own; a second layer
                # here would multiply the wait before the caller hears anything.
                retries={"max_attempts": 2, "mode": "standard"},
                connect_timeout=5,
                read_timeout=30,
            ),
        }

    def _client(self) -> Any:
        """A fresh client for one operation.

        Held open across the process it would be faster, but botocore's async
        clients are bound to the event loop that created them, and a shared one
        outliving its loop fails in ways that are painful to trace. One per
        operation is the boring choice.
        """
        return self._session.client("s3", **self._client_kwargs)

    async def start_multipart(
        self, *, key: str, size_bytes: int, content_type: str = "audio/ogg"
    ) -> MultipartUpload:
        parts_needed = part_count_for(size_bytes, part_size_bytes=self._part_size)
        try:
            async with self._client() as client:
                created = await client.create_multipart_upload(
                    Bucket=self._bucket, Key=key, ContentType=content_type
                )
                upload_id = str(created["UploadId"])
                parts = [
                    PartUpload(
                        part_number=number,
                        url=await client.generate_presigned_url(
                            "upload_part",
                            Params={
                                "Bucket": self._bucket,
                                "Key": key,
                                "UploadId": upload_id,
                                "PartNumber": number,
                            },
                            ExpiresIn=self._ttl,
                        ),
                    )
                    for number in range(1, parts_needed + 1)
                ]
        except ClientError as exc:
            raise StorageError(_describe(exc)) from exc

        # The URLs themselves are credentials and stay out of the log.
        logger.info("multipart_upload_started", key=key, parts=parts_needed)
        return MultipartUpload(
            key=key, upload_id=upload_id, parts=parts, part_size_bytes=self._part_size
        )

    async def complete_multipart(
        self, *, key: str, upload_id: str, etags: Sequence[tuple[int, str]]
    ) -> StoredObject:
        ordered = sorted(etags, key=lambda pair: pair[0])
        try:
            async with self._client() as client:
                await client.complete_multipart_upload(
                    Bucket=self._bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={
                        "Parts": [{"PartNumber": number, "ETag": etag} for number, etag in ordered]
                    },
                )
                described = await client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            raise StorageError(_describe(exc)) from exc

        stored = StoredObject(
            key=key,
            size_bytes=int(described["ContentLength"]),
            etag=str(described["ETag"]).strip('"'),
        )
        logger.info("multipart_upload_completed", key=key, size_bytes=stored.size_bytes)
        return stored

    async def abort_multipart(self, *, key: str, upload_id: str) -> None:
        try:
            async with self._client() as client:
                await client.abort_multipart_upload(
                    Bucket=self._bucket, Key=key, UploadId=upload_id
                )
        except ClientError as exc:
            raise StorageError(_describe(exc)) from exc
        logger.info("multipart_upload_aborted", key=key)

    async def presign_get(self, *, key: str) -> str:
        try:
            async with self._client() as client:
                url: str = await client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self._bucket, "Key": key},
                    ExpiresIn=self._ttl,
                )
        except ClientError as exc:
            raise StorageError(_describe(exc)) from exc
        return url

    async def head(self, *, key: str) -> StoredObject | None:
        try:
            async with self._client() as client:
                described = await client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _is_missing(exc):
                return None
            raise StorageError(_describe(exc)) from exc
        return StoredObject(
            key=key,
            size_bytes=int(described["ContentLength"]),
            etag=str(described["ETag"]).strip('"'),
        )

    async def delete(self, *, key: str) -> None:
        try:
            async with self._client() as client:
                await client.delete_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            # S3 reports a delete of a missing key as success; anything else is
            # a real failure and the purge needs to know it did not happen.
            raise StorageError(_describe(exc)) from exc
        logger.info("object_deleted", key=key)


def _is_missing(error: ClientError) -> bool:
    """Whether the store answered "no such object"."""
    code = str(error.response.get("Error", {}).get("Code", ""))
    return code in {"404", "NoSuchKey", "NotFound"}


def _describe(error: ClientError) -> str:
    """A message safe to raise.

    The provider's own text is dropped: it quotes the key and sometimes the
    request, and a key names an organization and a meeting.
    """
    code = str(error.response.get("Error", {}).get("Code", "unknown"))
    return f"object storage refused the request ({code})"


@dataclass
class InMemoryStorageProvider:
    """A double for tests, and for a developer with no bucket.

    It behaves like the real thing where behaviour matters — parts must be
    completed before the object exists, a missing key heads to None — and not
    at all where it does not, since the URLs it hands out point nowhere.
    """

    objects: dict[str, int] = field(default_factory=dict)
    uploads: dict[str, tuple[list[int], int]] = field(default_factory=dict)
    part_size_bytes: int = MIN_PART_SIZE_BYTES
    fail_next: bool = False
    # Simulates a truncated or padded upload: what the store ends up holding,
    # instead of what the client said it would send.
    stored_size_override: int | None = None

    async def start_multipart(
        self, *, key: str, size_bytes: int, content_type: str = "audio/ogg"
    ) -> MultipartUpload:
        if self.fail_next:
            self.fail_next = False
            message = "simulated storage failure"
            raise StorageError(message)
        count = part_count_for(size_bytes, part_size_bytes=self.part_size_bytes)
        upload_id = uuid.uuid4().hex
        # The declared size is remembered so completion reports what a real
        # store would report: the bytes it actually received.
        self.uploads[upload_id] = (list(range(1, count + 1)), size_bytes)
        return MultipartUpload(
            key=key,
            upload_id=upload_id,
            parts=[
                PartUpload(part_number=number, url=f"memory://{key}?part={number}")
                for number in range(1, count + 1)
            ],
            part_size_bytes=self.part_size_bytes,
        )

    async def complete_multipart(
        self, *, key: str, upload_id: str, etags: Sequence[tuple[int, str]]
    ) -> StoredObject:
        pending = self.uploads.pop(upload_id, None)
        if pending is None:
            message = "no such upload"
            raise StorageError(message)
        expected, declared = pending
        if sorted(number for number, _ in etags) != expected:
            message = "the completed parts do not match the upload"
            raise StorageError(message)
        size = self.stored_size_override if self.stored_size_override is not None else declared
        self.objects[key] = size
        return StoredObject(key=key, size_bytes=size, etag=uuid.uuid4().hex)

    async def abort_multipart(self, *, key: str, upload_id: str) -> None:
        self.uploads.pop(upload_id, None)

    async def presign_get(self, *, key: str) -> str:
        return f"memory://{key}"

    async def head(self, *, key: str) -> StoredObject | None:
        size = self.objects.get(key)
        if size is None:
            return None
        return StoredObject(key=key, size_bytes=size, etag=uuid.uuid4().hex)

    async def delete(self, *, key: str) -> None:
        self.objects.pop(key, None)
