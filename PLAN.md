# Ahya Expense Tracker API — Implementation Plan

Full-scope intent: all PDF parts 1–4 and hard features 2.1–2.5. The README **skipped** section lists whatever is unfinished at submission time (do not pre-assert "none"). Risks below are watch-outs and mitigations, not a cut list.

Stack: Python 3.11+, FastAPI, async SQLAlchemy 2.0 / asyncpg, Pydantic v2, Alembic, PostgreSQL, Redis (`redis.asyncio`), arq, aioboto3. Money is `Decimal` / `NUMERIC(14,2)`. One error envelope. Layering is `api/` → `services/` → `repositories/`. All S3 code lives in `app/storage/s3.py`. Ownership queries always filter `user_id` and return 404, never 403. Config includes `S3_PUBLIC_ENDPOINT` used only for presigning (MinIO host differs inside vs outside Docker).

`rate_to_usd` means **1 unit of that currency = X USD**. Conversion: `amount_in_base = amount * rate_expense_to_usd / rate_base_to_usd`. USD is always `1`.

On expense create/update and each CSV row: require a rate with `valid_on <= spent_on` for **both** the expense currency and the user's `base_currency`; otherwise **422**. Reject `spent_on` in the future (**422**). Register still requires `base_currency` to appear in `exchange_rates`.

---

## 1. Project structure

```
ahya-expense-tracker-api/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app, CORS, router include, lifespan
│   ├── config.py               # pydantic-settings; includes S3_PUBLIC_ENDPOINT for presign only
│   ├── db.py                   # async engine, session factory
│   ├── redis.py                # redis.asyncio pool
│   ├── errors.py               # envelope + handlers (HTTP, validation, domain)
│   ├── deps.py                 # get_db, get_redis, get_current_user
│   ├── api/v1/
│   │   ├── router.py
│   │   ├── auth.py
│   │   ├── categories.py
│   │   ├── expenses.py         # CRUD + import + receipts
│   │   ├── reports.py
│   │   ├── notifications.py
│   │   └── imports.py          # GET /imports/{job_id}
│   ├── models/                 # SQLAlchemy 2.0 mapped classes
│   ├── schemas/                # Pydantic v2 request/response
│   ├── repositories/           # SQL only
│   ├── services/               # business logic
│   ├── workers/
│   │   ├── settings.py         # arq WorkerSettings + cron (outbox, import, s3, expired keys)
│   │   ├── outbox.py
│   │   ├── import_csv.py
│   │   ├── s3_cleanup.py
│   │   └── cleanup.py          # expired idempotency_keys and refresh_tokens
│   ├── storage/s3.py           # the only S3 module
│   └── security/
│       ├── passwords.py        # argon2 via asyncio.to_thread
│       ├── tokens.py           # JWT access + hashed refresh
│       └── rate_limit.py       # Redis Lua INCR+EXPIRE
├── alembic/env.py
├── alembic/versions/0001_initial.py
├── tests/
│   ├── conftest.py             # engine pool_size>=5, TRUNCATE+FLUSHDB, AsyncClient, moto
│   ├── test_health.py
│   ├── test_auth.py
│   ├── test_categories.py
│   ├── test_expenses.py
│   ├── test_reports.py
│   ├── test_idempotency.py
│   ├── test_etag.py
│   ├── test_budgets.py
│   ├── test_imports.py
│   └── test_receipts.py
├── postman/Ahya_Expense_Tracker.postman_collection.json
├── docker-compose.yml          # postgres, redis, minio, api, worker
├── Dockerfile
├── alembic.ini
├── pyproject.toml
├── .env.example
├── .gitignore
├── README.md
└── PLAN.md
```

`docker-compose.yml` services: `postgres` (5432), `redis` (6379), `s3` (LocalStack `4.0.3` on host 9000 → 4566, bucket `ahya-receipts`), `api` (uvicorn), `worker` (arq). Tests use compose Postgres/Redis only, a separate DB `expense_tracker_test`, and moto for S3.

---

## 2. Full DB schema

All PK `id` values are `UUID` default `gen_random_uuid()` unless noted. Timestamps are `TIMESTAMPTZ`. Money is `NUMERIC(14,2)`. Currencies are `CHAR(3)`.

### users
- `id UUID PK`
- `email VARCHAR(255) NOT NULL` stored lowercased
- `password_hash VARCHAR(255) NOT NULL`
- `base_currency CHAR(3) NOT NULL`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- UNIQUE `email` — login lookup and duplicate register (409)
- CHECK `char_length(base_currency) = 3`

### categories
- `id UUID PK`
- `user_id UUID NOT NULL FK users ON DELETE CASCADE`
- `name VARCHAR(100) NOT NULL`
- `monthly_budget NUMERIC(14,2) NULL`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- UNIQUE `(user_id, name)` — per-user uniqueness (409) and prefix for "my categories"
- CHECK `monthly_budget IS NULL OR monthly_budget >= 0`

### expenses
- `id UUID PK`
- `user_id UUID NOT NULL FK users ON DELETE CASCADE`
- `category_id UUID NOT NULL FK categories ON DELETE RESTRICT`
- `amount NUMERIC(14,2) NOT NULL`
- `currency CHAR(3) NOT NULL`
- `description VARCHAR(500) NULL`
- `spent_on DATE NOT NULL`
- `receipt_key VARCHAR(512) NULL`
- `version INTEGER NOT NULL DEFAULT 1`
- `dedupe_hash CHAR(64) NULL` — CSV only: `sha256(file_hash || ':' || row_number)`; NULL for manual creates (Postgres UNIQUE allows many NULLs)
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- CHECK `amount > 0`
- UNIQUE `(user_id, dedupe_hash)` WHERE `dedupe_hash IS NOT NULL` — worker-retry idempotency (not content merge)
- INDEX `(user_id, spent_on DESC, id DESC)` — cursor pagination and date-range listing (keyset)
- INDEX `(user_id, category_id, spent_on)` — monthly report / budget recompute / category filter
- GIN `expenses.description gin_trgm_ops` — `q` description search (requires `CREATE EXTENSION pg_trgm`)
- No `INDEX (user_id, currency)` — currency filter rides the pagination index + residual filter

Same-user category ownership is enforced in the service (FK is global on `categories.id`). At write time: `spent_on` not in the future, and a rate with `valid_on <= spent_on` must exist for both expense currency and user `base_currency` → else 422.

### exchange_rates
- `currency CHAR(3) NOT NULL`
- `rate_to_usd NUMERIC(18,8) NOT NULL` — 1 unit = X USD
- `valid_on DATE NOT NULL`
- PK `(currency, valid_on)` — one row per currency per day; as-of lookup by equality
- INDEX `(currency, valid_on DESC)` — LATERAL "latest rate where valid_on <= spent_on"
- CHECK `rate_to_usd > 0`

Seed (dev/prod migration): pure function `generate_seed_rates(today) -> list[row]` — PKR, AED, USD, EUR over the last 90 calendar days, **skip weekends**, slight per-day weekday variation (USD always `1`). Migration calls that function. Unit-test the generator with no DB (4 currencies, no weekend rows, USD == 1).

**Tests do not rely on migration seed data for assertions.** Autouse TRUNCATE includes `exchange_rates`. A `rates` fixture inserts small, **deterministic** rates (fixed values + a known gap day) so report tests can assert exact numbers.

### refresh_tokens
- `id UUID PK`
- `user_id UUID NOT NULL FK users ON DELETE CASCADE`
- `jti UUID NOT NULL`
- `token_hash CHAR(64) NOT NULL` — sha256 of the raw refresh token
- `family_id UUID NOT NULL` — one family per login
- `revoked_at TIMESTAMPTZ NULL`
- `replaced_by UUID NULL FK refresh_tokens.id`
- `expires_at TIMESTAMPTZ NOT NULL`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- UNIQUE `jti` — decode JWT then load row
- UNIQUE `token_hash` — lookup presented token without storing plaintext
- INDEX `(user_id)` — reuse detection revokes every token for that user
- INDEX `(expires_at)` — periodic cleanup of expired rows

### idempotency_keys
- `id UUID PK`
- `user_id UUID NOT NULL FK users ON DELETE CASCADE`
- `key VARCHAR(255) NOT NULL`
- `request_hash CHAR(64) NOT NULL` — sha256 of `model_dump(mode="json")` with sorted keys
- `response_status INTEGER NULL` — filled before commit; NULL only while the winning txn is open
- `response_body JSONB NULL`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `expires_at TIMESTAMPTZ NOT NULL` — `now() + 24h`
- UNIQUE `(user_id, key)` — concurrent duplicate blocks here, then reads the stored response
- INDEX `(expires_at)` — periodic delete of expired keys

### outbox_events
- `id UUID PK`
- `event_type VARCHAR(64) NOT NULL` — `expense.created|updated|deleted`
- `payload JSONB NOT NULL` — `{user_id, category_id, expense_id, spent_on}`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `processed_at TIMESTAMPTZ NULL`
- INDEX `(created_at) WHERE processed_at IS NULL` — worker poll of unprocessed rows

### notifications
- `id UUID PK`
- `user_id UUID NOT NULL FK users ON DELETE CASCADE`
- `category_id UUID NOT NULL FK categories ON DELETE CASCADE`
- `period_month DATE NOT NULL` — first day of the month
- `level SMALLINT NOT NULL` — `80` or `100`
- `message TEXT NOT NULL`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- UNIQUE `(user_id, category_id, period_month, level)` — one alert per level per category per month; worker uses `ON CONFLICT DO NOTHING`
- INDEX `(user_id, created_at DESC)` — `GET /notifications`
- CHECK `level IN (80, 100)`

### import_jobs
- `id UUID PK`
- `user_id UUID NOT NULL FK users ON DELETE CASCADE`
- `file_hash CHAR(64) NOT NULL` — sha256 of the uploaded bytes
- `filename VARCHAR(255) NOT NULL`
- `storage_key VARCHAR(512) NOT NULL` — S3/MinIO key `imports/{user_id}/{job_id}.csv` (not local disk)
- `status VARCHAR(20) NOT NULL` — `pending|processing|completed|failed`
- `total_rows INTEGER NOT NULL DEFAULT 0`
- `processed_rows INTEGER NOT NULL DEFAULT 0`
- `failed_rows INTEGER NOT NULL DEFAULT 0`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `started_at TIMESTAMPTZ NULL`
- `finished_at TIMESTAMPTZ NULL`
- UNIQUE `(user_id, file_hash)` — same file twice returns the existing job, no new expenses
- INDEX `(user_id, created_at DESC)` — ownership lookup for `GET /imports/{id}`

### import_errors
- `id BIGSERIAL PK`
- `job_id UUID NOT NULL FK import_jobs ON DELETE CASCADE`
- `row_number INTEGER NOT NULL`
- `reason TEXT NOT NULL`
- INDEX `(job_id, row_number)` — status payload for a job

### s3_deletion_queue
- `id UUID PK`
- `s3_key VARCHAR(512) NOT NULL`
- `attempts INTEGER NOT NULL DEFAULT 0`
- `last_error TEXT NULL`
- `next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `deleted_at TIMESTAMPTZ NULL`
- INDEX `(next_attempt_at) WHERE deleted_at IS NULL` — worker retries until S3 delete succeeds

---

## 3. Hard-feature decisions

No change to the prescribed approaches. Notes below are implementation detail, not redesign.

### Refresh tokens
Store sha256 of the raw token plus `jti`, `family_id`, `revoked_at`, `replaced_by`. Access JWT 15 min; refresh 7 days. Rotation is atomic:

```sql
UPDATE refresh_tokens
SET revoked_at = now(), replaced_by = :new_id
WHERE id = :id AND revoked_at IS NULL
RETURNING id;
```

Zero rows = reuse (or already revoked) → `UPDATE refresh_tokens SET revoked_at = now() WHERE user_id = :uid AND revoked_at IS NULL` and reject. **Reusing a logged-out token also triggers revoke-all** (deliberate: logout sets `revoked_at`, so a later present is treated as theft). Concurrent refresh with the same token: exactly one `RETURNING` succeeds; the loser's 401 path is revoke-all, so the **winner's newly issued refresh token is subsequently rejected** (reuse detection). `family_id` is session grouping only.

### Login rate limit
Redis key `login:fail:{email.lower()}`. **Check the counter BEFORE verifying the password.** If count `>= 5`, return 429 + `Retry-After` from TTL immediately — do not run argon2. On a real attempt: verify password; **increment only on failure** (atomic Lua `INCR` + `EXPIRE 900` when count becomes 1); **delete the key on success**. The 6th failed attempt (or any attempt while count ≥ 5) yields 429. Redis-backed for multi-server safety. If Redis is down, fail-open on login (documented in README). Check-then-increment is not perfectly atomic across concurrent requests (see Risks).

### Cursor pagination
Opaque cursor = urlsafe base64 of `{spent_on, id}`. Query: `WHERE user_id = :uid AND (spent_on, id) < (:spent_on, :id) ORDER BY spent_on DESC, id DESC LIMIT n+1`. Extra row means `next_cursor` is set from item n. Filters AND into the same WHERE. Keyset stays stable if rows are inserted between pages (offset would not). Index `(user_id, spent_on DESC, id DESC)`.

### Monthly report (one SQL)
`rate_to_usd`: 1 unit = X USD. `amount_in_base = amount * src.rate_to_usd / base.rate_to_usd`. **Round to 2 decimals only at the final aggregate**, not per row.

One statement: CTE `bounds` → build a **categories × {prev_month, current_month}** grid (`CROSS JOIN` of the user's categories with the two month starts) → `LEFT JOIN` converted expense totals (expenses in `[prev_start, next_start)` with two `LEFT JOIN LATERAL` rate lookups, `GROUP BY category_id, month`) → `COALESCE(total, 0)` → `LAG(total) OVER (PARTITION BY category_id ORDER BY month)` for change. Response rows are the current-month slice: total, budget, percent_used (null if no budget), change_from_last_month (can be **negative** when only last month had spend). Missing rate **day** (e.g. weekend gap) uses the latest earlier row via LATERAL. **Persisted expenses never lack an as-of rate:** create/update/CSV reject unless a rate with `valid_on <= spent_on` exists for both the expense currency and the user's `base_currency`, and reject future `spent_on` (422).

### Currency / rate validation
- Register: `base_currency` must appear in `exchange_rates` → else 422.
- Expense create/update and each CSV row: `spent_on` must not be in the future → else 422; and `EXISTS (SELECT 1 FROM exchange_rates WHERE currency = :expense_currency AND valid_on <= :spent_on)` **and** the same for the user's `base_currency` → else 422.
- Weekend gaps are fine (LATERAL picks the prior weekday). A date before the earliest seeded rate for either currency is rejected (covered by `test_expense_before_earliest_rate_rejected`).

### Idempotency
Header `Idempotency-Key` on `POST /expenses`. Hash = sha256 of canonical JSON from the **validated** body: `json.dumps(model_dump(mode="json"), sort_keys=True, separators=(",", ":"))`. Insert key row **and** the expense in the **same** transaction, then set response status + body before commit. On `UniqueViolation`: **roll back the session**, then `SELECT` the stored row — same hash → replay stored status and body; different hash → 422. Do **not** implement a "status NULL → 409" branch (unreachable after the winner commits with status filled; any defensive check belongs only as a commented unreachable guard). Expired (`expires_at < now()`): delete and treat as new. arq cron cleans expired keys. Unique index is the race control; no Redis lock.

### Optimistic locking
GET sets `ETag: "{version}"` (quoted). PATCH requires `If-Match`; missing → 428. Single statement: `UPDATE expenses SET ..., version = version + 1, updated_at = now() WHERE id = :id AND user_id = :uid AND version = :v RETURNING *`. No row: existence check → 404 vs 412. If `category_id` or `spent_on` changes, write **outbox events for both the old and new (category_id, month)** in the same transaction as the update (so budget recompute covers both sides).

### Budget alerts
Every expense create/update/delete (and CSV batch-affected pairs) writes `outbox_events` in the same transaction as the data change. Worker claim + notify + mark done is **ONE transaction**: `SELECT ... FOR UPDATE SKIP LOCKED` → recompute month total in base currency → `INSERT notifications ... ON CONFLICT DO NOTHING` → set `processed_at` → commit. Replays and multiple workers cannot duplicate alerts. **Edits or deletes that lower the total do not retract alerts already sent.** New crossings still insert (80 then 100 → two rows). Categories with no budget: worker no-ops.

### CSV import
Order of operations:

1. Generate `job_id` in Python (`uuid4`).
2. `upload_fileobj` from `UploadFile` (already disk-spooled by FastAPI) to `imports/{user_id}/{job_id}.csv` via `app/storage/s3.py`, hashing sha256 while streaming — never load the whole file into memory.
3. `INSERT import_jobs` with `storage_key`, `file_hash`, status `pending`.
4. On `UNIQUE(user_id, file_hash)` conflict: **delete the just-uploaded object**, return the existing `job_id` with **202**.
5. Else enqueue arq and return 202 with the new `job_id`.

Worker **streams** the object back, reads line by line, validates (including as-of rate + no future `spent_on`), resolves category names via a cached dict, inserts batches of 1000 with `ON CONFLICT (user_id, dedupe_hash) DO NOTHING`. `dedupe_hash = sha256(file_hash || ':' || row_number)` — file-level unique stops re-uploads; row hash makes worker retries idempotent **without** merging identical legitimate rows (two identical content rows both import). After each batch, write **one outbox event per affected `(category_id, month)`**. Record `import_errors` with row numbers. `GET /imports/{job_id}` returns status, counts, and errors.

### Receipts
Presigned **POST** (not PUT) with `content-length-range` 1..5_242_880; key `receipts/{user_id}/{expense_id}/{uuid}.{ext}`. Presign URLs use **`S3_PUBLIC_ENDPOINT`** only (browser/host-reachable LocalStack URL); internal ops use the cluster endpoint. LocalStack must enforce POST policy conditions for manual/Postman demos (moto covers unit tests). Confirm: assert key prefix `receipts/{user_id}/{expense_id}/`, `head_object`, ranged GET magic bytes (JPG `FF D8 FF`, PNG `89 50 4E 47 0D 0A 1A 0A`, PDF `25 50 44 46`). On magic mismatch: **delete the object** and return 422. On success: if replacing, enqueue the **old** `receipt_key` into `s3_deletion_queue`, then save the new key. Download: presigned GET, 15 minutes. Expense delete enqueues `receipt_key` in the same transaction; worker retries until success. All S3 code only in `app/storage/s3.py`. Tests: `ThreadedMotoServer`.

---

## 4. Test strategy

- docker-compose Postgres + Redis; `TEST_DATABASE_URL` points at `expense_tracker_test`
- Session-scoped async engine with **`pool_size >= 5`** so `asyncio.gather` tests run truly concurrently
- `asyncio_default_fixture_loop_scope = "session"` in pytest config
- Session setup: **`alembic upgrade head` once** (not `metadata.create_all`) so migrations are exercised
- Autouse fixture: **TRUNCATE all tables CASCADE** (including `exchange_rates`) and **`FLUSHDB` the test Redis DB** between tests (not a wrapping transaction)
- `rates` fixture: insert fixed deterministic exchange rates (+ known gap day); required by every expense write
- pytest-asyncio auto mode; httpx `AsyncClient` + `ASGITransport`
- moto `ThreadedMotoServer` for S3; a `.exe` renamed to `.png` must fail confirm (object deleted)
- Error envelope: unknown route → 404 envelope; malformed body → 422 envelope with `details`
- `[tool.pytest.ini_options] addopts = "--cov=app --cov-report=term-missing --cov-fail-under=85"` so bare `pytest` prints coverage

Mandatory (PDF), exact names:

1. `test_refresh_token_reuse_revokes_all_tokens`
2. `test_sixth_failed_login_returns_429`
3. `test_other_users_expense_returns_404`
4. `test_amount_zero_rejected`
5. `test_amount_negative_rejected`
6. `test_amount_more_than_two_decimals_rejected`
7. `test_cursor_pagination_stable_when_rows_inserted_between_pages`
8. `test_monthly_report_mixed_currencies_and_missing_rate_day`
9. `test_parallel_posts_same_idempotency_key_create_one_expense`
10. `test_parallel_patches_same_etag_one_200_one_412`
11. `test_duplicate_budget_alert_event_creates_one_notification`
12. `test_csv_import_good_and_bad_rows_counts`

Additional required tests:

13. `test_rate_limited_correct_password_still_returns_429`
14. `test_concurrent_refresh_same_token_exactly_one_success` — assert **one 200, one 401**, and the **winner's new refresh token is subsequently rejected** (loser's reuse detection revoked all of the user's tokens)
15. `test_identical_legitimate_csv_rows_both_import`
16. `test_monthly_report_negative_change_when_spend_only_last_month`
17. `test_expense_before_earliest_rate_rejected`

Other tests (target ≥35, coverage ≥85%):

- health: `test_health_ok`
- auth: register 201, duplicate email 409, invalid base_currency 422, login 200, wrong password 401, refresh rotates, logout 204, missing token 401, `Retry-After` present on 429
- categories: create, duplicate name 409, list includes spend vs budget, other user's category 404
- expenses: create, get, delete 204, unknown currency / no as-of rate 422, future `spent_on` 422, filters (`category_id`, dates, amounts, `currency`, `q`), GET returns ETag
- reports: percent_used and month-over-month change
- idempotency: same key+body replays, same key different body 422, expired key treated as new
- etag: missing If-Match 428, stale ETag 412
- budgets: no alert under 80%, lowering total does not retract, GET /notifications
- import: same file_hash does not duplicate expenses (orphan upload deleted), GET /imports/{id} status
- receipts: presigned POST fields, confirm rejects exe-as-png and deletes object, confirm accepts real PNG magic, replace enqueues old key, download URL, delete enqueues s3 key

---

## 5. Phased order (app stays runnable; tests pass each phase)

- **Phase 0** — scaffold, docker-compose (`s3` = LocalStack), config, error envelope, `GET /health`. Tests: health + envelope 404/422.
- **Phase 1** — models, Alembic `0001_initial` (`pg_trgm`, all tables/indexes), `generate_seed_rates(today)` pure function + migration seed, conftest `alembic upgrade head` + deterministic `rates` fixture. Tests: generator unit test; migration applied.
- **Phase 2** — register/login/refresh/logout (atomic rotate + revoke-all), Redis rate limit (check before argon2). Tests: auth + 429 + reuse + concurrent refresh (200+401+winner reuse) + rate-limited correct password.
- **Phase 3** — categories + expenses CRUD, as-of rate + future `spent_on` validation 422, filters (incl. trigram `q`), cursor pagination, 404 ownership, GET /categories spend vs budget. Tests: expenses + categories + pagination + `test_expense_before_earliest_rate_rejected`.
- **Phase 4** — monthly report SQL (categories × months grid, COALESCE, LAG, round at aggregate). Tests: mixed currencies + missing rate day + negative change.
- **Phase 5** — idempotency (`model_dump` hash, rollback-on-UniqueViolation, expired cleanup cron). Tests: replay, 422, parallel gather, expired key.
- **Phase 6** — ETag / If-Match; PATCH bumps `updated_at` and dual outbox on category/spent_on change. Tests: 428, 412, parallel PATCH.
- **Phase 7** — outbox worker (claim + notify + `processed_at` in one txn) + GET /notifications. Tests: duplicate event, no retract, list, under 80%.
- **Phase 8** — CSV: `job_id` → `upload_fileobj`+sha256 → INSERT; conflict deletes object and returns existing job; worker stream + row dedupe + batch outbox; GET /imports/{job_id}. Tests: mixed rows, same-file dedupe, identical rows both import, job status.
- **Phase 9** — S3 receipts: `S3_PUBLIC_ENDPOINT` presign POST, prefix + magic confirm (delete on mismatch), replace enqueues old key, deletion queue worker. Tests: moto.
- **Phase 10** — Postman for every endpoint, README (setup, coverage, AI-usage, **skipped = whatever unfinished at submission**, AWS notes, five short answers).

---

## 6. Risks (watch-outs, not a cut list)

Aim to finish everything. README **skipped** lists only what remains unfinished at submission — never invent "none" in advance.

Where a 48-hour attempt is most likely to break, and how we still finish it:

- **Monthly report grid + LAG** — categories × months CROSS JOIN first; round only at final aggregate; lock `amount * src / base`.
- **`asyncio.gather` races** — TRUNCATE + Redis FLUSHDB; `pool_size >= 5`; session-scoped event loop; never a wrapping transaction.
- **Concurrent refresh** — atomic `UPDATE ... RETURNING`; assert one 200, one 401, then winner's new refresh also rejected after revoke-all.
- **Rate limit check-then-increment** — under concurrent login attempts, the pre-check can allow **one or two extra attempts** past five before all see `count >= 5`. Accepted trade-off (simple, no Redis lock); candidate for **README short answer 5** (simple over perfect).
- **Alembic** — `pg_trgm` in the same migration as the GIN index; async `env.py` via `run_sync`.
- **moto + aioboto3 + S3_PUBLIC_ENDPOINT** — `ThreadedMotoServer`; separate public endpoint for presign.
- **CSV on S3** — `job_id` first, then upload while hashing, then INSERT; conflict path must delete the orphan object.
- **Worker coverage** — call outbox/import/s3/cleanup jobs directly in tests.
- **Idempotency UniqueViolation** — always roll back, then SELECT; no 409-on-NULL-status path.

README short answers: why presigned POST; 20M-row `EXPLAIN (ANALYZE, BUFFERS)` + composite index + why offset dies + N+1; 3-server LB; 500k outbox drain with `SKIP LOCKED` batching; one simple-over-perfect choice (**check-then-increment rate limit allowing 1–2 extra concurrent attempts**, fail-open login if Redis down, or no retraction of budget alerts).
