#!/usr/bin/env sh
# Idempotent: create expense_tracker_test if missing (host or docker postgres).
set -eu

HOST="${POSTGRES_HOST:-localhost}"
PORT="${POSTGRES_PORT:-5432}"
USER="${POSTGRES_USER:-ahya}"
PASSWORD="${POSTGRES_PASSWORD:-ahya}"

export PGPASSWORD="$PASSWORD"

exists=$(psql -h "$HOST" -p "$PORT" -U "$USER" -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname = 'expense_tracker_test'")

if [ "$exists" = "1" ]; then
  echo "expense_tracker_test already exists"
  exit 0
fi

psql -h "$HOST" -p "$PORT" -U "$USER" -d postgres \
  -c "CREATE DATABASE expense_tracker_test;"
echo "created expense_tracker_test"
