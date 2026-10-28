import asyncio
import json
import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.repositories import budgets as budgets_repo
from app.workers.outbox import process_outbox_batch, purge_processed_outbox
from tests.helpers import create_category, register_and_login


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> dict:
    return {"session_factory": session_factory}


async def _insert_outbox(
    engine: AsyncEngine,
    *,
    user_id: str,
    category_id: str,
    spent_on: str = "2026-03-02",
    processed_at_sql: str | None = None,
    created_at_sql: str | None = None,
) -> None:
    payload = json.dumps(
        {
            "user_id": user_id,
            "category_id": category_id,
            "expense_id": str(uuid.uuid4()),
            "spent_on": spent_on,
        }
    )
    processed = processed_at_sql or "NULL"
    created = created_at_sql or "now()"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"""
                INSERT INTO outbox_events (event_type, payload, processed_at, created_at)
                VALUES (
                    'expense.created',
                    CAST(:payload AS jsonb),
                    {processed},
                    {created}
                )
                """
            ),
            {"payload": payload},
        )


async def _post_expense(
    client: AsyncClient,
    headers: dict,
    *,
    category_id: str,
    amount: str,
    spent_on: str | None = None,
    currency: str = "USD",
):
    from datetime import date as date_cls

    spent = spent_on or date_cls(2026, 3, 2).isoformat()
    resp = await client.post(
        "/api/v1/expenses",
        headers=headers,
        json={
            "category_id": category_id,
            "amount": amount,
            "currency": currency,
            "description": "budget",
            "spent_on": spent,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_expense_to_85_percent_creates_level_80_notification(
    client: AsyncClient,
    rates,
    session_factory: async_sessionmaker[AsyncSession],
):
    auth = await register_and_login(client)
    cat = await create_category(
        client, auth["headers"], name="Groceries", monthly_budget="100.00"
    )
    await _post_expense(client, auth["headers"], category_id=cat["id"], amount="85.00")

    n = await process_outbox_batch(_ctx(session_factory))
    assert n >= 1

    listed = await client.get("/api/v1/notifications", headers=auth["headers"])
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["level"] == 80
    assert items[0]["category_id"] == cat["id"]
    assert "80%" in items[0]["message"]


@pytest.mark.asyncio
async def test_duplicate_budget_alert_event_creates_one_notification(
    client: AsyncClient,
    rates,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
):
    auth = await register_and_login(client)
    cat = await create_category(
        client, auth["headers"], name="Food", monthly_budget="100.00"
    )
    await _post_expense(client, auth["headers"], category_id=cat["id"], amount="85.00")

    await process_outbox_batch(_ctx(session_factory))
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE outbox_events SET processed_at = NULL WHERE processed_at IS NOT NULL")
        )

    await process_outbox_batch(_ctx(session_factory))

    async with engine.connect() as conn:
        count = (
            await conn.execute(text("SELECT count(*) FROM notifications"))
        ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_two_workers_concurrent_no_duplicate_notifications(
    client: AsyncClient,
    rates,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
):
    auth = await register_and_login(client)
    cat = await create_category(
        client, auth["headers"], name="Food", monthly_budget="100.00"
    )
    await _post_expense(client, auth["headers"], category_id=cat["id"], amount="85.00")

    ctx = _ctx(session_factory)
    await asyncio.gather(process_outbox_batch(ctx), process_outbox_batch(ctx))

    async with engine.connect() as conn:
        notes = (await conn.execute(text("SELECT count(*) FROM notifications"))).scalar_one()
        pending = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM outbox_events WHERE processed_at IS NULL"
                )
            )
        ).scalar_one()
        processed = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM outbox_events WHERE processed_at IS NOT NULL"
                )
            )
        ).scalar_one()
    assert notes == 1
    assert pending == 0
    assert processed >= 1


@pytest.mark.asyncio
async def test_80_then_100_across_two_expenses(
    client: AsyncClient,
    rates,
    session_factory: async_sessionmaker[AsyncSession],
):
    auth = await register_and_login(client)
    cat = await create_category(
        client, auth["headers"], name="Food", monthly_budget="100.00"
    )
    await _post_expense(client, auth["headers"], category_id=cat["id"], amount="85.00")
    await process_outbox_batch(_ctx(session_factory))
    await _post_expense(
        client, auth["headers"], category_id=cat["id"], amount="20.00", spent_on="2026-03-01"
    )
    await process_outbox_batch(_ctx(session_factory))

    listed = await client.get("/api/v1/notifications", headers=auth["headers"])
    levels = sorted(i["level"] for i in listed.json()["items"])
    assert levels == [80, 100]


@pytest.mark.asyncio
async def test_jump_50_to_120_creates_both_levels(
    client: AsyncClient,
    rates,
    session_factory: async_sessionmaker[AsyncSession],
):
    auth = await register_and_login(client)
    cat = await create_category(
        client, auth["headers"], name="Food", monthly_budget="100.00"
    )
    await _post_expense(client, auth["headers"], category_id=cat["id"], amount="120.00")
    await process_outbox_batch(_ctx(session_factory))

    levels = sorted(
        i["level"]
        for i in (await client.get("/api/v1/notifications", headers=auth["headers"])).json()[
            "items"
        ]
    )
    assert levels == [80, 100]


@pytest.mark.asyncio
async def test_under_80_and_null_budget_no_notification(
    client: AsyncClient,
    rates,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
):
    auth = await register_and_login(client)
    under = await create_category(
        client, auth["headers"], name="Under", monthly_budget="100.00"
    )
    nobudget = await create_category(
        client, auth["headers"], name="Free", monthly_budget=None
    )
    await _post_expense(client, auth["headers"], category_id=under["id"], amount="50.00")
    await _post_expense(client, auth["headers"], category_id=nobudget["id"], amount="999.00")
    await process_outbox_batch(_ctx(session_factory))

    async with engine.connect() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM notifications"))).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_lowering_total_does_not_retract_alerts(
    client: AsyncClient,
    rates,
    session_factory: async_sessionmaker[AsyncSession],
):
    auth = await register_and_login(client)
    cat = await create_category(
        client, auth["headers"], name="Food", monthly_budget="100.00"
    )
    created = await _post_expense(
        client, auth["headers"], category_id=cat["id"], amount="85.00"
    )
    await process_outbox_batch(_ctx(session_factory))
    assert len((await client.get("/api/v1/notifications", headers=auth["headers"])).json()["items"]) == 1

    patched = await client.patch(
        f"/api/v1/expenses/{created['id']}",
        headers={**auth["headers"], "If-Match": '"1"'},
        json={"amount": "10.00"},
    )
    assert patched.status_code == 200
    await process_outbox_batch(_ctx(session_factory))

    items = (await client.get("/api/v1/notifications", headers=auth["headers"])).json()["items"]
    assert len(items) == 1
    assert items[0]["level"] == 80


@pytest.mark.asyncio
async def test_mixed_currency_converted_before_budget_compare(
    client: AsyncClient,
    rates,
    session_factory: async_sessionmaker[AsyncSession],
):
    # base USD; 80 EUR at rate 1.1 => 88 USD against budget 100 => level 80
    auth = await register_and_login(client, base_currency="USD")
    cat = await create_category(
        client, auth["headers"], name="Travel", monthly_budget="100.00"
    )
    await _post_expense(
        client, auth["headers"], category_id=cat["id"], amount="80.00", currency="EUR"
    )
    await process_outbox_batch(_ctx(session_factory))

    items = (await client.get("/api/v1/notifications", headers=auth["headers"])).json()["items"]
    assert len(items) == 1
    assert items[0]["level"] == 80
    assert "88.00" in items[0]["message"]


@pytest.mark.asyncio
async def test_poison_event_increments_attempts_valid_still_processed(
    client: AsyncClient,
    rates,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
):
    auth = await register_and_login(client)
    good = await create_category(
        client, auth["headers"], name="Good", monthly_budget="100.00"
    )
    await _post_expense(client, auth["headers"], category_id=good["id"], amount="85.00")

    poison_cat = uuid.uuid4()
    await _insert_outbox(
        engine, user_id=auth["user"]["id"], category_id=str(poison_cat)
    )

    real = budgets_repo.category_month_spend_in_base

    async def flaky(*args, **kwargs):
        if kwargs.get("category_id") == poison_cat:
            raise RuntimeError("simulated poison")
        return await real(*args, **kwargs)

    monkeypatch.setattr(budgets_repo, "category_month_spend_in_base", flaky)

    await process_outbox_batch(_ctx(session_factory))

    async with engine.connect() as conn:
        notes = (await conn.execute(text("SELECT count(*) FROM notifications"))).scalar_one()
        poison = (
            await conn.execute(
                text(
                    """
                    SELECT attempts, last_error, processed_at
                    FROM outbox_events
                    WHERE payload->>'category_id' = :cid
                    """
                ),
                {"cid": str(poison_cat)},
            )
        ).one()
        good_processed = (
            await conn.execute(
                text(
                    """
                    SELECT count(*) FROM outbox_events
                    WHERE payload->>'category_id' = :cid
                      AND processed_at IS NOT NULL
                    """
                ),
                {"cid": good["id"]},
            )
        ).scalar_one()
    assert notes == 1
    assert good_processed >= 1
    assert poison[0] == 1
    assert poison[1] is not None
    assert "poison" in poison[1]
    assert poison[2] is None


@pytest.mark.asyncio
async def test_missing_category_marked_processed_no_notification(
    client: AsyncClient,
    rates,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
):
    """README rule: category gone → no alert, events drained (not poisoned)."""
    auth = await register_and_login(client)
    missing = uuid.uuid4()
    await _insert_outbox(
        engine, user_id=auth["user"]["id"], category_id=str(missing)
    )

    await process_outbox_batch(_ctx(session_factory))

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    """
                    SELECT processed_at IS NOT NULL, attempts
                    FROM outbox_events
                    WHERE payload->>'category_id' = :cid
                    """
                ),
                {"cid": str(missing)},
            )
        ).one()
        notes = (await conn.execute(text("SELECT count(*) FROM notifications"))).scalar_one()
    assert row[0] is True
    assert row[1] == 0
    assert notes == 0


@pytest.mark.asyncio
async def test_coalescing_ten_events_one_notification(
    client: AsyncClient,
    rates,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
):
    auth = await register_and_login(client)
    cat = await create_category(
        client, auth["headers"], name="Food", monthly_budget="100.00"
    )
    # One real expense so spend is 85%; then inject 9 duplicate outbox rows for same month.
    await _post_expense(client, auth["headers"], category_id=cat["id"], amount="85.00")
    for _ in range(9):
        await _insert_outbox(
            engine, user_id=auth["user"]["id"], category_id=cat["id"]
        )

    await process_outbox_batch(_ctx(session_factory), batch_size=100)

    async with engine.connect() as conn:
        notes = (await conn.execute(text("SELECT count(*) FROM notifications"))).scalar_one()
        pending = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM outbox_events WHERE processed_at IS NULL"
                )
            )
        ).scalar_one()
        processed = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM outbox_events WHERE processed_at IS NOT NULL"
                )
            )
        ).scalar_one()
    assert notes == 1
    assert pending == 0
    assert processed >= 10


@pytest.mark.asyncio
async def test_retention_job_deletes_old_processed_only(
    client: AsyncClient,
    rates,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
):
    auth = await register_and_login(client)
    await _insert_outbox(
        engine,
        user_id="00000000-0000-0000-0000-000000000001",
        category_id=str(uuid.uuid4()),
        processed_at_sql="now() - interval '8 days'",
        created_at_sql="now() - interval '8 days'",
    )
    await _insert_outbox(
        engine,
        user_id="00000000-0000-0000-0000-000000000001",
        category_id=str(uuid.uuid4()),
        processed_at_sql="now() - interval '1 day'",
        created_at_sql="now() - interval '1 day'",
    )
    await _insert_outbox(
        engine,
        user_id=auth["user"]["id"],
        category_id=str(uuid.uuid4()),
    )

    deleted = await purge_processed_outbox(_ctx(session_factory))
    assert deleted >= 1

    async with engine.connect() as conn:
        old_gone = (
            await conn.execute(
                text(
                    """
                    SELECT count(*) FROM outbox_events
                    WHERE processed_at < now() - interval '7 days'
                    """
                )
            )
        ).scalar_one()
        recent_kept = (
            await conn.execute(
                text(
                    """
                    SELECT count(*) FROM outbox_events
                    WHERE processed_at IS NOT NULL
                      AND processed_at > now() - interval '7 days'
                    """
                )
            )
        ).scalar_one()
        pending = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM outbox_events WHERE processed_at IS NULL"
                )
            )
        ).scalar_one()
    assert old_gone == 0
    assert recent_kept >= 1
    assert pending >= 1


@pytest.mark.asyncio
async def test_notifications_only_current_user(
    client: AsyncClient,
    rates,
    session_factory: async_sessionmaker[AsyncSession],
):
    a = await register_and_login(client, email="a@example.com")
    b = await register_and_login(client, email="b@example.com")
    cat_a = await create_category(
        client, a["headers"], name="A", monthly_budget="100.00"
    )
    cat_b = await create_category(
        client, b["headers"], name="B", monthly_budget="100.00"
    )
    await _post_expense(client, a["headers"], category_id=cat_a["id"], amount="85.00")
    await _post_expense(client, b["headers"], category_id=cat_b["id"], amount="90.00")
    await process_outbox_batch(_ctx(session_factory))

    items_a = (await client.get("/api/v1/notifications", headers=a["headers"])).json()["items"]
    items_b = (await client.get("/api/v1/notifications", headers=b["headers"])).json()["items"]
    assert len(items_a) == 1 and items_a[0]["category_id"] == cat_a["id"]
    assert len(items_b) == 1 and items_b[0]["category_id"] == cat_b["id"]
