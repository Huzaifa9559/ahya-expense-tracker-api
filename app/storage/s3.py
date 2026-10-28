"""The only S3 module — all object storage I/O goes through here."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, BinaryIO

import aioboto3
from botocore.config import Config

from app.config import get_settings


def _session() -> aioboto3.Session:
    return aioboto3.Session()


@asynccontextmanager
async def _client():
    settings = get_settings()
    session = _session()
    async with session.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region,
        config=Config(s3={"addressing_style": "path"}),
    ) as client:
        yield client, settings.s3_bucket


async def put_fileobj(key: str, fileobj: BinaryIO) -> None:
    """Upload a seekable file-like object to the configured bucket."""
    async with _client() as (client, bucket):
        await client.upload_fileobj(fileobj, bucket, key)


async def delete_object(key: str) -> None:
    async with _client() as (client, bucket):
        await client.delete_object(Bucket=bucket, Key=key)


async def head_object(key: str) -> dict[str, Any]:
    async with _client() as (client, bucket):
        return await client.head_object(Bucket=bucket, Key=key)


async def get_object_range(key: str, *, start: int, end: int) -> bytes:
    async with _client() as (client, bucket):
        resp = await client.get_object(
            Bucket=bucket, Key=key, Range=f"bytes={start}-{end}"
        )
        body = resp["Body"]
        return await body.read()


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
