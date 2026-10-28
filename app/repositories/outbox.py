import uuid
from datetime import date
from typing import Any

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
