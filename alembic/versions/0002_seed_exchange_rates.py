"""Seed exchange rates for PKR, AED, USD, EUR (weekdays, 90 days).

Revision ID: 0002_seed_exchange_rates
Revises: 0001_initial
Create Date: 2026-10-28
"""

from datetime import date
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert

from app.seed_data import generate_seed_rates

revision: str = "0002_seed_exchange_rates"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Anchor used by downgrade so we only delete rows this migration inserted.
_SEED_TAG_START = date(1970, 1, 1)  # unused; we key off currency set + window


def upgrade() -> None:
    rows = generate_seed_rates(date.today())
    if not rows:
        return

    exchange_rates = sa.table(
        "exchange_rates",
        sa.column("currency", sa.CHAR(3)),
        sa.column("rate_to_usd", sa.Numeric(18, 8)),
        sa.column("valid_on", sa.Date),
    )
    # Chunk inserts; ON CONFLICT DO NOTHING keeps the migration re-runnable.
    chunk_size = 500
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i : i + chunk_size]
        stmt = insert(exchange_rates).values(chunk)
        stmt = stmt.on_conflict_do_nothing(index_elements=["currency", "valid_on"])
        op.execute(stmt)


def downgrade() -> None:
    # Remove only the four seeded currencies for the last 90 calendar days from today.
    # (Same window the upgrade wrote; safe if tests also inserted overlapping keys.)
    op.execute(
        sa.text(
            """
            DELETE FROM exchange_rates
            WHERE currency IN ('PKR', 'AED', 'USD', 'EUR')
              AND valid_on >= (CURRENT_DATE - INTERVAL '90 days')
              AND valid_on <= CURRENT_DATE
            """
        )
    )
