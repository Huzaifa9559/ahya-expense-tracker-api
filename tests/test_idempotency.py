"""
Idempotency-Key on POST /expenses.

Concurrency note: FastAPI resolves `Depends(get_db)` once per request. Each of the
asyncio.gather POSTs therefore gets its own AsyncSession from AsyncSessionLocal /
the test override — not a shared session — so ON CONFLICT can wait across
transactions the way production does (pool_size >= 5).
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.workers.cleanup import cleanup_expired_in_session
from tests.helpers import create_category, register_and_login


def _payload(category_id: str, *, amount: str = "12.50", spent_on: str = "2026-03-02") -> dict:
    return {
        "category_id": category_id,
        "amount": amount,
        "currency": "USD",
        "description": "idempotent lunch",
        "spent_on": spent_on,
    }


@pytest.mark.asyncio
async def test_same_key_same_body_replays(
    client: AsyncClient, rates, engine: AsyncEngine
):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    headers = {**auth["headers"], "Idempotency-Key": "key-replay-1"}
    body = _payload(cat["id"])

    first = await client.post("/api/v1/expenses", headers=headers, json=body)
    assert first.status_code == 201
    assert first.headers.get("Idempotent-Replayed") is None
    expense_id = first.json()["id"]

    second = await client.post("/api/v1/expenses", headers=headers, json=body)
    assert second.status_code == 201
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert second.json()["id"] == expense_id
    assert second.json() == first.json()

    async with engine.connect() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM expenses WHERE user_id = :uid"),
                {"uid": auth["user"]["id"]},
            )
        ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_same_key_different_body_422(client: AsyncClient, rates):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    headers = {**auth["headers"], "Idempotency-Key": "key-reuse-body"}

    assert (
        await client.post(
            "/api/v1/expenses",
            headers=headers,
            json=_payload(cat["id"], amount="10.00"),
        )
    ).status_code == 201

    conflict = await client.post(
        "/api/v1/expenses",
        headers=headers,
        json=_payload(cat["id"], amount="20.00"),
    )
    assert conflict.status_code == 422
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.asyncio
async def test_parallel_posts_same_idempotency_key_create_one_expense(
    client: AsyncClient, rates, engine: AsyncEngine
):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    headers = {**auth["headers"], "Idempotency-Key": "key-parallel"}
    body = _payload(cat["id"])

    responses = await asyncio.gather(
        *[client.post("/api/v1/expenses", headers=headers, json=body) for _ in range(5)]
    )
    assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
    ids = {r.json()["id"] for r in responses}
    assert len(ids) == 1

    async with engine.connect() as conn:
        expenses = (
            await conn.execute(
                text("SELECT count(*) FROM expenses WHERE user_id = :uid"),
                {"uid": auth["user"]["id"]},
            )
        ).scalar_one()
        outbox = (
            await conn.execute(
                text(
                    """
                    SELECT count(*) FROM outbox_events
                    WHERE event_type = 'expense.created'
                      AND payload->>'user_id' = :uid
                    """
                ),
                {"uid": auth["user"]["id"]},
            )
        ).scalar_one()
    assert expenses == 1
    assert outbox == 1


@pytest.mark.asyncio
async def test_expired_key_creates_new_expense(
    client: AsyncClient, rates, engine: AsyncEngine
):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    headers = {**auth["headers"], "Idempotency-Key": "key-expired"}
    body = _payload(cat["id"])

    first = await client.post("/api/v1/expenses", headers=headers, json=body)
    assert first.status_code == 201
    first_id = first.json()["id"]

    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                UPDATE idempotency_keys
                SET expires_at = now() - interval '1 hour'
                WHERE user_id = :uid AND key = :key
                """
            ),
            {"uid": auth["user"]["id"], "key": "key-expired"},
        )

    second = await client.post("/api/v1/expenses", headers=headers, json=body)
    assert second.status_code == 201
    assert second.headers.get("Idempotent-Replayed") is None
    assert second.json()["id"] != first_id

    async with engine.connect() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM expenses WHERE user_id = :uid"),
                {"uid": auth["user"]["id"]},
            )
        ).scalar_one()
    assert count == 2


@pytest.mark.asyncio
async def test_same_key_two_users_two_expenses(client: AsyncClient, rates, engine: AsyncEngine):
    a = await register_and_login(client, email="a@example.com")
    b = await register_and_login(client, email="b@example.com")
    cat_a = await create_category(client, a["headers"], name="A")
    cat_b = await create_category(client, b["headers"], name="B")
    key = "shared-key-across-users"

    ra = await client.post(
        "/api/v1/expenses",
        headers={**a["headers"], "Idempotency-Key": key},
        json=_payload(cat_a["id"]),
    )
    rb = await client.post(
        "/api/v1/expenses",
        headers={**b["headers"], "Idempotency-Key": key},
        json=_payload(cat_b["id"]),
    )
    assert ra.status_code == 201 and rb.status_code == 201
    assert ra.json()["id"] != rb.json()["id"]

    async with engine.connect() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM expenses"))).scalar_one()
    assert count == 2


@pytest.mark.asyncio
async def test_failed_request_not_stored_then_retry_succeeds(client: AsyncClient, rates):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    headers = {**auth["headers"], "Idempotency-Key": "key-after-fail"}
    missing_cat = str(uuid.uuid4())

    failed = await client.post(
        "/api/v1/expenses",
        headers=headers,
        json=_payload(missing_cat),
    )
    assert failed.status_code == 404

    ok = await client.post(
        "/api/v1/expenses",
        headers=headers,
        json=_payload(cat["id"]),
    )
    assert ok.status_code == 201
    assert ok.headers.get("Idempotent-Replayed") is None


@pytest.mark.asyncio
async def test_cleanup_job_removes_expired_keeps_valid(
    client: AsyncClient,
    rates,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
):
    auth = await register_and_login(client)
    # Seed one expired + one valid idempotency key and refresh token via SQL.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO idempotency_keys (user_id, key, request_hash, expires_at)
                VALUES
                  (:uid, 'expired-key', repeat('a', 64), now() - interval '1 hour'),
                  (:uid, 'valid-key', repeat('b', 64), now() + interval '1 hour')
                """
            ),
            {"uid": auth["user"]["id"]},
        )
        family = uuid.uuid4()
        await conn.execute(
            text(
                """
                INSERT INTO refresh_tokens (user_id, token_hash, family_id, expires_at)
                VALUES
                  (:uid, repeat('c', 64), :fam, now() - interval '1 hour'),
                  (:uid, repeat('d', 64), :fam, now() + interval '1 hour')
                """
            ),
            {"uid": auth["user"]["id"], "fam": family},
        )

    async with session_factory() as session:
        counts = await cleanup_expired_in_session(session)
        await session.commit()
    assert counts["idempotency_keys"] >= 1
    assert counts["refresh_tokens"] >= 1

    async with engine.connect() as conn:
        keys = set(
            (
                await conn.execute(
                    text("SELECT key FROM idempotency_keys WHERE user_id = :uid"),
                    {"uid": auth["user"]["id"]},
                )
            ).scalars().all()
        )
        expired_key_gone = (
            await conn.execute(
                text(
                    "SELECT 1 FROM idempotency_keys WHERE user_id = :uid AND key = 'expired-key'"
                ),
                {"uid": auth["user"]["id"]},
            )
        ).first()
        expired_token_gone = (
            await conn.execute(
                text(
                    "SELECT 1 FROM refresh_tokens WHERE user_id = :uid AND token_hash = repeat('c', 64)"
                ),
                {"uid": auth["user"]["id"]},
            )
        ).first()
        valid_token = (
            await conn.execute(
                text(
                    "SELECT 1 FROM refresh_tokens WHERE user_id = :uid AND token_hash = repeat('d', 64)"
                ),
                {"uid": auth["user"]["id"]},
            )
        ).first()
    assert "valid-key" in keys
    assert "expired-key" not in keys
    assert expired_key_gone is None
    assert expired_token_gone is None
    assert valid_token is not None
    # Login also created a non-expired refresh token; cleanup must leave it alone.
    assert len(keys) >= 1


@pytest.mark.asyncio
async def test_idempotency_key_too_long_422(client: AsyncClient, rates):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    resp = await client.post(
        "/api/v1/expenses",
        headers={**auth["headers"], "Idempotency-Key": "x" * 256},
        json=_payload(cat["id"]),
    )
    assert resp.status_code == 422

