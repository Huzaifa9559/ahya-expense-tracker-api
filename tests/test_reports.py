from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.helpers import (
    create_category,
    insert_exchange_rates,
    monthly_report_explain_sql,
    register_and_login,
)


async def _seed_march_2026_rates(engine: AsyncEngine) -> None:
    """Deterministic rates: EUR has a gap on 2026-03-03 (as-of falls back to 03-02)."""
    rows: list[tuple[str, Decimal, date]] = []
    for d in (date(2026, 2, 1), date(2026, 3, 1), date(2026, 3, 2), date(2026, 3, 4)):
        rows.append(("USD", Decimal("1.00000000"), d))
        rows.append(("PKR", Decimal("0.00350000"), d))
        rows.append(("AED", Decimal("0.27000000"), d))
    # EUR: present on Feb 1, Mar 1–2, Mar 4 — missing Mar 3
    for d in (date(2026, 2, 1), date(2026, 3, 1), date(2026, 3, 2), date(2026, 3, 4)):
        rate = Decimal("1.20000000") if d == date(2026, 3, 4) else Decimal("1.10000000")
        rows.append(("EUR", rate, d))
    # Also Mar 3 for non-EUR so as-of base PKR still resolves that day
    rows.append(("USD", Decimal("1.00000000"), date(2026, 3, 3)))
    rows.append(("PKR", Decimal("0.00350000"), date(2026, 3, 3)))
    rows.append(("AED", Decimal("0.27000000"), date(2026, 3, 3)))
    await insert_exchange_rates(engine, rows)


async def _expense(
    client: AsyncClient,
    headers: dict,
    *,
    category_id: str,
    amount: str,
    currency: str,
    spent_on: str,
):
    resp = await client.post(
        "/api/v1/expenses",
        headers=headers,
        json={
            "category_id": category_id,
            "amount": amount,
            "currency": currency,
            "spent_on": spent_on,
            "description": f"{currency} {amount}",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_monthly_report_mixed_currencies_and_missing_rate_day(
    client: AsyncClient, engine: AsyncEngine
):
    await _seed_march_2026_rates(engine)
    auth = await register_and_login(client, base_currency="PKR")
    cat = await create_category(
        client, auth["headers"], name="Travel", monthly_budget="10000.00"
    )

    await _expense(
        client, auth["headers"], category_id=cat["id"], amount="1000.00",
        currency="PKR", spent_on="2026-03-02",
    )
    await _expense(
        client, auth["headers"], category_id=cat["id"], amount="10.00",
        currency="USD", spent_on="2026-03-02",
    )
    # Gap day for EUR: no row on 2026-03-03 → LATERAL uses 2026-03-02 rate 1.1
    await _expense(
        client, auth["headers"], category_id=cat["id"], amount="10.00",
        currency="EUR", spent_on="2026-03-03",
    )
    await _expense(
        client, auth["headers"], category_id=cat["id"], amount="10.00",
        currency="AED", spent_on="2026-03-02",
    )

    # Hand arithmetic (amount_in_base = amount * src.rate_to_usd / base_pkr):
    #   PKR 1000 @ 0.0035/0.0035 = 1000
    #   USD   10 @ 1.0/0.0035    = 10 / 0.0035     = 2857.142857...
    #   EUR   10 @ 1.1/0.0035    = 11 / 0.0035     = 3142.857142...  (as-of Mar 2)
    #   AED   10 @ 0.27/0.0035   = 2.7 / 0.0035    = 771.428571...
    # Sum then ROUND(..., 2) once at the end (not per row) → 7771.43
    expected_total = (
        Decimal("1000") * Decimal("0.0035") / Decimal("0.0035")
        + Decimal("10") * Decimal("1") / Decimal("0.0035")
        + Decimal("10") * Decimal("1.1") / Decimal("0.0035")
        + Decimal("10") * Decimal("0.27") / Decimal("0.0035")
    ).quantize(Decimal("0.01"))
    assert expected_total == Decimal("7771.43")

    resp = await client.get(
        "/api/v1/reports/monthly",
        headers=auth["headers"],
        params={"year": 2026, "month": 3},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["year"] == 2026
    assert body["month"] == 3
    assert body["base_currency"] == "PKR"
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["category_id"] == cat["id"]
    assert item["total"] == "7771.43"
    assert isinstance(item["total"], str)
    assert item["previous_total"] == "0.00"
    assert item["change"] == "7771.43"
    assert item["budget"] == "10000.00"
    # percent_used = ROUND(7771.43 / 10000 * 100, 2) = 77.71
    assert item["percent_used"] == pytest.approx(77.71)


@pytest.mark.asyncio
async def test_monthly_report_negative_change_when_spend_only_last_month(
    client: AsyncClient, engine: AsyncEngine
):
    await _seed_march_2026_rates(engine)
    auth = await register_and_login(client, base_currency="USD")
    cat = await create_category(
        client, auth["headers"], name="Food", monthly_budget="100.00"
    )
    # Spend only in February (previous month for a March report)
    await _expense(
        client, auth["headers"], category_id=cat["id"], amount="40.00",
        currency="USD", spent_on="2026-02-01",
    )

    resp = await client.get(
        "/api/v1/reports/monthly",
        headers=auth["headers"],
        params={"year": 2026, "month": 3},
    )
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["total"] == "0.00"
    assert item["previous_total"] == "40.00"
    assert item["change"] == "-40.00"
    assert isinstance(item["change"], str)


@pytest.mark.asyncio
async def test_percent_used_null_budget_and_zero_budget(
    client: AsyncClient, engine: AsyncEngine
):
    await _seed_march_2026_rates(engine)
    auth = await register_and_login(client, base_currency="USD")
    no_budget = await create_category(
        client, auth["headers"], name="Misc", monthly_budget=None
    )
    zero_budget = await create_category(
        client, auth["headers"], name="Zero", monthly_budget="0.00"
    )
    with_budget = await create_category(
        client, auth["headers"], name="Food", monthly_budget="100.00"
    )
    await _expense(
        client, auth["headers"], category_id=no_budget["id"], amount="10.00",
        currency="USD", spent_on="2026-03-02",
    )
    await _expense(
        client, auth["headers"], category_id=zero_budget["id"], amount="5.00",
        currency="USD", spent_on="2026-03-02",
    )
    await _expense(
        client, auth["headers"], category_id=with_budget["id"], amount="25.00",
        currency="USD", spent_on="2026-03-02",
    )

    resp = await client.get(
        "/api/v1/reports/monthly",
        headers=auth["headers"],
        params={"year": 2026, "month": 3},
    )
    assert resp.status_code == 200
    by_name = {i["name"]: i for i in resp.json()["items"]}
    assert by_name["Misc"]["percent_used"] is None
    assert by_name["Misc"]["budget"] is None
    assert by_name["Zero"]["percent_used"] is None
    assert by_name["Zero"]["budget"] == "0.00"
    assert by_name["Food"]["percent_used"] == pytest.approx(25.0)
    assert by_name["Food"]["total"] == "25.00"


@pytest.mark.asyncio
async def test_january_report_compares_against_previous_december(
    client: AsyncClient, engine: AsyncEngine
):
    rows = []
    for d in (date(2025, 12, 1), date(2025, 12, 15), date(2026, 1, 1), date(2026, 1, 10)):
        rows.append(("USD", Decimal("1.00000000"), d))
    await insert_exchange_rates(engine, rows)

    auth = await register_and_login(client, base_currency="USD")
    cat = await create_category(client, auth["headers"], name="Rent", monthly_budget="500.00")
    await _expense(
        client, auth["headers"], category_id=cat["id"], amount="100.00",
        currency="USD", spent_on="2025-12-15",
    )
    await _expense(
        client, auth["headers"], category_id=cat["id"], amount="30.00",
        currency="USD", spent_on="2026-01-10",
    )

    resp = await client.get(
        "/api/v1/reports/monthly",
        headers=auth["headers"],
        params={"year": 2026, "month": 1},
    )
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["total"] == "30.00"
    assert item["previous_total"] == "100.00"
    assert item["change"] == "-70.00"


@pytest.mark.asyncio
async def test_category_with_no_spend_appears_with_zero_total(
    client: AsyncClient, engine: AsyncEngine
):
    await _seed_march_2026_rates(engine)
    auth = await register_and_login(client, base_currency="USD")
    await create_category(client, auth["headers"], name="Empty", monthly_budget="50.00")

    resp = await client.get(
        "/api/v1/reports/monthly",
        headers=auth["headers"],
        params={"year": 2026, "month": 3},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "Empty"
    assert items[0]["total"] == "0.00"
    assert items[0]["previous_total"] == "0.00"
    assert items[0]["change"] == "0.00"


@pytest.mark.asyncio
async def test_user_with_no_categories_empty_items(
    client: AsyncClient, engine: AsyncEngine
):
    await _seed_march_2026_rates(engine)
    auth = await register_and_login(client, base_currency="USD")
    resp = await client.get(
        "/api/v1/reports/monthly",
        headers=auth["headers"],
        params={"year": 2026, "month": 3},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["base_currency"] == "USD"


@pytest.mark.asyncio
async def test_other_users_expenses_never_appear(
    client: AsyncClient, engine: AsyncEngine
):
    await _seed_march_2026_rates(engine)
    owner = await register_and_login(client, email="owner@example.com", base_currency="USD")
    other = await register_and_login(client, email="other@example.com", base_currency="USD")
    owner_cat = await create_category(client, owner["headers"], name="Mine")
    other_cat = await create_category(client, other["headers"], name="Theirs")
    await _expense(
        client, other["headers"], category_id=other_cat["id"], amount="99.00",
        currency="USD", spent_on="2026-03-02",
    )
    await create_category(client, owner["headers"], name="Empty")

    resp = await client.get(
        "/api/v1/reports/monthly",
        headers=owner["headers"],
        params={"year": 2026, "month": 3},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert {i["name"] for i in items} == {"Mine", "Empty"}
    by_name = {i["name"]: i for i in items}
    assert by_name["Mine"]["total"] == "0.00"
    assert all(i["total"] != "99.00" for i in items)
    assert owner_cat["id"] in {i["category_id"] for i in items}


@pytest.mark.asyncio
async def test_invalid_month_422(client: AsyncClient, engine: AsyncEngine):
    await _seed_march_2026_rates(engine)
    auth = await register_and_login(client, base_currency="USD")
    resp = await client.get(
        "/api/v1/reports/monthly",
        headers=auth["headers"],
        params={"year": 2026, "month": 13},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_missing_year_or_month_422(client: AsyncClient, engine: AsyncEngine):
    await _seed_march_2026_rates(engine)
    auth = await register_and_login(client, base_currency="USD")
    assert (
        await client.get(
            "/api/v1/reports/monthly",
            headers=auth["headers"],
            params={"year": 2026},
        )
    ).status_code == 422
    assert (
        await client.get(
            "/api/v1/reports/monthly",
            headers=auth["headers"],
            params={"month": 3},
        )
    ).status_code == 422


def test_explain_helper_mentions_analyze_buffers():
    sql = monthly_report_explain_sql()
    assert "EXPLAIN (ANALYZE, BUFFERS)" in sql
    assert "JOIN LATERAL" in sql
    assert "LAG(" in sql
