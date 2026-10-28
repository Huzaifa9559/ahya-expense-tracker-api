from typing import Annotated

from fastapi import APIRouter, Query

from app.deps import CurrentUser, DbSession
from app.schemas.reports import MonthlyReportResponse
from app.services import reports as reports_service

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/monthly", response_model=MonthlyReportResponse)
async def monthly_report(
    session: DbSession,
    user: CurrentUser,
    year: Annotated[int, Query(ge=2000, le=2100)],
    month: Annotated[int, Query(ge=1, le=12)],
) -> MonthlyReportResponse:
    return await reports_service.monthly_report(
        session, user, year=year, month=month
    )
