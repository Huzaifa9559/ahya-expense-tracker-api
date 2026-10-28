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

```bash
pytest
```

**Coverage note (Phase 0):** `--cov-fail-under` is temporarily `0` in `pyproject.toml` because only the scaffold exists. **Restore to `85` in Phase 10** per PLAN.md §4.

## Phase status

- Phase 0: scaffold, docker-compose, config, error envelope, `/health` — done
- Phases 1–10: see [PLAN.md](PLAN.md)
