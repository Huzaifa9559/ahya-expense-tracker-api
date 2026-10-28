from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.repositories import notifications as notifications_repo
from app.schemas.notifications import NotificationItem, NotificationListResponse


async def list_notifications(
    session: AsyncSession,
    user: User,
    *,
    limit: int = 50,
) -> NotificationListResponse:
    rows = await notifications_repo.list_for_user(
        session, user_id=user.id, limit=limit
    )
    items = [
        NotificationItem(
            id=str(row["id"]),
            category_id=str(row["category_id"]),
            category_name=row["category_name"],
            period_month=row["period_month"],
            level=int(row["level"]),
            message=row["message"],
            created_at=row["created_at"],
        )
        for row in rows
    ]
    return NotificationListResponse(items=items)
