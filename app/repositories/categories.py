import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category


async def get_by_id_for_user(
    session: AsyncSession, *, category_id: uuid.UUID, user_id: uuid.UUID
) -> Category | None:
    result = await session.execute(
        select(Category).where(Category.id == category_id, Category.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def create(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    name: str,
    monthly_budget: Decimal | None,
) -> Category:
    row = Category(user_id=user_id, name=name, monthly_budget=monthly_budget)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def list_with_month_spend(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    month_start: date,
    month_end: date,
    base_currency: str,
) -> list[dict]:
    """
    One query: categories LEFT JOIN this-month expenses with LATERAL as-of rates.
    amount_in_base = amount * src.rate_to_usd / base.rate_to_usd; round only at SUM.
    """
    result = await session.execute(
        text(
            """
            SELECT
                c.id,
                c.name,
                c.monthly_budget,
                c.created_at,
                COALESCE(
                    ROUND(
                        SUM(
                            e.amount * src.rate_to_usd / NULLIF(base.rate_to_usd, 0)
                        ),
                        2
                    ),
                    0
                ) AS spend
            FROM categories c
            LEFT JOIN expenses e
                ON e.category_id = c.id
               AND e.user_id = c.user_id
               AND e.spent_on >= :month_start
               AND e.spent_on < :month_end
            LEFT JOIN LATERAL (
                SELECT er.rate_to_usd
                FROM exchange_rates er
                WHERE er.currency = e.currency
                  AND er.valid_on <= e.spent_on
                ORDER BY er.valid_on DESC
                LIMIT 1
            ) src ON e.id IS NOT NULL
            LEFT JOIN LATERAL (
                SELECT er.rate_to_usd
                FROM exchange_rates er
                WHERE er.currency = :base_currency
                  AND er.valid_on <= e.spent_on
                ORDER BY er.valid_on DESC
                LIMIT 1
            ) base ON e.id IS NOT NULL
            WHERE c.user_id = :user_id
            GROUP BY c.id, c.name, c.monthly_budget, c.created_at
            ORDER BY c.name ASC, c.id ASC
            """
        ),
        {
            "user_id": user_id,
            "month_start": month_start,
            "month_end": month_end,
            "base_currency": base_currency,
        },
    )
    return [dict(row._mapping) for row in result]
