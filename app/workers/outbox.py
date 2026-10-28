"""
Outbox → budget notifications.

Why replays and concurrent workers cannot duplicate alerts:
1. Claim uses FOR UPDATE SKIP LOCKED — two workers never process the same row.
2. Notifications have UNIQUE (user_id, category_id, period_month, level) with
   ON CONFLICT DO NOTHING — a second insert for the same alert is a no-op.
3. Marking processed_at happens in the same transaction as the notify insert, so a
   crash before commit leaves the event claimable again, but the unique constraint
   still prevents a second notification row.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import AsyncSessionLocal
from app.repositories import budgets as budgets_repo
from app.repositories import notifications as notifications_repo
from app.repositories import outbox as outbox_repo

logger = logging.getLogger(__name__)

_LEVELS = (80, 100)


def _month_end_exclusive(month_start: date) -> date:
    if month_start.month == 12:
        return date(month_start.year + 1, 1, 1)
    return date(month_start.year, month_start.month + 1, 1)


def _fmt_money(amount: Decimal) -> str:
    return f"{amount:,.2f}"


def _alert_message(
    *,
    category_name: str,
    level: int,
    period_month: date,
    base_currency: str,
    spend: Decimal,
    budget: Decimal,
) -> str:
    period = period_month.strftime("%Y-%m")
    return (
        f"{category_name} reached {level}% of its {period} budget "
        f"({base_currency} {_fmt_money(spend)} of {_fmt_money(budget)})"
    )


async def _user_base_currency(session: AsyncSession, user_id: uuid.UUID) -> str:
    result = await session.execute(
        text("SELECT base_currency FROM users WHERE id = :uid"),
        {"uid": user_id},
    )
    row = result.first()
    if row is None:
        raise ValueError(f"user {user_id} not found")
    return str(row[0]).strip()


async def _process_group(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    category_id: uuid.UUID,
    period_month: date,
    event_ids: list[uuid.UUID],
) -> None:
    month_end = _month_end_exclusive(period_month)
    stats = await budgets_repo.category_month_spend_in_base(
        session,
        user_id=user_id,
        category_id=category_id,
        month_start=period_month,
        month_end=month_end,
    )
    if stats is None:
        # Category gone: nothing to alert; still drain the events.
        await outbox_repo.mark_processed(session, event_ids=event_ids)
        return

    spend, budget, name = stats
    if budget is None or budget == 0:
        await outbox_repo.mark_processed(session, event_ids=event_ids)
        return

    base_currency = await _user_base_currency(session, user_id)
    for level in _LEVELS:
        threshold = budget * Decimal(level) / Decimal(100)
        if spend >= threshold:
            await notifications_repo.insert_if_absent(
                session,
                user_id=user_id,
                category_id=category_id,
                period_month=period_month,
                level=level,
                message=_alert_message(
                    category_name=name,
                    level=level,
                    period_month=period_month,
                    base_currency=base_currency,
                    spend=spend,
                    budget=budget,
                ),
            )
    await outbox_repo.mark_processed(session, event_ids=event_ids)


def _coalesce(
    events: list[dict[str, Any]],
) -> dict[tuple[uuid.UUID, uuid.UUID, date], list[uuid.UUID]]:
    groups: dict[tuple[uuid.UUID, uuid.UUID, date], list[uuid.UUID]] = defaultdict(list)
    for ev in events:
        payload = ev["payload"]
        user_id = uuid.UUID(str(payload["user_id"]))
        category_id = uuid.UUID(str(payload["category_id"]))
        spent_on = date.fromisoformat(str(payload["spent_on"]))
        period_month = date(spent_on.year, spent_on.month, 1)
        groups[(user_id, category_id, period_month)].append(ev["id"])
    return groups


async def process_outbox_batch(ctx: dict, batch_size: int = 100) -> int:
    """
    Claim up to batch_size events, coalesce by (user, category, month), recompute once
    per group. Returns the number of events marked processed in this run.
    """
    session_factory: async_sessionmaker[AsyncSession] = ctx.get(
        "session_factory", AsyncSessionLocal
    )
    processed = 0

    async with session_factory() as session:
        async with session.begin():
            claimed = await outbox_repo.claim_batch(session, batch_size=batch_size)
            if not claimed:
                return 0

            groups = _coalesce(claimed)
            for (user_id, category_id, period_month), event_ids in groups.items():
                try:
                    async with session.begin_nested():
                        await _process_group(
                            session,
                            user_id=user_id,
                            category_id=category_id,
                            period_month=period_month,
                            event_ids=event_ids,
                        )
                    processed += len(event_ids)
                except Exception as exc:
                    logger.exception(
                        "outbox group failed user=%s category=%s month=%s",
                        user_id,
                        category_id,
                        period_month,
                    )
                    await outbox_repo.record_failure(
                        session,
                        event_ids=event_ids,
                        error=f"{type(exc).__name__}: {exc}",
                    )

    return processed


async def purge_processed_outbox(ctx: dict) -> int:
    """Delete processed outbox rows older than 7 days."""
    session_factory: async_sessionmaker[AsyncSession] = ctx.get(
        "session_factory", AsyncSessionLocal
    )
    async with session_factory() as session:
        async with session.begin():
            return await outbox_repo.delete_processed_older_than_days(session, days=7)
