from datetime import date
from decimal import Decimal
from pathlib import Path

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

ROOT = Path(__file__).resolve().parents[1]


async def insert_exchange_rates(
    engine: AsyncEngine,
    rows: list[tuple[str, Decimal, date]],
) -> None:
    async with engine.begin() as conn:
        for currency, rate, valid_on in rows:
            await conn.execute(
                text(
                    """
                    INSERT INTO exchange_rates (currency, rate_to_usd, valid_on)
                    VALUES (:currency, :rate, :valid_on)
                    ON CONFLICT (currency, valid_on) DO NOTHING
                    """
                ),
                {"currency": currency, "rate": rate, "valid_on": valid_on},
            )


def monthly_report_explain_sql() -> str:
    """
    SQL template for short-answer EXPLAIN on the monthly report.

    Paste into psql after substituting :uid / :year / :month, e.g.:

      EXPLAIN (ANALYZE, BUFFERS)
      <body of app/repositories/reports.py _MONTHLY_REPORT_SQL>
      -- with uid = '...', year = 2026, month = 3
    """
    from app.repositories.reports import _MONTHLY_REPORT_SQL

    return (
        "EXPLAIN (ANALYZE, BUFFERS)\n"
        + _MONTHLY_REPORT_SQL.strip()
        + "\n-- Bind: :uid (user UUID), :year, :month\n"
        f"-- Source: {ROOT / 'app' / 'repositories' / 'reports.py'}\n"
    )


async def register_and_login(
    client: AsyncClient,
    *,
    email: str = "user@example.com",
    password: str = "password123",
    base_currency: str = "USD",
) -> dict:
    reg = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "base_currency": base_currency},
    )
    assert reg.status_code == 201, reg.text
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login.status_code == 200, login.text
    body = login.json()
    return {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
        "user": reg.json(),
    }


async def create_category(
    client: AsyncClient,
    headers: dict,
    *,
    name: str = "Food",
    monthly_budget: str | float | None = "100.00",
) -> dict:
    payload: dict = {"name": name}
    if monthly_budget is not None:
        payload["monthly_budget"] = monthly_budget
    resp = await client.post("/api/v1/categories", headers=headers, json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()
