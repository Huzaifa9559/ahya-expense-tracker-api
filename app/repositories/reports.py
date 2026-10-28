import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_MONTHLY_REPORT_SQL = """
-- Month window: current month start, previous month start, and exclusive next-month end.
WITH bounds AS (
    SELECT
        make_date(:year, :month, 1) AS cur_start,
        (make_date(:year, :month, 1) - interval '1 month')::date AS prev_start,
        (make_date(:year, :month, 1) + interval '1 month')::date AS next_start
),
-- Caller's base currency for conversion (one row).
u AS (
    SELECT base_currency
    FROM users
    WHERE id = :uid
),
-- Each expense in [prev_start, next_start) converted to base via as-of LATERAL rates (no per-row rounding).
converted AS (
    SELECT
        e.category_id,
        date_trunc('month', e.spent_on)::date AS month,
        e.amount * src.rate_to_usd / base.rate_to_usd AS amount_base
    FROM expenses e
    CROSS JOIN bounds b
    CROSS JOIN u
    JOIN LATERAL (
        SELECT r.rate_to_usd
        FROM exchange_rates r
        WHERE r.currency = e.currency
          AND r.valid_on <= e.spent_on
        ORDER BY r.valid_on DESC
        LIMIT 1
    ) src ON true
    JOIN LATERAL (
        SELECT r.rate_to_usd
        FROM exchange_rates r
        WHERE r.currency = u.base_currency
          AND r.valid_on <= e.spent_on
        ORDER BY r.valid_on DESC
        LIMIT 1
    ) base ON true
    WHERE e.user_id = :uid
      AND e.spent_on >= b.prev_start
      AND e.spent_on < b.next_start
),
-- Sum converted amounts per category per month (still unrounded).
totals AS (
    SELECT category_id, month, SUM(amount_base) AS total
    FROM converted
    GROUP BY category_id, month
),
-- Every category × {prev_start, cur_start} so LAG always has a previous row.
grid AS (
    SELECT
        c.id AS category_id,
        c.name,
        c.monthly_budget,
        m.month
    FROM categories c
    CROSS JOIN bounds b
    CROSS JOIN LATERAL (VALUES (b.prev_start), (b.cur_start)) AS m(month)
    WHERE c.user_id = :uid
),
-- Attach totals (0 if none) and previous-month total via LAG.
series AS (
    SELECT
        g.category_id,
        g.name,
        g.monthly_budget,
        g.month,
        COALESCE(t.total, 0) AS total,
        LAG(COALESCE(t.total, 0)) OVER (
            PARTITION BY g.category_id
            ORDER BY g.month
        ) AS prev_total
    FROM grid g
    LEFT JOIN totals t
        ON t.category_id = g.category_id
       AND t.month = g.month
)
-- Current-month slice only; round money and percent_used here (NULLIF avoids /0).
SELECT
    s.category_id,
    s.name,
    s.monthly_budget AS budget,
    ROUND(s.total, 2) AS total,
    ROUND(s.prev_total, 2) AS previous_total,
    ROUND(s.total - s.prev_total, 2) AS change,
    ROUND(s.total / NULLIF(s.monthly_budget, 0) * 100, 2) AS percent_used
FROM series s
CROSS JOIN bounds b
WHERE s.month = b.cur_start
ORDER BY total DESC, s.name ASC
"""


async def monthly_report(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    year: int,
    month: int,
) -> list[dict[str, Any]]:
    result = await session.execute(
        text(_MONTHLY_REPORT_SQL),
        {"uid": user_id, "year": year, "month": month},
    )
    return [dict(row._mapping) for row in result]
