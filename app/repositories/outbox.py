import uuid
from datetime import date
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OutboxEvent


async def add_expense_event(
    session: AsyncSession,
    *,
    event_type: str,
    user_id: uuid.UUID,
    category_id: uuid.UUID,
    expense_id: uuid.UUID,
    spent_on: date,
) -> OutboxEvent:
    payload: dict[str, Any] = {
        "user_id": str(user_id),
        "category_id": str(category_id),
        "expense_id": str(expense_id),
        "spent_on": spent_on.isoformat(),
    }
    row = OutboxEvent(event_type=event_type, payload=payload)
    session.add(row)
    await session.flush()
    return row


async def claim_batch(
    session: AsyncSession, *, batch_size: int
) -> list[dict[str, Any]]:
    """Claim pending events with FOR UPDATE SKIP LOCKED (must run inside a txn)."""
    result = await session.execute(
        text(
            """
            SELECT id, event_type, payload, attempts
            FROM outbox_events
            WHERE processed_at IS NULL
              AND attempts < 5
            ORDER BY created_at
            LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
            """
        ),
        {"batch_size": batch_size},
    )
    return [dict(row) for row in result.mappings().all()]


async def mark_processed(
    session: AsyncSession, *, event_ids: list[uuid.UUID]
) -> None:
    if not event_ids:
        return
    stmt = text(
        """
        UPDATE outbox_events
        SET processed_at = now()
        WHERE id = ANY(:ids)
        """
    ).bindparams(bindparam("ids", type_=ARRAY(PGUUID(as_uuid=True))))
    await session.execute(stmt, {"ids": event_ids})


async def record_failure(
    session: AsyncSession, *, event_ids: list[uuid.UUID], error: str
) -> None:
    if not event_ids:
        return
    stmt = text(
        """
        UPDATE outbox_events
        SET attempts = attempts + 1,
            last_error = :error
        WHERE id = ANY(:ids)
        """
    ).bindparams(bindparam("ids", type_=ARRAY(PGUUID(as_uuid=True))))
    await session.execute(stmt, {"ids": event_ids, "error": error[:2000]})


async def delete_processed_older_than_days(
    session: AsyncSession, *, days: int = 7
) -> int:
    result = await session.execute(
        text(
            """
            DELETE FROM outbox_events
            WHERE processed_at IS NOT NULL
              AND processed_at < now() - make_interval(days => :days)
            """
        ),
        {"days": days},
    )
    return result.rowcount or 0
