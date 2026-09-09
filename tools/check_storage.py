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

            bare = await client.get(f"{settings.r2_endpoint}/{settings.r2_bucket_audio}/{key}")
            if bare.status_code not in {401, 403}:
                print(
                    f"FAILED  the bucket answered an unsigned read with HTTP {bare.status_code}. "
                    "Meeting audio must not be publicly readable."
                )
                return 1
            print("ok      refused an unsigned read, so the bucket is private")

        await storage.delete(key=key)
        print("ok      cleaned up")
    except StorageError as exc:
        print(f"FAILED  {exc}")
        return 1
    except httpx.HTTPError as exc:
        print(f"FAILED  could not reach the store: {type(exc).__name__}")
        return 1

    print()
    print("object storage is usable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
