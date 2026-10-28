import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.asyncio
async def test_expenses_indexes_exist(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = 'public' AND tablename = 'expenses'
                ORDER BY indexname
                """
            )
        )
        names = {row[0] for row in result}
    assert "ix_expenses_user_spent_on_id" in names
    assert "ix_expenses_user_category_spent_on" in names
    assert "ix_expenses_description_trgm" in names
    assert "uq_expenses_user_dedupe_hash" in names


@pytest.mark.asyncio
async def test_rates_fixture_inserts_gap_day(engine: AsyncEngine, rates: dict) -> None:
    from datetime import date

    async with engine.connect() as conn:
        eur_on_gap = await conn.scalar(
            text(
                """
                SELECT rate_to_usd FROM exchange_rates
                WHERE currency = 'EUR' AND valid_on = :d
                """
            ),
            {"d": rates["gap_day"]},
        )
        eur_before = await conn.scalar(
            text(
                """
                SELECT rate_to_usd FROM exchange_rates
                WHERE currency = 'EUR' AND valid_on = :d
                """
            ),
            {"d": date(2026, 3, 2)},
        )
    assert eur_on_gap is None
    assert eur_before == rates["eur_before_gap"]
