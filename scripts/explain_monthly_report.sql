-- Short answer 2 helper: run against a real user/month after seeding data.
-- Replace the three literals, then:
--   psql "$DATABASE_URL" -f scripts/explain_monthly_report.sql
--
-- Tiny tables often Seq Scan even with indexes (cheaper than an index probe).
-- For a demo that shows index plans: INSERT ~100k expenses for one user, or:
--   SET enable_seqscan = off;
-- Look for Index Scan / Bitmap on expenses (user_id, spent_on, …) and exchange_rates
-- PK (currency, valid_on); Seq Scan on expenses at 20M-row scale is the smell.

EXPLAIN (ANALYZE, BUFFERS)
-- Month window: current month start, previous month start, and exclusive next-month end.
WITH bounds AS (
    SELECT
        make_date(2026, 3, 1) AS cur_start,
        (make_date(2026, 3, 1) - interval '1 month')::date AS prev_start,
        (make_date(2026, 3, 1) + interval '1 month')::date AS next_start
),
u AS (
    SELECT base_currency
    FROM users
    WHERE id = '00000000-0000-0000-0000-000000000001'::uuid  -- <-- replace
),
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
    WHERE e.user_id = '00000000-0000-0000-0000-000000000001'::uuid  -- <-- replace
      AND e.spent_on >= b.prev_start
      AND e.spent_on < b.next_start
),
totals AS (
    SELECT category_id, month, SUM(amount_base) AS total
    FROM converted
    GROUP BY category_id, month
),
grid AS (
    SELECT
        c.id AS category_id,
        c.name,
        c.monthly_budget,
        m.month
    FROM categories c
    CROSS JOIN bounds b
    CROSS JOIN LATERAL (VALUES (b.prev_start), (b.cur_start)) AS m(month)
    WHERE c.user_id = '00000000-0000-0000-0000-000000000001'::uuid  -- <-- replace
),
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
ORDER BY total DESC, s.name ASC;
