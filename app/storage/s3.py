"""The only S3 module — all object storage I/O goes through here."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, BinaryIO

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import get_settings


def _session() -> aioboto3.Session:
    return aioboto3.Session()


def _client_kwargs(*, public: bool = False) -> dict[str, Any]:
    settings = get_settings()
    endpoint = (
        settings.s3_public_endpoint if public else settings.s3_endpoint_url
    )
    return {
        "service_name": "s3",
        "endpoint_url": endpoint,
        "aws_access_key_id": settings.s3_access_key_id,
        "aws_secret_access_key": settings.s3_secret_access_key,
        "region_name": settings.s3_region,
        # signature_version=s3v4 so presigned URLs carry X-Amz-Expires (not legacy Expires=).
        "config": Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    }


@asynccontextmanager
async def _client(*, public: bool = False):
    settings = get_settings()
    session = _session()
    async with session.client(**_client_kwargs(public=public)) as client:
        yield client, settings.s3_bucket


async def put_fileobj(key: str, fileobj: BinaryIO) -> None:
    """Upload a seekable file-like object to the configured bucket."""
    async with _client() as (client, bucket):
        await client.upload_fileobj(fileobj, bucket, key)


async def delete_object(key: str) -> None:
    # S3 delete of a missing key succeeds — deletes are idempotent.
    async with _client() as (client, bucket):
        await client.delete_object(Bucket=bucket, Key=key)


async def head_object(key: str) -> dict[str, Any]:
    async with _client() as (client, bucket):
        return await client.head_object(Bucket=bucket, Key=key)


async def head(key: str) -> int | None:
    """Return object size in bytes, or None if the key is missing."""
    try:
        meta = await head_object(key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    return int(meta["ContentLength"])


async def get_object_range(key: str, *, start: int, end: int) -> bytes:
    async with _client() as (client, bucket):
        resp = await client.get_object(
            Bucket=bucket, Key=key, Range=f"bytes={start}-{end}"
        )
        body = resp["Body"]
        return await body.read()


async def read_prefix(key: str, n: int = 8) -> bytes:
    """Read the first n bytes (Range bytes=0-{n-1}) for magic-byte detection."""
    if n < 1:
        return b""
    return await get_object_range(key, start=0, end=n - 1)


async def presign_post(
    key: str,
    content_type: str,
    *,
    max_bytes: int = 5_242_880,
    expires: int = 600,
) -> dict[str, Any]:
    """
    Presigned POST for browser/host upload via S3_PUBLIC_ENDPOINT.

    Pins the exact server-chosen key (not starts-with) plus Content-Type and
    content-length-range. Stricter than a prefix policy; confirm's key check is
    defense in depth.
    """
    async with _client(public=True) as (client, bucket):
        return await client.generate_presigned_post(
            Bucket=bucket,
            Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[
                ["content-length-range", 1, max_bytes],
                {"Content-Type": content_type},
                {"key": key},
            ],
            ExpiresIn=expires,
        )


async def presign_get(key: str, *, expires: int = 900) -> str:
    """Presigned GET via S3_PUBLIC_ENDPOINT (15 minutes by default)."""
    async with _client(public=True) as (client, bucket):
        return await client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires,
        )


async def stream_lines(key: str, *, chunk_size: int = 64 * 1024) -> AsyncIterator[str]:
    """
    Async iterator of decoded text lines from an S3 object.

    Handles UTF-8 BOM, CRLF (via rstrip of CR), and lines split across chunks.
    Documented contract: one CSV record per line — no multi-line quoted fields.
    """
    async with _client() as (client, bucket):
        resp = await client.get_object(Bucket=bucket, Key=key)
        body = resp["Body"]
        pending = ""
        first_chunk = True
        while True:
            chunk: bytes = await body.read(chunk_size)
            if not chunk:
                break
            text = chunk.decode("utf-8")
            if first_chunk:
                if text.startswith("\ufeff"):
                    text = text[1:]
                first_chunk = False
            pending += text
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                yield line.rstrip("\r")
        if pending:
            yield pending.rstrip("\r")
