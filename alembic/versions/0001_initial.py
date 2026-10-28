"""Initial schema: all tables, constraints, indexes, pg_trgm.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-28
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.execute(
        """
        CREATE TABLE users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email VARCHAR(255) NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            base_currency CHAR(3) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_users_email UNIQUE (email),
            CONSTRAINT ck_users_base_currency_len CHECK (char_length(base_currency) = 3)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE categories (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name VARCHAR(100) NOT NULL,
            monthly_budget NUMERIC(14, 2) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_categories_user_name UNIQUE (user_id, name),
            CONSTRAINT ck_categories_monthly_budget_nonneg
                CHECK (monthly_budget IS NULL OR monthly_budget >= 0)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE expenses (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            category_id UUID NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
            amount NUMERIC(14, 2) NOT NULL,
            currency CHAR(3) NOT NULL,
            description VARCHAR(500) NULL,
            spent_on DATE NOT NULL,
            receipt_key VARCHAR(512) NULL,
            version INTEGER NOT NULL DEFAULT 1,
            dedupe_hash CHAR(64) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_expenses_amount_positive CHECK (amount > 0)
        )
        """
    )
    # Partial unique: worker-retry idempotency for CSV rows (NULLs allowed for manual creates).
    op.execute(
        """
        CREATE UNIQUE INDEX uq_expenses_user_dedupe_hash
        ON expenses (user_id, dedupe_hash)
        WHERE dedupe_hash IS NOT NULL
        """
    )
    # Cursor / keyset pagination: ORDER BY spent_on DESC, id DESC filtered by user.
    op.execute(
        """
        CREATE INDEX ix_expenses_user_spent_on_id
        ON expenses (user_id, spent_on DESC, id DESC)
        """
    )
    # Monthly report, budget recompute, category filter.
    op.execute(
        """
        CREATE INDEX ix_expenses_user_category_spent_on
        ON expenses (user_id, category_id, spent_on)
        """
    )
    # Description search (`q`) via trigram similarity / ILIKE.
    op.execute(
        """
        CREATE INDEX ix_expenses_description_trgm
        ON expenses USING gin (description gin_trgm_ops)
        """
    )

    op.execute(
        """
        CREATE TABLE exchange_rates (
            currency CHAR(3) NOT NULL,
            rate_to_usd NUMERIC(18, 8) NOT NULL,
            valid_on DATE NOT NULL,
            CONSTRAINT pk_exchange_rates PRIMARY KEY (currency, valid_on),
            CONSTRAINT ck_exchange_rates_rate_positive CHECK (rate_to_usd > 0)
        )
        """
    )
    # No separate (currency, valid_on DESC) index: the PK btree (currency, valid_on)
    # already supports WHERE currency = X AND valid_on <= D ORDER BY valid_on DESC LIMIT 1
    # (Postgres can scan a btree backwards).

    op.execute(
        """
        CREATE TABLE refresh_tokens (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash CHAR(64) NOT NULL,
            family_id UUID NOT NULL,
            revoked_at TIMESTAMPTZ NULL,
            replaced_by UUID NULL REFERENCES refresh_tokens(id),
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_refresh_tokens_token_hash UNIQUE (token_hash)
        )
        """
    )
    # Revoke-all on reuse detection (filter by user_id).
    op.execute("CREATE INDEX ix_refresh_tokens_user_id ON refresh_tokens (user_id)")
    # Periodic cleanup of expired rows.
    op.execute("CREATE INDEX ix_refresh_tokens_expires_at ON refresh_tokens (expires_at)")

    op.execute(
        """
        CREATE TABLE idempotency_keys (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            key VARCHAR(255) NOT NULL,
            request_hash CHAR(64) NOT NULL,
            response_status INTEGER NULL,
            response_body JSONB NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_idempotency_keys_user_key UNIQUE (user_id, key)
        )
        """
    )
    # Periodic delete of expired keys.
    op.execute("CREATE INDEX ix_idempotency_keys_expires_at ON idempotency_keys (expires_at)")

    op.execute(
        """
        CREATE TABLE outbox_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            event_type VARCHAR(64) NOT NULL,
            payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            processed_at TIMESTAMPTZ NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NULL
        )
        """
    )
    # Worker poll of unprocessed, non-poisoned events (FOR UPDATE SKIP LOCKED).
    op.execute(
        """
        CREATE INDEX ix_outbox_events_unprocessed
        ON outbox_events (created_at)
        WHERE processed_at IS NULL AND attempts < 5
        """
    )

    op.execute(
        """
        CREATE TABLE notifications (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            category_id UUID NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            period_month DATE NOT NULL,
            level SMALLINT NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_notifications_user_category_period_level
                UNIQUE (user_id, category_id, period_month, level),
            CONSTRAINT ck_notifications_level CHECK (level IN (80, 100))
        )
        """
    )
    # GET /notifications ordered by newest first.
    op.execute(
        """
        CREATE INDEX ix_notifications_user_created_at
        ON notifications (user_id, created_at DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE import_jobs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            file_hash CHAR(64) NOT NULL,
            filename VARCHAR(255) NOT NULL,
            storage_key VARCHAR(512) NOT NULL,
            status VARCHAR(20) NOT NULL,
            total_rows INTEGER NOT NULL DEFAULT 0,
            processed_rows INTEGER NOT NULL DEFAULT 0,
            failed_rows INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at TIMESTAMPTZ NULL,
            finished_at TIMESTAMPTZ NULL,
            CONSTRAINT uq_import_jobs_user_file_hash UNIQUE (user_id, file_hash)
        )
        """
    )
    # No (user_id, created_at) index: GET /imports/{id} is PK + user_id filter.
    # A list-my-imports endpoint would need that index; we don't have one.

    op.execute(
        """
        CREATE TABLE import_errors (
            id BIGSERIAL PRIMARY KEY,
            job_id UUID NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
            row_number INTEGER NOT NULL,
            reason TEXT NOT NULL
        )
        """
    )
    # Status payload errors ordered by row.
    op.execute(
        """
        CREATE INDEX ix_import_errors_job_row
        ON import_errors (job_id, row_number)
        """
    )

    op.execute(
        """
        CREATE TABLE s3_deletion_queue (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            s3_key VARCHAR(512) NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NULL,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ NULL
        )
        """
    )
    # Worker retries pending S3 deletes.
    op.execute(
        """
        CREATE INDEX ix_s3_deletion_queue_pending
        ON s3_deletion_queue (next_attempt_at)
        WHERE deleted_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS s3_deletion_queue")
    op.execute("DROP TABLE IF EXISTS import_errors")
    op.execute("DROP TABLE IF EXISTS import_jobs")
    op.execute("DROP TABLE IF EXISTS notifications")
    op.execute("DROP TABLE IF EXISTS outbox_events")
    op.execute("DROP TABLE IF EXISTS idempotency_keys")
    op.execute("DROP TABLE IF EXISTS refresh_tokens")
    op.execute("DROP TABLE IF EXISTS exchange_rates")
    op.execute("DROP TABLE IF EXISTS expenses")
    op.execute("DROP TABLE IF EXISTS categories")
    op.execute("DROP TABLE IF EXISTS users")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
