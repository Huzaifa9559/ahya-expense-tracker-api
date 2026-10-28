import asyncio
import uuid
from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.helpers import create_category, register_and_login


async def _create_expense(
    client: AsyncClient,
    headers: dict,
    *,
    category_id: str,
    amount: str = "10.00",
    spent_on: str = "2026-03-02",
    currency: str = "USD",
) -> dict:
    resp = await client.post(
        "/api/v1/expenses",
        headers=headers,
        json={
            "category_id": category_id,
            "amount": amount,
            "currency": currency,
            "description": "etag test",
            "spent_on": spent_on,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_get_returns_etag_quoted_one(client: AsyncClient, rates):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    created = await _create_expense(client, auth["headers"], category_id=cat["id"])

    resp = await client.get(
        f"/api/v1/expenses/{created['id']}", headers=auth["headers"]
    )
    assert resp.status_code == 200
    assert resp.headers.get("ETag") == '"1"'
    assert resp.json()["version"] == 1


@pytest.mark.asyncio
async def test_patch_matching_etag_bumps_version(client: AsyncClient, rates):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    created = await _create_expense(client, auth["headers"], category_id=cat["id"])

    resp = await client.patch(
        f"/api/v1/expenses/{created['id']}",
        headers={**auth["headers"], "If-Match": '"1"'},
        json={"amount": "15.00"},
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 2
    assert resp.json()["amount"] == "15.00"
    assert resp.headers.get("ETag") == '"2"'

    weak = await client.patch(
        f"/api/v1/expenses/{created['id']}",
        headers={**auth["headers"], "If-Match": 'W/"2"'},
        json={"amount": "16.00"},
    )
    assert weak.status_code == 200
    assert weak.json()["version"] == 3
    assert weak.headers.get("ETag") == '"3"'


@pytest.mark.asyncio
async def test_stale_missing_malformed_if_match(client: AsyncClient, rates):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    created = await _create_expense(client, auth["headers"], category_id=cat["id"])

    stale = await client.patch(
        f"/api/v1/expenses/{created['id']}",
        headers={**auth["headers"], "If-Match": '"99"'},
        json={"amount": "11.00"},
    )
    assert stale.status_code == 412
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"

    missing = await client.patch(
        f"/api/v1/expenses/{created['id']}",
        headers=auth["headers"],
        json={"amount": "11.00"},
    )
    assert missing.status_code == 428
    assert missing.json()["error"]["code"] == "PRECONDITION_REQUIRED"

    bad = await client.patch(
        f"/api/v1/expenses/{created['id']}",
        headers={**auth["headers"], "If-Match": "not-a-version"},
        json={"amount": "11.00"},
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_IF_MATCH"


@pytest.mark.asyncio
async def test_other_users_expense_patch_404(client: AsyncClient, rates):
    owner = await register_and_login(client, email="owner@example.com")
    other = await register_and_login(client, email="other@example.com")
    cat = await create_category(client, owner["headers"])
    created = await _create_expense(client, owner["headers"], category_id=cat["id"])

    resp = await client.patch(
        f"/api/v1/expenses/{created['id']}",
        headers={**other["headers"], "If-Match": '"1"'},
        json={"amount": "99.00"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_parallel_patches_same_etag_one_200_one_412(
    client: AsyncClient, rates, engine: AsyncEngine
):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    created = await _create_expense(
        client, auth["headers"], category_id=cat["id"], amount="10.00"
    )
    expense_id = created["id"]

    r1, r2 = await asyncio.gather(
        client.patch(
            f"/api/v1/expenses/{expense_id}",
            headers={**auth["headers"], "If-Match": '"1"'},
            json={"amount": "21.00"},
        ),
        client.patch(
            f"/api/v1/expenses/{expense_id}",
            headers={**auth["headers"], "If-Match": '"1"'},
            json={"amount": "22.00"},
        ),
    )
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 412]

    winner = r1 if r1.status_code == 200 else r2
    winner_amount = winner.json()["amount"]

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT amount::text, version FROM expenses WHERE id = :id"),
                {"id": expense_id},
            )
        ).one()
    assert row[0] == winner_amount
    assert row[1] == 2


@pytest.mark.asyncio
async def test_category_change_writes_two_outbox_events(
    client: AsyncClient, rates, engine: AsyncEngine
):
    auth = await register_and_login(client)
    food = await create_category(client, auth["headers"], name="Food")
    travel = await create_category(client, auth["headers"], name="Travel")
    created = await _create_expense(client, auth["headers"], category_id=food["id"])

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM outbox_events"))

    resp = await client.patch(
        f"/api/v1/expenses/{created['id']}",
        headers={**auth["headers"], "If-Match": '"1"'},
        json={"category_id": travel["id"]},
    )
    assert resp.status_code == 200

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT payload->>'category_id' AS category_id,
                           payload->>'spent_on' AS spent_on
                    FROM outbox_events
                    WHERE event_type = 'expense.updated'
                    ORDER BY payload->>'category_id'
                    """
                )
            )
        ).mappings().all()
    assert len(rows) == 2
    cats = {r["category_id"] for r in rows}
    assert cats == {food["id"], travel["id"]}


@pytest.mark.asyncio
async def test_spent_on_month_change_writes_two_outbox_events(
    client: AsyncClient, rates, engine: AsyncEngine
):
    # rates fixture is March-centric; seed February so the month move validates.
    from decimal import Decimal

    from tests.helpers import insert_exchange_rates

    await insert_exchange_rates(
        engine,
        [
            ("USD", Decimal("1.00000000"), date(2026, 2, 1)),
            ("PKR", Decimal("0.00350000"), date(2026, 2, 1)),
            ("EUR", Decimal("1.10000000"), date(2026, 2, 1)),
            ("AED", Decimal("0.27000000"), date(2026, 2, 1)),
        ],
    )
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    created = await _create_expense(
        client, auth["headers"], category_id=cat["id"], spent_on="2026-03-02"
    )

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM outbox_events"))

    resp = await client.patch(
        f"/api/v1/expenses/{created['id']}",
        headers={**auth["headers"], "If-Match": '"1"'},
        json={"spent_on": "2026-02-01"},
    )
    assert resp.status_code == 200

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT payload->>'spent_on' AS spent_on
                    FROM outbox_events
                    WHERE event_type = 'expense.updated'
                    ORDER BY payload->>'spent_on'
                    """
                )
            )
        ).mappings().all()
    assert len(rows) == 2
    assert {r["spent_on"] for r in rows} == {"2026-02-01", "2026-03-02"}


@pytest.mark.asyncio
async def test_amount_only_change_writes_one_outbox_event(
    client: AsyncClient, rates, engine: AsyncEngine
):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    created = await _create_expense(client, auth["headers"], category_id=cat["id"])

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM outbox_events"))

    resp = await client.patch(
        f"/api/v1/expenses/{created['id']}",
        headers={**auth["headers"], "If-Match": '"1"'},
        json={"amount": "30.00"},
    )
    assert resp.status_code == 200

    async with engine.connect() as conn:
        count = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM outbox_events WHERE event_type = 'expense.updated'"
                )
            )
        ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_patch_validation_amount_future_empty(client: AsyncClient, rates):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    created = await _create_expense(client, auth["headers"], category_id=cat["id"])
    headers = {**auth["headers"], "If-Match": '"1"'}

    zero = await client.patch(
        f"/api/v1/expenses/{created['id']}",
        headers=headers,
        json={"amount": "0"},
    )
    assert zero.status_code == 422

    future = await client.patch(
        f"/api/v1/expenses/{created['id']}",
        headers=headers,
        json={"spent_on": "2099-01-01"},
    )
    assert future.status_code == 422
    assert future.json()["error"]["code"] == "FUTURE_SPENT_ON"

    empty = await client.patch(
        f"/api/v1/expenses/{created['id']}",
        headers=headers,
        json={},
    )
    assert empty.status_code == 422
