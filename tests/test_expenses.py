from datetime import UTC, datetime, timedelta

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
    amount: str | float = "12.50",
    currency: str = "USD",
    description: str | None = "lunch",
    spent_on: str = "2026-03-02",
):
    payload = {
        "category_id": category_id,
        "amount": amount,
        "currency": currency,
        "spent_on": spent_on,
    }
    if description is not None:
        payload["description"] = description
    return await client.post("/api/v1/expenses", headers=headers, json=payload)


@pytest.mark.asyncio
async def test_expense_create_201_money_as_string(client: AsyncClient, rates):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    resp = await _create_expense(client, auth["headers"], category_id=cat["id"], amount="12.50")
    assert resp.status_code == 201
    body = resp.json()
    assert body["amount"] == "12.50"
    assert isinstance(body["amount"], str)
    assert body["currency"] == "USD"

    # JSON number input also accepted and serialized as string
    resp2 = await _create_expense(
        client, auth["headers"], category_id=cat["id"], amount=7.5, spent_on="2026-03-01"
    )
    assert resp2.status_code == 201
    assert resp2.json()["amount"] == "7.50"
    assert isinstance(resp2.json()["amount"], str)


@pytest.mark.asyncio
async def test_expense_get_200(client: AsyncClient, rates):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    created = (await _create_expense(client, auth["headers"], category_id=cat["id"])).json()
    resp = await client.get(
        f"/api/v1/expenses/{created['id']}", headers=auth["headers"]
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]
    assert resp.json()["amount"] == "12.50"


@pytest.mark.asyncio
async def test_expense_delete_204_then_404_and_outbox(
    client: AsyncClient, rates, engine: AsyncEngine
):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    created = (await _create_expense(client, auth["headers"], category_id=cat["id"])).json()

    deleted = await client.delete(
        f"/api/v1/expenses/{created['id']}", headers=auth["headers"]
    )
    assert deleted.status_code == 204

    again = await client.get(
        f"/api/v1/expenses/{created['id']}", headers=auth["headers"]
    )
    assert again.status_code == 404

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT event_type, payload->>'expense_id' AS expense_id
                    FROM outbox_events
                    WHERE event_type = 'expense.deleted'
                    """
                )
            )
        ).mappings().all()
    assert len(rows) == 1
    assert rows[0]["expense_id"] == created["id"]


@pytest.mark.asyncio
async def test_delete_with_receipt_key_enqueues_s3_deletion_queue(
    client: AsyncClient, rates, engine: AsyncEngine
):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    created = (await _create_expense(client, auth["headers"], category_id=cat["id"])).json()
    receipt_key = f"receipts/{auth['user']['id']}/{created['id']}/a.png"

    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE expenses SET receipt_key = :k WHERE id = :id"),
            {"k": receipt_key, "id": created["id"]},
        )

    deleted = await client.delete(
        f"/api/v1/expenses/{created['id']}", headers=auth["headers"]
    )
    assert deleted.status_code == 204

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT s3_key FROM s3_deletion_queue WHERE s3_key = :k"
                ),
                {"k": receipt_key},
            )
        ).first()
    assert row is not None
    assert row[0] == receipt_key


@pytest.mark.asyncio
async def test_amount_zero_rejected(client: AsyncClient, rates):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    resp = await _create_expense(client, auth["headers"], category_id=cat["id"], amount="0")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_amount_negative_rejected(client: AsyncClient, rates):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    resp = await _create_expense(client, auth["headers"], category_id=cat["id"], amount="-1.00")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_amount_more_than_two_decimals_rejected(client: AsyncClient, rates):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    resp = await _create_expense(
        client, auth["headers"], category_id=cat["id"], amount="10.999"
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_future_spent_on_422(client: AsyncClient, rates):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    tomorrow = (datetime.now(UTC).date() + timedelta(days=1)).isoformat()
    resp = await _create_expense(
        client, auth["headers"], category_id=cat["id"], spent_on=tomorrow
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "FUTURE_SPENT_ON"


@pytest.mark.asyncio
async def test_expense_before_earliest_rate_rejected(client: AsyncClient, rates):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    resp = await _create_expense(
        client, auth["headers"], category_id=cat["id"], spent_on="2026-02-01"
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "MISSING_EXCHANGE_RATE"


@pytest.mark.asyncio
async def test_unknown_currency_422(client: AsyncClient, rates):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    resp = await _create_expense(
        client,
        auth["headers"],
        category_id=cat["id"],
        currency="ZZZ",
        spent_on="2026-03-02",
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "MISSING_EXCHANGE_RATE"


@pytest.mark.asyncio
async def test_other_users_expense_returns_404(client: AsyncClient, rates):
    owner = await register_and_login(client, email="owner@example.com")
    other = await register_and_login(client, email="other@example.com")
    cat = await create_category(client, owner["headers"])
    created = (
        await _create_expense(client, owner["headers"], category_id=cat["id"])
    ).json()

    get_resp = await client.get(
        f"/api/v1/expenses/{created['id']}", headers=other["headers"]
    )
    assert get_resp.status_code == 404

    del_resp = await client.delete(
        f"/api/v1/expenses/{created['id']}", headers=other["headers"]
    )
    assert del_resp.status_code == 404


@pytest.mark.asyncio
async def test_filter_category_id(client: AsyncClient, rates):
    auth = await register_and_login(client)
    food = await create_category(client, auth["headers"], name="Food")
    travel = await create_category(client, auth["headers"], name="Travel")
    await _create_expense(client, auth["headers"], category_id=food["id"], amount="1.00")
    await _create_expense(
        client, auth["headers"], category_id=travel["id"], amount="2.00", spent_on="2026-03-01"
    )
    resp = await client.get(
        "/api/v1/expenses",
        headers=auth["headers"],
        params={"category_id": food["id"]},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["category_id"] == food["id"]


@pytest.mark.asyncio
async def test_filter_date_from_to(client: AsyncClient, rates):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    await _create_expense(
        client, auth["headers"], category_id=cat["id"], amount="1.00", spent_on="2026-03-01"
    )
    await _create_expense(
        client, auth["headers"], category_id=cat["id"], amount="2.00", spent_on="2026-03-02"
    )
    await _create_expense(
        client, auth["headers"], category_id=cat["id"], amount="3.00", spent_on="2026-03-04"
    )
    resp = await client.get(
        "/api/v1/expenses",
        headers=auth["headers"],
        params={"date_from": "2026-03-02", "date_to": "2026-03-02"},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["spent_on"] == "2026-03-02"


@pytest.mark.asyncio
async def test_filter_min_max_amount(client: AsyncClient, rates):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    await _create_expense(client, auth["headers"], category_id=cat["id"], amount="5.00")
    await _create_expense(
        client, auth["headers"], category_id=cat["id"], amount="15.00", spent_on="2026-03-01"
    )
    await _create_expense(
        client, auth["headers"], category_id=cat["id"], amount="25.00", spent_on="2026-03-04"
    )
    resp = await client.get(
        "/api/v1/expenses",
        headers=auth["headers"],
        params={"min_amount": "10.00", "max_amount": "20.00"},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["amount"] == "15.00"


@pytest.mark.asyncio
async def test_filter_currency(client: AsyncClient, rates):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    await _create_expense(
        client, auth["headers"], category_id=cat["id"], amount="1.00", currency="USD"
    )
    await _create_expense(
        client,
        auth["headers"],
        category_id=cat["id"],
        amount="2.00",
        currency="EUR",
        spent_on="2026-03-01",
    )
    resp = await client.get(
        "/api/v1/expenses",
        headers=auth["headers"],
        params={"currency": "eur"},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["currency"] == "EUR"


@pytest.mark.asyncio
async def test_filter_q_literal_percent(client: AsyncClient, rates):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])
    await _create_expense(
        client,
        auth["headers"],
        category_id=cat["id"],
        amount="1.00",
        description="save 50% off",
    )
    await _create_expense(
        client,
        auth["headers"],
        category_id=cat["id"],
        amount="2.00",
        description="save 50 off",
        spent_on="2026-03-01",
    )
    resp = await client.get(
        "/api/v1/expenses",
        headers=auth["headers"],
        params={"q": "50%"},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert "50%" in items[0]["description"]


@pytest.mark.asyncio
async def test_invalid_cursor_400(client: AsyncClient, rates):
    auth = await register_and_login(client)
    resp = await client.get(
        "/api/v1/expenses",
        headers=auth["headers"],
        params={"cursor": "not-a-valid-cursor"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_CURSOR"


@pytest.mark.asyncio
async def test_cursor_pagination_stable_when_rows_inserted_between_pages(
    client: AsyncClient, rates
):
    auth = await register_and_login(client)
    cat = await create_category(client, auth["headers"])

    # 25 expenses; several share spent_on to exercise the id tiebreaker.
    # Dates within rates window, newest first in keyset order.
    original_ids: list[str] = []
    spent_dates = (
        ["2026-03-04"] * 5
        + ["2026-03-03"] * 5
        + ["2026-03-02"] * 8
        + ["2026-03-01"] * 7
    )
    assert len(spent_dates) == 25
    for i, spent_on in enumerate(spent_dates):
        resp = await _create_expense(
            client,
            auth["headers"],
            category_id=cat["id"],
            amount=f"{(i % 9) + 1}.00",
            description=f"row-{i}",
            spent_on=spent_on,
        )
        assert resp.status_code == 201, resp.text
        original_ids.append(resp.json()["id"])

    page1 = await client.get(
        "/api/v1/expenses",
        headers=auth["headers"],
        params={"limit": 10},
    )
    assert page1.status_code == 200
    body1 = page1.json()
    assert len(body1["items"]) == 10
    assert body1["next_cursor"] is not None
    page1_ids = [item["id"] for item in body1["items"]]

    # Insert after page 1: some newer than all rows, some on the page-1 boundary spent_on.
    boundary_spent_on = body1["items"][-1]["spent_on"]
    for spent_on in ["2026-03-04", "2026-03-04", boundary_spent_on, boundary_spent_on]:
        resp = await _create_expense(
            client,
            auth["headers"],
            category_id=cat["id"],
            amount="99.00",
            description="inserted-between-pages",
            spent_on=spent_on,
        )
        assert resp.status_code == 201, resp.text

    seen: list[str] = list(page1_ids)
    cursor = body1["next_cursor"]
    while cursor:
        page = await client.get(
            "/api/v1/expenses",
            headers=auth["headers"],
            params={"limit": 10, "cursor": cursor},
        )
        assert page.status_code == 200
        body = page.json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]

    # Every original row exactly once; no duplicate ids in the walk.
    assert len(seen) == len(set(seen))
    for oid in original_ids:
        assert seen.count(oid) == 1
