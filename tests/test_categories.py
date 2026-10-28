import pytest
from httpx import AsyncClient

from tests.helpers import create_category, register_and_login


@pytest.mark.asyncio
async def test_category_create_201(client: AsyncClient, rates):
    auth = await register_and_login(client)
    resp = await client.post(
        "/api/v1/categories",
        headers=auth["headers"],
        json={"name": "  Food  ", "monthly_budget": "100.00"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Food"
    assert body["monthly_budget"] == "100.00"
    assert isinstance(body["monthly_budget"], str)


@pytest.mark.asyncio
async def test_category_duplicate_name_409(client: AsyncClient, rates):
    auth = await register_and_login(client)
    await create_category(client, auth["headers"], name="Food")
    resp = await client.post(
        "/api/v1/categories",
        headers=auth["headers"],
        json={"name": "Food"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


@pytest.mark.asyncio
async def test_category_list_shows_spend_budget_percent_used(client: AsyncClient, rates):
    auth = await register_and_login(client, base_currency="USD")
    cat = await create_category(
        client, auth["headers"], name="Travel", monthly_budget="100.00"
    )
    today = rates["today"].isoformat()

    # 10 USD + 10 EUR (rate 1.1) => spend in USD = 10 + 11 = 21.00; percent = 21%
    for amount, currency in [("10.00", "USD"), ("10.00", "EUR")]:
        resp = await client.post(
            "/api/v1/expenses",
            headers=auth["headers"],
            json={
                "category_id": cat["id"],
                "amount": amount,
                "currency": currency,
                "description": "trip",
                "spent_on": today,
            },
        )
        assert resp.status_code == 201, resp.text

    listed = await client.get("/api/v1/categories", headers=auth["headers"])
    assert listed.status_code == 200
    items = listed.json()
    assert len(items) == 1
    item = items[0]
    assert item["monthly_budget"] == "100.00"
    assert item["spend"] == "21.00"
    assert isinstance(item["spend"], str)
    assert item["percent_used"] == pytest.approx(21.0)


@pytest.mark.asyncio
async def test_other_users_category_returns_404(client: AsyncClient, rates):
    owner = await register_and_login(client, email="owner@example.com")
    other = await register_and_login(client, email="other@example.com")
    cat = await create_category(client, owner["headers"], name="Secret")

    get_resp = await client.get(
        f"/api/v1/categories/{cat['id']}", headers=other["headers"]
    )
    assert get_resp.status_code == 404

    create_exp = await client.post(
        "/api/v1/expenses",
        headers=other["headers"],
        json={
            "category_id": cat["id"],
            "amount": "5.00",
            "currency": "USD",
            "spent_on": rates["anchor"].isoformat(),
        },
    )
    assert create_exp.status_code == 404
