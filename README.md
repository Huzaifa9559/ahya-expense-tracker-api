# Ahya Expense Tracker API

Async FastAPI backend for multi-currency expense tracking (48-hour assessment).

## Prerequisites

- Python 3.11–3.13 (`requires-python = ">=3.11,<3.14"`)
- Docker / Docker Compose

## Setup

```bash
cp .env.example .env
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Start Postgres, Redis, LocalStack S3, API, and arq worker
docker compose up -d --build

# Apply migrations (schema + exchange-rate seed)
alembic upgrade head

# Test DB is created automatically on first Postgres init via
# scripts/init-databases.sql. If you need to recreate it later:
#   ./scripts/create_test_db.sh
# or:
#   docker compose exec postgres psql -U ahya -d postgres -c "CREATE DATABASE expense_tracker_test;"
```

S3 in compose is **LocalStack 4.0.3** (service name `s3`, host port 9000 → 4566), matching the PDF’s MinIO/LocalStack option. Tests mock S3 with moto.

## Health check

```bash
curl http://localhost:8000/health
```

## Tests

Tests use real Postgres (`expense_tracker_test`) and Redis DB 15 (`TEST_REDIS_URL`).
`conftest` runs `alembic upgrade head` against the test DB once per session. To rebuild the test schema by hand after editing `0001_initial` (prefix the env for **both** commands — `VAR=x cmd1 && cmd2` only applies `VAR` to `cmd1`):

```bash
export ALEMBIC_DATABASE_URL=postgresql+asyncpg://ahya:ahya@localhost:5432/expense_tracker_test
alembic downgrade base && alembic upgrade head
unset ALEMBIC_DATABASE_URL

pytest
```

**Coverage note (Phase 0):** `--cov-fail-under` is temporarily `0` in `pyproject.toml` because only the scaffold exists. **Restore to `85` in Phase 10** per PLAN.md §4.

## Auth notes

- Access JWT: HS256, 15 minutes. Refresh: opaque `secrets.token_urlsafe(32)`; only sha256 is stored.
- Login rate limit: Redis key `login:fail:{email}`; check before argon2; Lua `INCR`+`EXPIRE 900` on failure; `DEL` on success.
- **Redis down → fail open** (log a warning, allow the attempt). Cost: an attacker who can take Redis down gets unlimited password guesses. Mitigation we’d add next: an in-process per-instance fallback limiter (weaker across servers, but never unlimited).
- **Concurrent refresh / revoke-all** is a known trade-off, not a feature: the loser’s reuse path revokes the winner’s new token too, so a mobile client that retries refresh after a network drop can get logged out. Production fix: a short grace window (~10s) — if the old token is reused and its successor hasn’t been used yet, return that same successor instead of revoking everything.

## Phase status

- Phase 0: scaffold, docker-compose, config, error envelope, `/health` — done
- Phase 1: models, Alembic, seed rates, test DB harness — done
- Phase 2: register/login/refresh/logout, rate limit, `get_current_user` — done
- Phase 3: categories + expenses CRUD (no PATCH), filters, keyset cursor, category spend — done
- Phase 4: `GET /reports/monthly` (one SQL, LAG MoM) — done
- Phase 5: `Idempotency-Key` on `POST /expenses` + cleanup cron — done
- Phases 6–10: see [PLAN.md](PLAN.md)

## Idempotency notes

- Optional `Idempotency-Key` header (1..255 chars), scoped per user.
- Race control: `INSERT … ON CONFLICT DO NOTHING RETURNING id` waits for any in-flight txn with the same key; losers `SELECT` and replay (header `Idempotent-Replayed: true`) or 422 `IDEMPOTENCY_KEY_REUSED` if the body hash differs.
- **Waiting holds a pool connection.** A burst of retries behind one slow winner can exhaust the pool. Improvement: `SET LOCAL lock_timeout = '2s'` on that INSERT and map timeout → **409 Request in progress, retry**.
- **Replay returns the original 201 body**, not the live expense. If the expense was edited after creation, replaying the key still answers “what did my first request do?”, not “what is it now?” — correct idempotency semantics.
- **Failed creates are not stored.** A 404/422 rolls back the key row with the rest of the transaction, so the client can fix the input and retry the same key. Cost: a buggy client can hammer validation errors with one key (login rate limits only).

## Expenses notes

- `min_amount` / `max_amount` on `GET /expenses` compare the expense's **own** `amount` (stored currency), not a converted base-currency value.
- Money in JSON responses is always a string (e.g. `"12.50"`), never a float.

## Monthly report notes (interview)

- **Round once at the end, not per row.** Postgres `NUMERIC` arithmetic is exact decimal math — there are no floats in the report SQL. Intermediate `NUMERIC` values keep many fractional digits (division can produce 16+ places of scale). Rounding each converted row to 2 decimals would accumulate error across thousands of expenses; `SUM` then `ROUND(..., 2)` once avoids that.
- **Money stays `Decimal` / `NUMERIC` end-to-end** (never Python `float` / JSON number for amounts).

## Monthly report EXPLAIN (short answer 2)

After seeding a user + expenses, run the statement in `scripts/explain_monthly_report.sql` (replace the three UUID placeholders) via:

```bash
# If DATABASE_URL uses asyncpg, switch the scheme for psql:
psql "postgresql://ahya:ahya@localhost:5432/expense_tracker" \
  -f scripts/explain_monthly_report.sql
```

Or from Python: `from tests.helpers import monthly_report_explain_sql; print(monthly_report_explain_sql())`.

On a handful of rows Postgres will often **Seq Scan** even when indexes exist — scanning a tiny heap is cheaper than an index lookup. That does **not** mean the indexes are wrong. For a demo that shows the index plan you’d describe at 20M rows, either insert ~100k expenses for one user, or in that psql session only:

```sql
SET enable_seqscan = off;
-- then re-run EXPLAIN (ANALYZE, BUFFERS) …
```

Look for Index / Bitmap scans on `expenses (user_id, spent_on, …)` and the `exchange_rates` PK `(currency, valid_on)`. At large scale, a Seq Scan on `expenses` is the smell offset pagination would also hit.
