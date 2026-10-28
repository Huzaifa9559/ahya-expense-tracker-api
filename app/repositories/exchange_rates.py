from datetime import date

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExchangeRate


async def currency_exists(session: AsyncSession, currency: str) -> bool:
    result = await session.execute(
        select(ExchangeRate.currency).where(ExchangeRate.currency == currency).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def has_as_of_rate(
    session: AsyncSession, *, currency: str, on_or_before: date
) -> bool:
    """True if any rate exists with valid_on <= on_or_before for that currency."""
    result = await session.execute(
        text(
            """
            SELECT 1
            FROM exchange_rates
            WHERE currency = :currency AND valid_on <= :on_or_before
            LIMIT 1
            """
        ),
        {"currency": currency, "on_or_before": on_or_before},
    )
    return result.first() is not None
