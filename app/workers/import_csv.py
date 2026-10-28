"""
CSV import worker.

Contract: one CSV record per line — no multi-line quoted fields. Category names
must already exist for the user (unknown names are row errors; we do not auto-create).

Batched INSERT … ON CONFLICT instead of COPY: COPY cannot skip conflicting
dedupe_hash rows without a staging table. At scale, COPY into a temp table then
INSERT … ON CONFLICT from it is the natural upgrade path.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import AsyncSessionLocal
from app.models import Category
from app.repositories import imports as imports_repo
from app.storage import s3

logger = logging.getLogger(__name__)

BATCH_SIZE = 1000


@dataclass
class _GoodRow:
    row_number: int
    category_id: uuid.UUID
    amount: Decimal
    currency: str
    description: str | None
    spent_on: date
    dedupe_hash: str


def _month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def _dedupe_hash(file_hash: str, row_number: int) -> str:
    return hashlib.sha256(f"{file_hash}:{row_number}".encode()).hexdigest()


def _validate_row(
    cells: list[str],
    *,
    row_number: int,
    file_hash: str,
    categories: dict[str, uuid.UUID],
    min_valid_on: dict[str, date],
    base_currency: str,
    today: date,
) -> _GoodRow | tuple[int, str]:
    if len(cells) < 4:
        return row_number, "expected columns: date,amount,currency,category,description"

    raw_date, raw_amount, raw_currency, raw_category = (
        cells[0].strip(),
        cells[1].strip(),
        cells[2].strip(),
        cells[3].strip(),
    )
    raw_description = cells[4].strip() if len(cells) > 4 else ""
    description = raw_description if raw_description else None

    try:
        spent_on = date.fromisoformat(raw_date)
    except ValueError:
        return row_number, "invalid date; expected YYYY-MM-DD"

    if spent_on > today:
        return row_number, "spent_on must not be in the future"

    try:
        amount = Decimal(raw_amount)
    except (InvalidOperation, ValueError):
        return row_number, "invalid amount"

    if amount <= 0:
        return row_number, "amount must be greater than 0"
    if amount.as_tuple().exponent < -2:
        return row_number, "amount must have at most 2 decimal places"

    currency = raw_currency.upper()
    if len(currency) != 3 or not currency.isalpha():
        return row_number, "currency must be a 3-letter code"

    currency_min = min_valid_on.get(currency)
    if currency_min is None or spent_on < currency_min:
        return row_number, f"no exchange rate for {currency} on or before {spent_on}"

    base_min = min_valid_on.get(base_currency)
    if base_min is None or spent_on < base_min:
        return (
            row_number,
            f"no exchange rate for base currency {base_currency} on or before {spent_on}",
        )

    category_id = categories.get(raw_category.strip().lower())
    if category_id is None:
        return row_number, f"unknown category: {raw_category}"

    if description is not None and len(description) > 500:
        return row_number, "description must be at most 500 characters"

    return _GoodRow(
        row_number=row_number,
        category_id=category_id,
        amount=amount,
        currency=currency,
        description=description,
        spent_on=spent_on,
        dedupe_hash=_dedupe_hash(file_hash, row_number),
    )


async def _load_category_map(
    session: AsyncSession, user_id: uuid.UUID
) -> dict[str, uuid.UUID]:
    result = await session.execute(
        select(Category.id, Category.name).where(Category.user_id == user_id)
    )
    return {name.strip().lower(): cid for cid, name in result.all()}


async def _load_base_currency(session: AsyncSession, user_id: uuid.UUID) -> str:
    result = await session.execute(
        text("SELECT base_currency FROM users WHERE id = :uid"),
        {"uid": user_id},
    )
    row = result.first()
    if row is None:
        raise ValueError(f"user {user_id} not found")
    return str(row[0]).strip().upper()


async def _flush_batch(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    good: list[_GoodRow],
    errors: list[tuple[int, str]],
) -> None:
    # 1000 rows × ~7 bind params stays under asyncpg's 32767-parameter limit.
    if good:
        await session.execute(
            text(
                """
                INSERT INTO expenses (
                    user_id, category_id, amount, currency, description,
                    spent_on, dedupe_hash
                )
                VALUES (
                    :user_id, :category_id, :amount, :currency, :description,
                    :spent_on, :dedupe_hash
                )
                ON CONFLICT (user_id, dedupe_hash)
                    WHERE dedupe_hash IS NOT NULL
                DO NOTHING
                """
            ),
            [
                {
                    "user_id": user_id,
                    "category_id": row.category_id,
                    "amount": row.amount,
                    "currency": row.currency,
                    "description": row.description,
                    "spent_on": row.spent_on,
                    "dedupe_hash": row.dedupe_hash,
                }
                for row in good
            ],
        )

        # One outbox event per distinct (category_id, month) in this batch — not per row.
        seen: set[tuple[uuid.UUID, date]] = set()
        outbox_rows: list[dict[str, Any]] = []
        for row in good:
            key = (row.category_id, _month_start(row.spent_on))
            if key in seen:
                continue
            seen.add(key)
            outbox_rows.append(
                {
                    "event_type": "expense.created",
                    "payload": {
                        "user_id": str(user_id),
                        "category_id": str(row.category_id),
                        "expense_id": str(uuid.uuid4()),
                        "spent_on": key[1].isoformat(),
                    },
                }
            )
        if outbox_rows:
            await session.execute(
                text(
                    """
                    INSERT INTO outbox_events (event_type, payload)
                    VALUES (:event_type, CAST(:payload AS jsonb))
                    """
                ),
                [
                    {
                        "event_type": r["event_type"],
                        "payload": json.dumps(r["payload"]),
                    }
                    for r in outbox_rows
                ],
            )

    await imports_repo.insert_errors(session, job_id=job_id, errors=errors)
    await imports_repo.bump_batch_counts(
        session,
        job_id=job_id,
        processed_delta=len(good) + len(errors),
        failed_delta=len(errors),
    )


async def import_csv(ctx: dict, job_id: str) -> str:
    """arq entrypoint: process one import_jobs row end-to-end."""
    session_factory: async_sessionmaker[AsyncSession] = ctx.get(
        "session_factory", AsyncSessionLocal
    )
    jid = uuid.UUID(str(job_id))
    today = datetime.now(UTC).date()

    async with session_factory() as session:
        try:
            # Each begin()/commit is discrete so S3 streaming is not held inside a txn.
            async with session.begin():
                job = await imports_repo.get_by_id(session, job_id=jid)
                if job is None:
                    logger.error("import_csv: job %s not found", jid)
                    return "missing"
                if job.status == "completed":
                    return "already_completed"
                if job.status == "processing":
                    await imports_repo.reset_for_retry(session, job_id=jid)
                await imports_repo.mark_processing(
                    session, job_id=jid, started_at=datetime.now(UTC)
                )
                user_id = job.user_id
                file_hash = job.file_hash.strip()
                storage_key = job.storage_key

            async with session.begin():
                categories = await _load_category_map(session, user_id)
                min_valid_on = await imports_repo.min_valid_on_by_currency(session)
                base_currency = await _load_base_currency(session, user_id)

            good_batch: list[_GoodRow] = []
            error_batch: list[tuple[int, str]] = []
            total_rows = 0
            header_seen = False

            async for line in s3.stream_lines(storage_key):
                if not header_seen:
                    header_seen = True
                    continue  # header already validated at upload time

                total_rows += 1
                row_number = total_rows + 1  # header = 1, first data = 2
                cells = next(csv.reader([line]), [])
                outcome = _validate_row(
                    cells,
                    row_number=row_number,
                    file_hash=file_hash,
                    categories=categories,
                    min_valid_on=min_valid_on,
                    base_currency=base_currency,
                    today=today,
                )
                if isinstance(outcome, _GoodRow):
                    good_batch.append(outcome)
                else:
                    error_batch.append(outcome)

                if len(good_batch) + len(error_batch) >= BATCH_SIZE:
                    async with session.begin():
                        await _flush_batch(
                            session,
                            user_id=user_id,
                            job_id=jid,
                            good=good_batch,
                            errors=error_batch,
                        )
                    good_batch = []
                    error_batch = []

            if good_batch or error_batch:
                async with session.begin():
                    await _flush_batch(
                        session,
                        user_id=user_id,
                        job_id=jid,
                        good=good_batch,
                        errors=error_batch,
                    )

            async with session.begin():
                await imports_repo.mark_completed(
                    session,
                    job_id=jid,
                    total_rows=total_rows,
                    finished_at=datetime.now(UTC),
                )
            return "completed"

        except Exception as exc:
            logger.exception("import_csv failed job_id=%s: %s", jid, exc)
            try:
                async with session.begin():
                    await imports_repo.mark_failed(
                        session, job_id=jid, finished_at=datetime.now(UTC)
                    )
            except Exception:
                logger.exception("could not mark import job %s failed", jid)
            return "failed"
