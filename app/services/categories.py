import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.repositories import categories as categories_repo
from app.schemas.categories import CategoryListItem, CategoryResponse


def _month_bounds_utc(now: datetime | None = None) -> tuple[date, date]:
    """Return [month_start, next_month_start) in UTC calendar months."""
    now = now or datetime.now(UTC)
    start = date(now.year, now.month, 1)
    if now.month == 12:
        end = date(now.year + 1, 1, 1)
    else:
        end = date(now.year, now.month + 1, 1)
    return start, end


def _percent_used(spend: Decimal, budget: Decimal | None) -> float | None:
    if budget is None or budget == 0:
        return None
    return float((spend * Decimal("100") / budget).quantize(Decimal("0.01")))


def _category_response(row) -> CategoryResponse:
    return CategoryResponse(
        id=str(row.id),
        name=row.name,
        monthly_budget=row.monthly_budget,
        created_at=row.created_at,
    )


async def create_category(
    session: AsyncSession,
    user: User,
    *,
    name: str,
    monthly_budget: Decimal | None,
) -> CategoryResponse:
    try:
        row = await categories_repo.create(
            session,
            user_id=user.id,
            name=name,
            monthly_budget=monthly_budget,
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Category name already exists",
        ) from None
    return _category_response(row)


async def get_category(
    session: AsyncSession, user: User, category_id: uuid.UUID
) -> CategoryResponse:
    row = await categories_repo.get_by_id_for_user(
        session, category_id=category_id, user_id=user.id
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    return _category_response(row)


async def list_categories(session: AsyncSession, user: User) -> list[CategoryListItem]:
    month_start, month_end = _month_bounds_utc()
    rows = await categories_repo.list_with_month_spend(
        session,
        user_id=user.id,
        month_start=month_start,
        month_end=month_end,
        base_currency=user.base_currency,
    )
    items: list[CategoryListItem] = []
    for row in rows:
        spend = Decimal(str(row["spend"] or 0))
        budget = row["monthly_budget"]
        if budget is not None:
            budget = Decimal(str(budget))
        items.append(
            CategoryListItem(
                id=str(row["id"]),
                name=row["name"],
                monthly_budget=budget,
                created_at=row["created_at"],
                spend=spend,
                percent_used=_percent_used(spend, budget),
            )
        )
    return items
