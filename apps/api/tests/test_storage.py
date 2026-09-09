"""Object storage (lot L2.2), against a real S3 implementation.

These run against MinIO rather than a double, and that is the point: the double
proves our own logic, but only a real server proves the signature is right, the
part numbering is accepted and a presigned URL actually opens. Getting SigV4
subtly wrong is the kind of mistake that passes every unit test and fails on
the first upload in production.

The same code path serves Cloudflare R2. What changes between the two is four
values in the environment.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

from app.config import Settings
from app.storage import (
    MIN_PART_SIZE_BYTES,
    InMemoryStorageProvider,
    S3StorageProvider,
    StorageError,
    audio_key,
    part_count_for,
)

pytestmark = pytest.mark.asyncio

ENDPOINT = os.environ.get("TEST_R2_ENDPOINT", "http://localhost:9000")
ACCESS_KEY = os.environ.get("TEST_R2_ACCESS_KEY_ID", "novabrief")
SECRET_KEY = os.environ.get("TEST_R2_SECRET_ACCESS_KEY", "novabrief-dev-secret")
BUCKET = os.environ.get("TEST_R2_BUCKET_AUDIO", "novabrief-audio")


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[S3StorageProvider]:
    """A provider pointed at MinIO, skipped if none is running."""
    settings = Settings(
        r2_endpoint=ENDPOINT,
        r2_access_key_id=ACCESS_KEY,
        r2_secret_access_key=SECRET_KEY,
        r2_bucket_audio=BUCKET,
    )
    provider = S3StorageProvider(settings)
    try:
        await provider.head(key="probe/does-not-exist")
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no object store reachable at {ENDPOINT} ({type(exc).__name__})")
    yield provider


def _key() -> str:
    return f"tests/{uuid.uuid4().hex}/audio.ogg"


# --------------------------------------------------------------------------
# Sizing, without a server
# --------------------------------------------------------------------------


async def test_a_small_upload_needs_one_part() -> None:
    assert part_count_for(1024, part_size_bytes=MIN_PART_SIZE_BYTES) == 1


async def test_the_last_partial_part_still_counts() -> None:
    """Ceiling division: 5 MiB + 1 byte is two parts, not one."""
    assert part_count_for(MIN_PART_SIZE_BYTES + 1, part_size_bytes=MIN_PART_SIZE_BYTES) == 2


async def test_an_empty_upload_is_refused() -> None:
    """A meeting with no audio is a bug upstream, not a zero-byte object."""
    with pytest.raises(StorageError):
        part_count_for(0, part_size_bytes=MIN_PART_SIZE_BYTES)


async def test_an_absurd_upload_is_refused() -> None:
    """S3 stops at 10 000 parts; past that it is not a meeting."""
    with pytest.raises(StorageError):
        part_count_for(10_001 * MIN_PART_SIZE_BYTES, part_size_bytes=MIN_PART_SIZE_BYTES)


async def test_the_key_carries_the_organization() -> None:
    """An object's owner has to be readable from its name during an incident."""
    organization_id = uuid.uuid4()
    meeting_id = uuid.uuid4()

    key = audio_key(organization_id=organization_id, meeting_id=meeting_id)

    assert str(organization_id) in key
    assert str(meeting_id) in key


# --------------------------------------------------------------------------
# Against MinIO
# --------------------------------------------------------------------------


async def test_a_presigned_part_url_actually_accepts_an_upload(
    storage: S3StorageProvider,
) -> None:
    """The test that a double cannot replace: is the signature valid?"""
    key = _key()
    body = b"a" * MIN_PART_SIZE_BYTES

    upload = await storage.start_multipart(key=key, size_bytes=len(body))
    assert len(upload.parts) == 1

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.put(upload.parts[0].url, content=body)
    assert response.status_code == 200, response.text

    etag = response.headers["ETag"].strip('"')
    stored = await storage.complete_multipart(
        key=key, upload_id=upload.upload_id, etags=[(1, etag)]
    )

    assert stored.size_bytes == len(body)
    await storage.delete(key=key)


async def test_an_upload_in_several_parts_is_reassembled(storage: S3StorageProvider) -> None:
    """Section 16.4: a real recording arrives in chunks, not in one request."""
    key = _key()
    first = b"1" * MIN_PART_SIZE_BYTES
    second = b"2" * 1024

    upload = await storage.start_multipart(key=key, size_bytes=len(first) + len(second))
    assert len(upload.parts) == 2

    etags: list[tuple[int, str]] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for part, chunk in zip(upload.parts, (first, second), strict=True):
            response = await client.put(part.url, content=chunk)
            assert response.status_code == 200, response.text
            etags.append((part.part_number, response.headers["ETag"].strip('"')))

    stored = await storage.complete_multipart(key=key, upload_id=upload.upload_id, etags=etags)

    assert stored.size_bytes == len(first) + len(second)
    await storage.delete(key=key)


async def test_the_object_does_not_exist_until_the_upload_completes(
    storage: S3StorageProvider,
) -> None:
    """Half an upload must not read as a finished recording."""
    key = _key()
    upload = await storage.start_multipart(key=key, size_bytes=MIN_PART_SIZE_BYTES)

    assert await storage.head(key=key) is None

    await storage.abort_multipart(key=key, upload_id=upload.upload_id)
    assert await storage.head(key=key) is None


async def test_a_presigned_read_url_returns_the_audio(storage: S3StorageProvider) -> None:
    """How the web player and the desktop client fetch a recording."""
    key = _key()
    body = b"z" * MIN_PART_SIZE_BYTES
    upload = await storage.start_multipart(key=key, size_bytes=len(body))
    async with httpx.AsyncClient(timeout=30.0) as client:
        put = await client.put(upload.parts[0].url, content=body)
        await storage.complete_multipart(
            key=key, upload_id=upload.upload_id, etags=[(1, put.headers["ETag"].strip('"'))]
        )

        url = await storage.presign_get(key=key)
        fetched = await client.get(url)

    assert fetched.status_code == 200
    assert fetched.content == body
    await storage.delete(key=key)


async def test_the_bucket_refuses_an_unsigned_read(storage: S3StorageProvider) -> None:
    """The bucket must be private: meeting audio is not public by accident."""
    key = _key()
    body = b"y" * MIN_PART_SIZE_BYTES
    upload = await storage.start_multipart(key=key, size_bytes=len(body))
    async with httpx.AsyncClient(timeout=30.0) as client:
        put = await client.put(upload.parts[0].url, content=body)
        await storage.complete_multipart(
            key=key, upload_id=upload.upload_id, etags=[(1, put.headers["ETag"].strip('"'))]
        )

        bare = await client.get(f"{ENDPOINT}/{BUCKET}/{key}")

    assert bare.status_code in {401, 403}, "the audio bucket is readable without a signature"
    await storage.delete(key=key)


async def test_heading_a_missing_object_is_not_an_error(storage: S3StorageProvider) -> None:
    assert await storage.head(key=f"tests/{uuid.uuid4().hex}/absent.ogg") is None


async def test_deleting_twice_is_harmless(storage: S3StorageProvider) -> None:
    """The purge may run again after a crash; it must not fail on the second pass."""
    key = _key()
    await storage.delete(key=key)
    await storage.delete(key=key)


# --------------------------------------------------------------------------
# The double behaves like the real thing where it matters
# --------------------------------------------------------------------------


async def test_the_double_hides_the_object_until_completion() -> None:
    provider = InMemoryStorageProvider()
    upload = await provider.start_multipart(key="k", size_bytes=MIN_PART_SIZE_BYTES)

    assert await provider.head(key="k") is None

    await provider.complete_multipart(key="k", upload_id=upload.upload_id, etags=[(1, "e")])
    assert await provider.head(key="k") is not None


async def test_the_double_refuses_a_mismatched_completion() -> None:
    """A client that skips a part must not end up with a truncated recording."""
    provider = InMemoryStorageProvider()
    upload = await provider.start_multipart(key="k", size_bytes=2 * MIN_PART_SIZE_BYTES)

    with pytest.raises(StorageError):
        await provider.complete_multipart(key="k", upload_id=upload.upload_id, etags=[(1, "e")])
