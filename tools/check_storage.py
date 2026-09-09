"""Prove that object storage is configured and reachable.

Run it after filling the R2 values in `.env`, before trusting a deployment:

    python tools/check_storage.py

It reads the configuration the API reads, writes a small object, reads it back
through a presigned URL, checks the bucket refuses an unsigned read, and
deletes what it made. It never prints a credential — the point is that whoever
holds the keys can verify them without showing them to anyone.

Exit code 0 means the store is usable. Anything else means it is not, and the
message says which step failed.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "api"))
sys.path.insert(0, str(_ROOT / "packages"))

from app.config import Settings  # noqa: E402
from app.storage import (  # noqa: E402
    MIN_PART_SIZE_BYTES,
    S3StorageProvider,
    StorageError,
)


def _redacted(value: str | None) -> str:
    """Enough to recognise a key, not enough to use one."""
    if not value:
        return "(not set)"
    return f"{value[:4]}...{value[-2:]} ({len(value)} chars)"


async def main() -> int:
    settings = Settings()

    print("configuration")
    print(f"  endpoint : {settings.r2_endpoint}")
    print(f"  bucket   : {settings.r2_bucket_audio}")
    print(f"  key id   : {_redacted(settings.r2_access_key_id)}")
    print(f"  secret   : {_redacted(settings.r2_secret_access_key)}")
    print(f"  presign  : {settings.r2_presign_ttl_seconds}s")
    print()

    try:
        storage = S3StorageProvider(settings)
    except RuntimeError as exc:
        print(f"FAILED  {exc}")
        return 1

    key = f"healthcheck/{uuid.uuid4().hex}.bin"
    body = b"novabrief" * (MIN_PART_SIZE_BYTES // 9 + 1)
    body = body[:MIN_PART_SIZE_BYTES]

    try:
        return await _probe(storage, settings, key=key, body=body)
    finally:
        # In a finally block because every failure above used to return early
        # and leave a five-megabyte object behind in the customer's bucket.
        try:
            await storage.delete(key=key)
            print("ok      cleaned up")
        except StorageError as exc:  # pragma: no cover - operational tool
            print(f"WARNING could not delete {key}: {exc}")


async def _probe(storage: S3StorageProvider, settings: Settings, *, key: str, body: bytes) -> int:
    """Write, read back, check the endpoint refuses an unsigned read."""
    try:
        upload = await storage.start_multipart(key=key, size_bytes=len(body))
        print(f"ok      opened an upload in {len(upload.parts)} part(s)")

        async with httpx.AsyncClient(timeout=60.0) as client:
            put = await client.put(upload.parts[0].url, content=body)
            if put.status_code != 200:
                print(f"FAILED  the presigned upload was refused (HTTP {put.status_code})")
                await storage.abort_multipart(key=key, upload_id=upload.upload_id)
                return 1
            print("ok      wrote a part through a presigned URL")

            stored = await storage.complete_multipart(
                key=key,
                upload_id=upload.upload_id,
                etags=[(1, put.headers["ETag"].strip('"'))],
            )
            if stored.size_bytes != len(body):
                print(f"FAILED  stored {stored.size_bytes} bytes, sent {len(body)}")
                return 1
            print(f"ok      assembled the object ({stored.size_bytes} bytes)")

            fetched = await client.get(await storage.presign_get(key=key))
            if fetched.status_code != 200 or fetched.content != body:
                print("FAILED  the object did not read back identically")
                return 1
            print("ok      read it back through a presigned URL")

            # Any refusal counts. R2 answers 400 rather than 403 to a request
            # carrying no signature at all, and both mean the same thing: the
            # object was not served.
            bare = await client.get(f"{settings.r2_endpoint}/{settings.r2_bucket_audio}/{key}")
            if bare.status_code < 400 or bare.content == body:
                print(
                    f"FAILED  an unsigned read returned HTTP {bare.status_code} and content. "
                    "Meeting audio must not be readable without a signature."
                )
                return 1
            print(f"ok      refused an unsigned read (HTTP {bare.status_code})")
    except StorageError as exc:
        print(f"FAILED  {exc}")
        return 1
    except httpx.HTTPError as exc:
        print(f"FAILED  could not reach the store: {type(exc).__name__}")
        return 1

    print()
    print("object storage is usable.")
    print()
    print(
        "One thing this cannot check: R2 serves public objects through an r2.dev "
        "subdomain or a custom domain, not through the S3 endpoint tested above. "
        "Confirm in the bucket settings that public access is disabled."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
