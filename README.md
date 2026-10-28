# Ahya Expense Tracker API

I built this as a 48-hour assessment backend: multi-currency expenses with auth, keyset pagination, a one-SQL monthly report, Postgres idempotency, ETag PATCH, outbox budget alerts, CSV import via S3, and receipt uploads.

**Stack:** Python 3.11+, FastAPI, async SQLAlchemy 2.0 + asyncpg, Pydantic v2, Alembic, PostgreSQL 16, Redis (`redis.asyncio`), arq, aioboto3 (S3 only in `app/storage/s3.py`).

## Quick start

```bash
cp .env.example .env
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

docker compose up -d --build
# Wait until postgres/redis/s3 are healthy (compose healthchecks), then:
alembic upgrade head

# API: http://localhost:8000  |  worker: arq cron jobs
curl http://localhost:8000/health
```

Test DB is created on first Postgres init via `scripts/init-databases.sql`. Prefix **both** alembic commands — `VAR=x a && b` only applies `VAR` to `a`:

```bash
export ALEMBIC_DATABASE_URL=postgresql+asyncpg://ahya:ahya@localhost:5432/expense_tracker_test
alembic downgrade base && alembic upgrade head
unset ALEMBIC_DATABASE_URL

pytest
```

If `expense_tracker_test` is missing (e.g. you reused an old volume): `./scripts/create_test_db.sh`

S3 in compose is LocalStack 4.0.3 (`localhost:9000`). Pytest uses moto `ThreadedMotoServer`.

## Architecture

```
api/ (routers) → services/ (rules) → repositories/ (SQL)
workers/ (arq): cleanup_expired, process_outbox_batch, purge_processed_outbox,
                import_csv, process_s3_deletions
storage/s3.py  — the only S3 I/O
```

**Cron:** outbox every 5s; S3 deletions every 30s; idempotency/refresh cleanup hourly; processed-outbox purge daily.

Every money query filters by `user_id`. Other users' resources return **404**, never 403. Errors use one envelope: `{"error":{"code","message","details":[]}}`.

## Design decisions

### Auth
Opaque refresh (`secrets.token_urlsafe(32)`), store only sha256. Access JWT 15m. Rotation: `UPDATE … WHERE revoked_at IS NULL RETURNING id`. Reuse → revoke all for that user (logout tokens included). Concurrent refresh with the same token: one winner; loser's 401 path revokes the winner's new token too — a deliberate trade-off. Improvement: ~10s grace window that re-issues the successor if it hasn't been used yet.

### Login rate limit
Redis `login:fail:{email}` — **check before argon2**. Failures: Lua `INCR` + `EXPIRE 900`. Success: `DEL`. Redis down → **fail open** (logged); next step would be an in-process per-instance fallback so a Redis outage never means unlimited guesses.

### Pagination
Opaque cursor = urlsafe base64 of `{spent_on, id}`. Keyset: `(spent_on, id) < (:c_spent, :c_id) ORDER BY spent_on DESC, id DESC`. Index `(user_id, spent_on DESC, id DESC)`.

### Monthly report
One SQL: CTE bounds → categories × {prev, current} month grid → LEFT JOIN converted totals (LATERAL as-of rates) → `LAG` for MoM. Round once at the aggregate. The grid exists so categories with spend only last month still appear with a **negative** change. Persist rejects missing as-of rates and future `spent_on`.

### Idempotency
Optional `Idempotency-Key` on `POST /expenses`, scoped per user, stored in **Postgres** (not Redis). Race control: `INSERT … ON CONFLICT DO NOTHING RETURNING id` waits for the in-flight txn. Failures roll back the key row. Replay returns the **original 201 body** + `Idempotent-Replayed: true`. Improvement: `SET LOCAL lock_timeout` → 409 "in progress".

### ETag / PATCH
`ETag: "{version}"`. Postgres 16: CTE `FOR UPDATE` then `UPDATE … FROM old RETURNING e.*, old.*`. On PG18 I'd use `RETURNING old.*` directly. Missing `If-Match` → 428; stale → 412.

### Budget alerts
Same txn as expense writes → `outbox_events`. Worker: `FOR UPDATE SKIP LOCKED`, coalesce by `(user, category, month)`, recompute once, insert levels 80 and 100 with `ON CONFLICT DO NOTHING`, savepoints for poison. No retraction when totals drop.

### CSV import
Validate header → sha256 in 1 MB chunks → commit `import_jobs` **then** enqueue (so a fast worker never races an uncommitted row). Same `(user_id, file_hash)` → same `job_id`. Worker streams S3; `dedupe_hash = sha256(file_hash:row_number)`; `ON CONFLICT (user_id, dedupe_hash) WHERE dedupe_hash IS NOT NULL`. Retry resets counters/errors (re-reads whole file; checkpoint `last_committed_row` would be the upgrade). Batched INSERT beats COPY here because COPY can't skip conflicts without a staging table.

### Receipts
Presigned **POST** (not PUT) so the policy can enforce `content-length-range`, exact key, and Content-Type. Confirm: `head` (treats **403 and 404** as missing — real AWS without `s3:ListBucket` returns 403), magic bytes must match extension, bumps **version/ETag**. Deletion queue with `min(2^attempts, 60)` minute backoff. Orphan uploads (URL fetched, never confirmed) are a known gap — nightly reconciliation of unreferenced `receipts/` keys older than 24h.

**LocalStack 5 MB policy — verified:** a 6 MB file through the presigned POST returns `400 EntityTooLarge` with `MaxSizeAllowed=5242880`. Confirm still re-checks size via `head` as a second layer (and moto tests assert the policy JSON conditions even when the mock server is not the demo path).

When the task role uses temporary credentials, a presigned URL dies when those credentials expire even if its own `Expires` hasn't — short 10/15-minute URLs make that rare, but it's the real-AWS footgun.

## Indexes

| Index | Why |
| --- | --- |
| `users(email)` unique | Login / register |
| `expenses (user_id, spent_on DESC, id DESC)` | Keyset list |
| `expenses (user_id, category_id, spent_on)` | Category filter + month spend |
| `expenses (user_id, dedupe_hash) WHERE dedupe_hash IS NOT NULL` | CSV retry idempotency |
| `exchange_rates (currency, valid_on)` PK | As-of LATERAL |
| `idempotency_keys (user_id, key)` unique | Idempotency race |
| `outbox_events (created_at) WHERE processed_at IS NULL AND attempts < 5` | Worker poll |
| `notifications (user_id, category_id, period_month, level)` unique | Alert idempotency |
| `import_jobs (user_id, file_hash)` unique | File-level dedupe |
| `import_errors (job_id, row_number)` | Status payload |
| `s3_deletion_queue (next_attempt_at) WHERE deleted_at IS NULL` | Delete worker |
| `refresh_tokens (token_hash)` | Opaque lookup |

`expenses.category_id` has **no leading index** by itself — category filters always include `user_id`. A bare `category_id` index would only help admin cross-user queries we don't have.

## Money

`Decimal` in Python, `NUMERIC(14,2)` in Postgres, never `float`. JSON amounts are **strings** (e.g. `"12.50"`).

## Tests

```bash
pytest
```

**112+ tests**, coverage gate **85%** (`concurrency = ["greenlet"]` so SQLAlchemy async paths count). Latest run: **~91.86%**. Real Postgres + Redis DB 15; S3 via moto.

## AI usage

I used Cursor for scaffolding, boilerplate, tests, and first drafts. I reviewed and directed the design: rate-limit ordering (check before argon2), atomic refresh rotation, opaque refresh tokens, CSV row-hash dedupe + enqueue-after-commit, report category×month grid, partial unique `ON CONFLICT … WHERE`, and pruning indexes that didn't earn their keep.

## Skipped / known gaps

| Gap | How I'd build it |
| --- | --- |
| Orphan receipt reconciliation | Nightly job: list `receipts/`, delete keys >24h with no `expenses.receipt_key` |
| Refresh grace window | On reuse, if successor unused within ~10s, return it instead of revoke-all |
| Idempotency `lock_timeout` | `SET LOCAL lock_timeout = '2s'` on the claim INSERT → 409 |
| CSV checkpoint resume | Persist `last_committed_row`; skip already-flushed rows on retry |
| Multi-line CSV fields | Documented out of scope; would need a real CSV state machine over the stream |

## Deployment notes (AWS)

**ECS Fargate.** API service ≥2 tasks across 2 AZs behind an ALB, health check `GET /health`, rolling deploy `minimumHealthyPercent=100` / `maximumPercent=200`, autoscaling on CPU and ALB request count. Worker is a **separate** service (1–2 tasks). Safe to scale: outbox uses `SKIP LOCKED`, notifications are unique, arq cron jobs are unique per run.

**RDS PostgreSQL** in private subnets; security group allows **5432 only** from the API and worker SGs. Password from Secrets Manager via the task definition `secrets` block, with rotation.

**ElastiCache Redis** in private subnets, **6379** from app SGs only. If Redis is down: login rate limiting **fails open** (logged); **idempotency is unaffected** because keys live in Postgres; arq stops, so import jobs stay `pending` and outbox events accumulate durably in Postgres, then drain when Redis recovers.

**S3.** Private bucket, Block Public Access on, SSE-S3, CORS allowing POST from app origins, lifecycle rule expiring `imports/` after 7 days. The app uses its ECS task role — never long-lived access keys. Minimal IAM:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AppObjectAccess",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": [
        "arn:aws:s3:::ahya-receipts/receipts/*",
        "arn:aws:s3:::ahya-receipts/imports/*"
      ]
    }
  ]
}
```

`HeadObject` is authorized by `s3:GetObject`. Without `s3:ListBucket`, a missing key returns **403** (not 404) on real AWS; `app/storage/s3.py` `head()` treats both as not found.

**Migrations.** One-off ECS task runs `alembic upgrade head` **before** the new service version rolls out — never on app startup. Expand/contract: add nullable columns first, backfill, drop later. `CREATE INDEX CONCURRENTLY` needs an Alembic autocommit block (can't run in a transaction). Set `lock_timeout` so a migration can't stall live traffic.

## Postman

Import the files from the repo (this is the copy with saved examples):

1. Postman → **Import** → `postman/Ahya_Expense_Tracker.postman_collection.json`
2. Import `postman/local.postman_environment.json` and select **Ahya Local** (`baseUrl=http://localhost:8000`, empty tokens)

Do **not** rely on a shared workspace link — personal workspaces are private to me, and a cloud sync without a re-import can miss the captured examples. The JSON in `postman/` is the source of truth for reviewers.

**Run order:** Auth → Login first (test script saves tokens). Worker must be up for Notifications and Imports. Examples are real captures with tokens/signatures redacted.

Regenerate:

```bash
python scripts/capture_postman_examples.py   # needs compose + alembic upgrade head
python scripts/build_postman_collection.py
```

Optional Newman:

```bash
npx newman run postman/Ahya_Expense_Tracker.postman_collection.json \
  -e postman/local.postman_environment.json
```

## Short answers

**1. Presigned upload.** The API stays out of the data path — no memory, bandwidth, or worker time on 5 MB bodies. Risks: leaked URL reuse until expiry, oversized/wrong-type files, orphans, temporary-credential expiry killing a still-"valid" URL. Guards: short expiry, exact key, `content-length-range` in the POST policy, magic-byte confirm, delete on mismatch, reconciliation job.

**2. 20M rows.** Reproduce with real binds and `EXPLAIN (ANALYZE, BUFFERS)`. Look for Seq Scan, rows removed by filter, high shared reads, Sort nodes. `(user_id, spent_on DESC, id DESC)` matches the ORDER BY so there's no sort and `LIMIT` stops early. OFFSET reads and discards every skipped row. Trigram GIN for `q`. N+1 check: SQL logging, one query per page, no per-item category lookups.

**3. Three servers.** JWT is stateless; refresh state is in Postgres. Rate limit is shared Redis. Idempotency is a Postgres unique constraint. Workers are safe via `SKIP LOCKED` + unique notifications. Uploads go to S3, not local disk (CSV too). Migrations run once as a separate task, never on app startup.

**4. 500k outbox backlog.** Drain in batches of 100 with `SKIP LOCKED`, coalescing by `(user, category, month)` so recomputes are far fewer than events. The partial index keeps polling cheap. Protect the DB: cap batch size, sleep between batches, limit worker concurrency, optionally recompute on a read replica. Alerts arrive late but exactly once.

**5. Simple over perfect.** Check-then-increment on login rate limit can allow 1–2 extra attempts under concurrency; a single Lua check-and-increment would be exact. Acceptable here because argon2 still dominates cost and the window is short — perfect atomicity wasn't worth the complexity for this assessment.
