import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Select, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Expense


def _escape_like(value: str) -> str:
    # Escape ILIKE wildcards so user "%" / "_" are literals.
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def get_by_id_for_user(
    session: AsyncSession, *, expense_id: uuid.UUID, user_id: uuid.UUID
) -> Expense | None:
    result = await session.execute(
        select(Expense).where(Expense.id == expense_id, Expense.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def create(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    category_id: uuid.UUID,
    amount: Decimal,
    currency: str,
    description: str | None,
    spent_on: date,
) -> Expense:
    row = Expense(
        user_id=user_id,
        category_id=category_id,
        amount=amount,
        currency=currency,
        description=description,
        spent_on=spent_on,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def delete(session: AsyncSession, expense: Expense) -> None:
    await session.delete(expense)
    await session.flush()


async def list_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    limit: int,
    cursor_spent_on: date | None = None,
    cursor_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    currency: str | None = None,
    q: str | None = None,
) -> list[Expense]:
    """
    Keyset page: ORDER BY spent_on DESC, id DESC; fetch limit+1 to detect next page.
    min_amount / max_amount filter the expense's own amount (not converted to base).
    """
    stmt: Select[tuple[Expense]] = select(Expense).where(Expense.user_id == user_id)

    if cursor_spent_on is not None and cursor_id is not None:
        stmt = stmt.where(
            tuple_(Expense.spent_on, Expense.id) < (cursor_spent_on, cursor_id)
        )
    if category_id is not None:
        stmt = stmt.where(Expense.category_id == category_id)
    if date_from is not None:
        stmt = stmt.where(Expense.spent_on >= date_from)
    if date_to is not None:
        stmt = stmt.where(Expense.spent_on <= date_to)
    if min_amount is not None:
        stmt = stmt.where(Expense.amount >= min_amount)
    if max_amount is not None:
        stmt = stmt.where(Expense.amount <= max_amount)
    if currency is not None:
        stmt = stmt.where(Expense.currency == currency)
    if q is not None and q != "":
        pattern = f"%{_escape_like(q)}%"
        stmt = stmt.where(Expense.description.ilike(pattern, escape="\\"))

    stmt = stmt.order_by(Expense.spent_on.desc(), Expense.id.desc()).limit(limit + 1)
    result = await session.execute(stmt)
    return list(result.scalars().all())
