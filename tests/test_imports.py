"""CSV import: accept endpoint + worker (called directly, no live arq)."""

from __future__ import annotations

import io
import uuid
from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.services import imports as imports_service
from app.workers.import_csv import import_csv
from tests.helpers import create_category, register_and_login

CSV_HEADER = "date,amount,currency,category,description\n"


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> dict:
    return {"session_factory": session_factory}


def _csv_bytes(rows: list[str], *, crlf: bool = False, bom: bool = False) -> bytes:
    body = CSV_HEADER + "\n".join(rows) + ("\n" if rows else "")
    if crlf:
        body = body.replace("\n", "\r\n")
    raw = body.encode("utf-8")
    if bom:
        raw = b"\xef\xbb\xbf" + raw
    return raw


async def _upload(
    client: AsyncClient,
    headers: dict,
    content: bytes,
    *,
    filename: str = "expenses.csv",
):
    return await client.post(
        "/api/v1/expenses/import",
        headers=headers,
        files={"file": (filename, io.BytesIO(content), "text/csv")},
    )


async def _run_job(
    session_factory: async_sessionmaker[AsyncSession], job_id: str
) -> str:
    return await import_csv(_ctx(session_factory), job_id)


async def _expense_count(engine: AsyncEngine, user_id: str) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(
            text("SELECT COUNT(*) FROM expenses WHERE user_id = :uid"),
            {"uid": user_id},
        )
        return int(result.scalar_one())


async def _outbox_count(engine: AsyncEngine) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(text("SELECT COUNT(*) FROM outbox_events"))
        return int(result.scalar_one())


async def _error_count(engine: AsyncEngine, job_id: str) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(
            text("SELECT COUNT(*) FROM import_errors WHERE job_id = :jid"),
            {"jid": job_id},
        )
        return int(result.scalar_one())


@pytest.mark.asyncio
async def test_csv_import_good_and_bad_rows_counts(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    rates,
):
    auth = await register_and_login(client, email="import-mix@example.com")
    await create_category(client, auth["headers"], name="Food")

    # Row numbers: header=1; data starts at 2.
    rows = [
        "2026-03-02,10.00,USD,Food,ok",  # 2 good
        "not-a-date,10.00,USD,Food,bad date",  # 3
        "2099-01-01,10.00,USD,Food,future",  # 4
        "2026-03-02,0.00,USD,Food,zero",  # 5
        "2026-03-02,1.123,USD,Food,3dp",  # 6
        "2026-03-02,10.00,XYZ,Food,unknown currency",  # 7
        "2026-03-02,10.00,USD,Ghost,unknown category",  # 8
        "2020-01-01,10.00,USD,Food,before earliest rate",  # 9
        "2026-03-02,5.50,EUR,Food,good eur",  # 10 good
    ]
    resp = await _upload(client, auth["headers"], _csv_bytes(rows))
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    result = await _run_job(session_factory, job_id)
    assert result == "completed"

    status = await client.get(f"/api/v1/imports/{job_id}", headers=auth["headers"])
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["status"] == "completed"
    assert body["total_rows"] == 9
    assert body["processed_rows"] == 9
    assert body["failed_rows"] == 7
    assert body["succeeded_rows"] == 2
    assert await _expense_count(engine, auth["user"]["id"]) == 2

    by_row = {e["row_number"]: e["reason"] for e in body["errors"]}
    assert 3 in by_row and "date" in by_row[3].lower()
    assert 4 in by_row and "future" in by_row[4].lower()
    assert 5 in by_row and "greater" in by_row[5].lower()
    assert 6 in by_row and "decimal" in by_row[6].lower()
    assert 7 in by_row and "XYZ" in by_row[7]
    assert 8 in by_row and "Ghost" in by_row[8]
    assert 9 in by_row and "rate" in by_row[9].lower()


@pytest.mark.asyncio
async def test_same_file_uploaded_twice_same_job_no_duplicate_expenses(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    rates,
):
    auth = await register_and_login(client, email="import-dedupe@example.com")
    await create_category(client, auth["headers"], name="Food")
    content = _csv_bytes(["2026-03-02,10.00,USD,Food,once"])

    first = await _upload(client, auth["headers"], content)
    assert first.status_code == 202
    job_id = first.json()["job_id"]
    await _run_job(session_factory, job_id)

    second = await _upload(client, auth["headers"], content)
    assert second.status_code == 202
    assert second.json()["job_id"] == job_id
    assert await _expense_count(engine, auth["user"]["id"]) == 1


@pytest.mark.asyncio
async def test_identical_legitimate_csv_rows_both_import(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    rates,
):
    auth = await register_and_login(client, email="import-identical@example.com")
    await create_category(client, auth["headers"], name="Food")
    # Same content, different row numbers → different dedupe_hash → both import.
    content = _csv_bytes(
        [
            "2026-03-02,10.00,USD,Food,same",
            "2026-03-02,10.00,USD,Food,same",
        ]
    )
    resp = await _upload(client, auth["headers"], content)
    job_id = resp.json()["job_id"]
    await _run_job(session_factory, job_id)
    assert await _expense_count(engine, auth["user"]["id"]) == 2


@pytest.mark.asyncio
async def test_worker_retry_no_duplicate_expenses_or_errors(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    rates,
):
    auth = await register_and_login(client, email="import-retry@example.com")
    await create_category(client, auth["headers"], name="Food")
    content = _csv_bytes(
        [
            "2026-03-02,10.00,USD,Food,ok",
            "bad,10.00,USD,Food,err",
        ]
    )
    resp = await _upload(client, auth["headers"], content)
    job_id = resp.json()["job_id"]

    assert await _run_job(session_factory, job_id) == "completed"
    # Simulate crash-retry: force status back to processing so reset path runs.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                UPDATE import_jobs
                SET status = 'processing', finished_at = NULL
                WHERE id = :jid
                """
            ),
            {"jid": job_id},
        )

    assert await _run_job(session_factory, job_id) == "completed"
    assert await _expense_count(engine, auth["user"]["id"]) == 1
    assert await _error_count(engine, job_id) == 1

    status = await client.get(f"/api/v1/imports/{job_id}", headers=auth["headers"])
    body = status.json()
    assert body["processed_rows"] == 2
    assert body["failed_rows"] == 1
    assert body["succeeded_rows"] == 1


@pytest.mark.asyncio
async def test_large_csv_batches_and_outbox_coalesce(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    rates,
):
    auth = await register_and_login(client, email="import-large@example.com")
    await create_category(client, auth["headers"], name="Food")
    await create_category(client, auth["headers"], name="Travel", monthly_budget="200.00")

    # 3500 rows → 4 batches (1000+1000+1000+500). Mix two categories / one month.
    rows = []
    for i in range(3500):
        cat = "Food" if i % 2 == 0 else "Travel"
        rows.append(f"2026-03-02,1.00,USD,{cat},r{i}")
    resp = await _upload(client, auth["headers"], _csv_bytes(rows))
    job_id = resp.json()["job_id"]
    assert await _run_job(session_factory, job_id) == "completed"

    status = await client.get(f"/api/v1/imports/{job_id}", headers=auth["headers"])
    body = status.json()
    assert body["total_rows"] == 3500
    assert body["succeeded_rows"] == 3500
    assert await _expense_count(engine, auth["user"]["id"]) == 3500

    # Per batch: one outbox event per (category, month). 4 batches × 2 cats = 8.
    assert await _outbox_count(engine) == 8


@pytest.mark.asyncio
async def test_wrong_header_and_non_csv_rejected(
    client: AsyncClient,
    rates,
):
    auth = await register_and_login(client, email="import-header@example.com")
    bad_header = b"fecha,monto,moneda,categoria,desc\n2026-03-02,1,USD,Food,x\n"
    resp = await _upload(client, auth["headers"], bad_header)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"

    resp2 = await _upload(
        client,
        auth["headers"],
        _csv_bytes(["2026-03-02,1.00,USD,Food,x"]),
        filename="expenses.exe",
    )
    assert resp2.status_code == 422


@pytest.mark.asyncio
async def test_other_users_import_returns_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    rates,
):
    owner = await register_and_login(client, email="import-owner@example.com")
    await create_category(client, owner["headers"], name="Food")
    resp = await _upload(
        client,
        owner["headers"],
        _csv_bytes(["2026-03-02,1.00,USD,Food,x"]),
    )
    job_id = resp.json()["job_id"]
    await _run_job(session_factory, job_id)

    other = await register_and_login(client, email="import-other@example.com")
    sneak = await client.get(f"/api/v1/imports/{job_id}", headers=other["headers"])
    assert sneak.status_code == 404
    assert sneak.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_errors_list_capped_with_truncated_flag(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    rates,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(imports_service, "ERRORS_LIST_CAP", 5)
    auth = await register_and_login(client, email="import-cap@example.com")
    await create_category(client, auth["headers"], name="Food")
    # 8 bad rows → cap 5 → truncated true
    rows = [f"bad-{i},1.00,USD,Food,x" for i in range(8)]
    resp = await _upload(client, auth["headers"], _csv_bytes(rows))
    job_id = resp.json()["job_id"]
    await _run_job(session_factory, job_id)

    status = await client.get(f"/api/v1/imports/{job_id}", headers=auth["headers"])
    body = status.json()
    assert body["failed_rows"] == 8
    assert len(body["errors"]) == 5
    assert body["errors_truncated"] is True


@pytest.mark.asyncio
async def test_crlf_and_bom_parse_correctly(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    rates,
):
    auth = await register_and_login(client, email="import-bom@example.com")
    await create_category(client, auth["headers"], name="Food")
    content = _csv_bytes(
        ["2026-03-02,10.00,USD,Food,bom-crlf"],
        crlf=True,
        bom=True,
    )
    resp = await _upload(client, auth["headers"], content)
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    assert await _run_job(session_factory, job_id) == "completed"
    assert await _expense_count(engine, auth["user"]["id"]) == 1

    status = await client.get(f"/api/v1/imports/{job_id}", headers=auth["headers"])
    assert status.json()["succeeded_rows"] == 1
    assert status.json()["failed_rows"] == 0
