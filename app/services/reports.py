from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.repositories import reports as reports_repo
from app.schemas.reports import MonthlyReportItem, MonthlyReportResponse


def _money_or_none(value) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


async def monthly_report(
    session: AsyncSession,
    user: User,
    *,
    year: int,
    month: int,
) -> MonthlyReportResponse:
    rows = await reports_repo.monthly_report(
        session, user_id=user.id, year=year, month=month
    )
    items: list[MonthlyReportItem] = []
    for row in rows:
        percent = row["percent_used"]
        items.append(
            MonthlyReportItem(
                category_id=str(row["category_id"]),
                name=row["name"],
                budget=_money_or_none(row["budget"]),
                total=Decimal(str(row["total"] or 0)),
                previous_total=Decimal(str(row["previous_total"] or 0)),
                change=Decimal(str(row["change"] or 0)),
                percent_used=float(percent) if percent is not None else None,
            )
        )
    return MonthlyReportResponse(
        year=year,
        month=month,
        base_currency=user.base_currency.strip(),
        items=items,
    )
