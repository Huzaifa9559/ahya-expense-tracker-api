import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def category_month_spend_in_base(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    category_id: uuid.UUID,
    month_start: date,
    month_end: date,
) -> tuple[Decimal, Decimal | None, str] | None:
    """
    One query: category budget/name + this month's spend in the user's base currency
    (same LATERAL as-of conversion as the monthly report). Returns None if category missing.
    """
    result = await session.execute(
        text(
            """
            SELECT
                c.name,
                c.monthly_budget,
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
            JOIN users u ON u.id = c.user_id
            LEFT JOIN expenses e
                ON e.category_id = c.id
               AND e.user_id = c.user_id
               AND e.spent_on >= :month_start
               AND e.spent_on < :month_end
            LEFT JOIN LATERAL (
                SELECT r.rate_to_usd
                FROM exchange_rates r
                WHERE r.currency = e.currency
                  AND r.valid_on <= e.spent_on
                ORDER BY r.valid_on DESC
                LIMIT 1
            ) src ON e.id IS NOT NULL
            LEFT JOIN LATERAL (
                SELECT r.rate_to_usd
                FROM exchange_rates r
                WHERE r.currency = u.base_currency
                  AND r.valid_on <= e.spent_on
                ORDER BY r.valid_on DESC
                LIMIT 1
            ) base ON e.id IS NOT NULL
            WHERE c.id = :category_id
              AND c.user_id = :user_id
            GROUP BY c.id, c.name, c.monthly_budget
            """
        ),
        {
            "user_id": user_id,
            "category_id": category_id,
            "month_start": month_start,
            "month_end": month_end,
        },
    )
    row = result.mappings().first()
    if row is None:
        return None
    budget = row["monthly_budget"]
    return (
        Decimal(str(row["spend"] or 0)),
        Decimal(str(budget)) if budget is not None else None,
        str(row["name"]),
    )
