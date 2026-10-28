from datetime import date, datetime

from pydantic import BaseModel


class NotificationItem(BaseModel):
    id: str
    category_id: str
    category_name: str
    period_month: date
    level: int
    message: str
    created_at: datetime


class NotificationListResponse(BaseModel):
    items: list[NotificationItem]
