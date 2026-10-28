"""Receipts: presigned POST policy, confirm magic/extension, S3 deletion queue."""

from __future__ import annotations

import base64
import io
import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from botocore.exceptions import ClientError
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.storage import s3
from app.workers.s3_cleanup import process_s3_deletions
from tests.helpers import create_category, register_and_login

PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG_MAGIC = b"\xff\xd8\xff" + b"\x00" * 16
PDF_MAGIC = b"%PDF-1.4" + b"\n%%EOF\n"
EXE_AS_PNG = b"MZ" + b"\x00" * 32

MAX_BYTES = 5_242_880


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> dict:
    return {"session_factory": session_factory}


async def _make_expense(
    client: AsyncClient, headers: dict, rates, *, category_name: str | None = None
) -> dict:
    cat = await create_category(
        client, headers, name=category_name or f"Food-{uuid.uuid4().hex[:8]}"
    )
    resp = await client.post(
        "/api/v1/expenses",
        headers=headers,
        json={
            "category_id": cat["id"],
            "amount": "12.00",
            "currency": "USD",
            "description": "receipt-test",
            "spent_on": "2026-03-02",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _decode_policy(fields: dict) -> dict:
    return json.loads(base64.b64decode(fields["policy"]))


@pytest.mark.asyncio
async def test_head_treats_403_as_missing():
    """Real AWS without s3:ListBucket returns 403 for a missing key — treat as None."""
    err = ClientError(
        {
            "Error": {"Code": "403", "Message": "Forbidden"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        "HeadObject",
    )
    with patch.object(s3, "head_object", AsyncMock(side_effect=err)):
        assert await s3.head("receipts/u/e/missing.png") is None


@pytest.mark.asyncio
async def test_upload_url_policy_conditions(client: AsyncClient, rates):
    auth = await register_and_login(client, email="rcpt-policy@example.com")
    expense = await _make_expense(client, auth["headers"], rates)

    resp = await client.post(
        f"/api/v1/expenses/{expense['id']}/receipt/upload-url",
        headers=auth["headers"],
        json={"content_type": "image/png"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "url" in body and "fields" in body
    assert body["expires_in"] == 600
    assert body["max_bytes"] == MAX_BYTES
    assert body["key"].endswith(".png")
    assert body["key"].startswith(f"receipts/{auth['user']['id']}/{expense['id']}/")

    policy = _decode_policy(body["fields"])
    conditions = policy["conditions"]
    assert ["content-length-range", 1, MAX_BYTES] in conditions
    assert {"Content-Type": "image/png"} in conditions
    assert {"key": body["key"]} in conditions


@pytest.mark.asyncio
async def test_unsupported_content_type_and_other_users_expense(
    client: AsyncClient, rates
):
    owner = await register_and_login(client, email="rcpt-owner@example.com")
    expense = await _make_expense(client, owner["headers"], rates)

    bad = await client.post(
        f"/api/v1/expenses/{expense['id']}/receipt/upload-url",
        headers=owner["headers"],
        json={"content_type": "image/gif"},
    )
    assert bad.status_code == 422

    other = await register_and_login(client, email="rcpt-other@example.com")
    sneak = await client.post(
        f"/api/v1/expenses/{expense['id']}/receipt/upload-url",
        headers=other["headers"],
        json={"content_type": "image/png"},
    )
    assert sneak.status_code == 404


@pytest.mark.asyncio
async def test_confirm_png_bumps_version_and_etag(client: AsyncClient, rates):
    auth = await register_and_login(client, email="rcpt-png@example.com")
    expense = await _make_expense(client, auth["headers"], rates)
    assert expense["version"] == 1

    key = f"receipts/{auth['user']['id']}/{expense['id']}/{uuid.uuid4()}.png"
    await s3.put_fileobj(key, io.BytesIO(PNG_MAGIC))

    resp = await client.post(
        f"/api/v1/expenses/{expense['id']}/receipt/confirm",
        headers=auth["headers"],
        json={"key": key},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["receipt_key"] == key
    assert body["version"] == 2
    assert resp.headers.get("etag") == '"2"'


@pytest.mark.asyncio
async def test_exe_renamed_to_png_rejected_and_deleted(client: AsyncClient, rates):
    auth = await register_and_login(client, email="rcpt-exe@example.com")
    expense = await _make_expense(client, auth["headers"], rates)
    key = f"receipts/{auth['user']['id']}/{expense['id']}/{uuid.uuid4()}.png"
    await s3.put_fileobj(key, io.BytesIO(EXE_AS_PNG))

    resp = await client.post(
        f"/api/v1/expenses/{expense['id']}/receipt/confirm",
        headers=auth["headers"],
        json={"key": key},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_FILE_TYPE"
    assert await s3.head(key) is None


@pytest.mark.asyncio
async def test_jpeg_bytes_under_png_key_rejected(client: AsyncClient, rates):
    auth = await register_and_login(client, email="rcpt-mismatch@example.com")
    expense = await _make_expense(client, auth["headers"], rates)
    key = f"receipts/{auth['user']['id']}/{expense['id']}/{uuid.uuid4()}.png"
    await s3.put_fileobj(key, io.BytesIO(JPEG_MAGIC))

    resp = await client.post(
        f"/api/v1/expenses/{expense['id']}/receipt/confirm",
        headers=auth["headers"],
        json={"key": key},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_FILE_TYPE"
    assert await s3.head(key) is None


@pytest.mark.asyncio
async def test_pdf_accepted(client: AsyncClient, rates):
    auth = await register_and_login(client, email="rcpt-pdf@example.com")
    expense = await _make_expense(client, auth["headers"], rates)
    key = f"receipts/{auth['user']['id']}/{expense['id']}/{uuid.uuid4()}.pdf"
    await s3.put_fileobj(key, io.BytesIO(PDF_MAGIC))

    resp = await client.post(
        f"/api/v1/expenses/{expense['id']}/receipt/confirm",
        headers=auth["headers"],
        json={"key": key},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["receipt_key"] == key


@pytest.mark.asyncio
async def test_invalid_receipt_key_for_other_expense(client: AsyncClient, rates):
    auth = await register_and_login(client, email="rcpt-key@example.com")
    a = await _make_expense(client, auth["headers"], rates)
    b = await _make_expense(client, auth["headers"], rates)
    key = f"receipts/{auth['user']['id']}/{a['id']}/{uuid.uuid4()}.png"
    await s3.put_fileobj(key, io.BytesIO(PNG_MAGIC))

    resp = await client.post(
        f"/api/v1/expenses/{b['id']}/receipt/confirm",
        headers=auth["headers"],
        json={"key": key},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_RECEIPT_KEY"


@pytest.mark.asyncio
async def test_receipt_not_uploaded(client: AsyncClient, rates):
    auth = await register_and_login(client, email="rcpt-missing@example.com")
    expense = await _make_expense(client, auth["headers"], rates)
    key = f"receipts/{auth['user']['id']}/{expense['id']}/{uuid.uuid4()}.png"

    resp = await client.post(
        f"/api/v1/expenses/{expense['id']}/receipt/confirm",
        headers=auth["headers"],
        json={"key": key},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "RECEIPT_NOT_UPLOADED"


@pytest.mark.asyncio
async def test_receipt_too_large_deleted(client: AsyncClient, rates):
    auth = await register_and_login(client, email="rcpt-big@example.com")
    expense = await _make_expense(client, auth["headers"], rates)
    key = f"receipts/{auth['user']['id']}/{expense['id']}/{uuid.uuid4()}.png"
    await s3.put_fileobj(key, io.BytesIO(b"x" * (MAX_BYTES + 1)))

    resp = await client.post(
        f"/api/v1/expenses/{expense['id']}/receipt/confirm",
        headers=auth["headers"],
        json={"key": key},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "RECEIPT_TOO_LARGE"
    assert await s3.head(key) is None


@pytest.mark.asyncio
async def test_download_url_and_missing_receipt(client: AsyncClient, rates):
    auth = await register_and_login(client, email="rcpt-dl@example.com")
    expense = await _make_expense(client, auth["headers"], rates)

    missing = await client.get(
        f"/api/v1/expenses/{expense['id']}/receipt",
        headers=auth["headers"],
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "RECEIPT_NOT_FOUND"

    key = f"receipts/{auth['user']['id']}/{expense['id']}/{uuid.uuid4()}.png"
    await s3.put_fileobj(key, io.BytesIO(PNG_MAGIC))
    conf = await client.post(
        f"/api/v1/expenses/{expense['id']}/receipt/confirm",
        headers=auth["headers"],
        json={"key": key},
    )
    assert conf.status_code == 200

    dl = await client.get(
        f"/api/v1/expenses/{expense['id']}/receipt",
        headers=auth["headers"],
    )
    assert dl.status_code == 200, dl.text
    url = dl.json()["url"]
    from urllib.parse import parse_qs, urlparse

    qs = parse_qs(urlparse(url).query)
    assert qs.get("X-Amz-Expires") == ["900"]
    assert dl.json()["expires_in"] == 900


@pytest.mark.asyncio
async def test_replacing_receipt_enqueues_old_key(
    client: AsyncClient, engine: AsyncEngine, rates
):
    auth = await register_and_login(client, email="rcpt-replace@example.com")
    expense = await _make_expense(client, auth["headers"], rates)
    old_key = f"receipts/{auth['user']['id']}/{expense['id']}/{uuid.uuid4()}.png"
    new_key = f"receipts/{auth['user']['id']}/{expense['id']}/{uuid.uuid4()}.png"
    await s3.put_fileobj(old_key, io.BytesIO(PNG_MAGIC))
    await s3.put_fileobj(new_key, io.BytesIO(PNG_MAGIC))

    r1 = await client.post(
        f"/api/v1/expenses/{expense['id']}/receipt/confirm",
        headers=auth["headers"],
        json={"key": old_key},
    )
    assert r1.status_code == 200
    r2 = await client.post(
        f"/api/v1/expenses/{expense['id']}/receipt/confirm",
        headers=auth["headers"],
        json={"key": new_key},
    )
    assert r2.status_code == 200
    assert r2.json()["receipt_key"] == new_key
    assert r2.json()["version"] == 3

    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    """
                    SELECT s3_key FROM s3_deletion_queue
                    WHERE s3_key = :k AND deleted_at IS NULL
                    """
                ),
                {"k": old_key},
            )
        ).first()
    assert row is not None


@pytest.mark.asyncio
async def test_delete_expense_enqueues_and_worker_deletes_object(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    rates,
):
    auth = await register_and_login(client, email="rcpt-del@example.com")
    expense = await _make_expense(client, auth["headers"], rates)
    key = f"receipts/{auth['user']['id']}/{expense['id']}/{uuid.uuid4()}.png"
    await s3.put_fileobj(key, io.BytesIO(PNG_MAGIC))
    conf = await client.post(
        f"/api/v1/expenses/{expense['id']}/receipt/confirm",
        headers=auth["headers"],
        json={"key": key},
    )
    assert conf.status_code == 200

    deleted = await client.delete(
        f"/api/v1/expenses/{expense['id']}",
        headers=auth["headers"],
    )
    assert deleted.status_code == 204

    n = await process_s3_deletions(_ctx(session_factory))
    assert n == 1
    assert await s3.head(key) is None

    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    """
                    SELECT deleted_at FROM s3_deletion_queue WHERE s3_key = :k
                    """
                ),
                {"k": key},
            )
        ).mappings().first()
    assert row is not None and row["deleted_at"] is not None


@pytest.mark.asyncio
async def test_delete_failure_backoff_then_retry_succeeds(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    rates,
    monkeypatch: pytest.MonkeyPatch,
):
    auth = await register_and_login(client, email="rcpt-backoff@example.com")
    expense = await _make_expense(client, auth["headers"], rates)
    key = f"receipts/{auth['user']['id']}/{expense['id']}/{uuid.uuid4()}.png"
    await s3.put_fileobj(key, io.BytesIO(PNG_MAGIC))
    await client.post(
        f"/api/v1/expenses/{expense['id']}/receipt/confirm",
        headers=auth["headers"],
        json={"key": key},
    )
    await client.delete(f"/api/v1/expenses/{expense['id']}", headers=auth["headers"])

    real_delete = s3.delete_object
    monkeypatch.setattr(
        s3, "delete_object", AsyncMock(side_effect=RuntimeError("s3 down"))
    )
    n = await process_s3_deletions(_ctx(session_factory))
    assert n == 0

    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    """
                    SELECT attempts, last_error, next_attempt_at, deleted_at
                    FROM s3_deletion_queue WHERE s3_key = :k
                    """
                ),
                {"k": key},
            )
        ).mappings().one()
    assert row["attempts"] == 1
    assert row["last_error"] is not None and "s3 down" in row["last_error"]
    assert row["deleted_at"] is None
    assert row["next_attempt_at"] > datetime.now(UTC)

    # Make the row due again and restore real delete.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                UPDATE s3_deletion_queue
                SET next_attempt_at = now() - interval '1 second'
                WHERE s3_key = :k
                """
            ),
            {"k": key},
        )
    monkeypatch.setattr(s3, "delete_object", real_delete)

    n2 = await process_s3_deletions(_ctx(session_factory))
    assert n2 == 1
    assert await s3.head(key) is None
