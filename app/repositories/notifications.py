import uuid
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def insert_if_absent(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    category_id: uuid.UUID,
    period_month: date,
    level: int,
    message: str,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO notifications (
                user_id, category_id, period_month, level, message
            ) VALUES (
                :user_id, :category_id, :period_month, :level, :message
            )
            ON CONFLICT (user_id, category_id, period_month, level) DO NOTHING
            """
        ),
        {
            "user_id": user_id,
            "category_id": category_id,
            "period_month": period_month,
            "level": level,
            "message": message,
        },
    )


async def list_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    limit: int,
) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            """
            SELECT
                n.id,
                n.category_id,
                c.name AS category_name,
                n.period_month,
                n.level,
                n.message,
                n.created_at
            FROM notifications n
            JOIN categories c ON c.id = n.category_id
            WHERE n.user_id = :user_id
            ORDER BY n.created_at DESC, n.id DESC
            LIMIT :limit
            """
        ),
        {"user_id": user_id, "limit": limit},
    )
    return [dict(row) for row in result.mappings().all()]
