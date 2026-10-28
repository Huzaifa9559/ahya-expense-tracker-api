import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models import Expense, User
from app.repositories import categories as categories_repo
from app.repositories import exchange_rates as rates_repo
from app.repositories import expenses as expenses_repo
from app.repositories import idempotency as idempotency_repo
from app.repositories import outbox as outbox_repo
from app.repositories import s3_deletion_queue as s3_queue_repo
from app.schemas.expenses import ExpenseCreate, ExpenseListResponse, ExpenseResponse
from app.services import cursors

_IDEMPOTENCY_TTL = timedelta(hours=24)


@dataclass
class CreateExpenseResult:
    response: ExpenseResponse | None
    replayed: bool
    status_code: int
    body: dict[str, Any] | None = None


def _expense_response(row: Expense) -> ExpenseResponse:
    return ExpenseResponse(
        id=str(row.id),
        category_id=str(row.category_id),
        amount=row.amount,
        currency=row.currency,
        description=row.description,
        spent_on=row.spent_on,
        receipt_key=row.receipt_key,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def request_hash(body: ExpenseCreate) -> str:
    canonical = json.dumps(
        body.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _validate_spent_on_and_rates(
    session: AsyncSession,
    *,
    spent_on: date,
    expense_currency: str,
    base_currency: str,
) -> None:
    today = datetime.now(UTC).date()
    if spent_on > today:
        raise AppError(
            code="FUTURE_SPENT_ON",
            message="spent_on cannot be in the future",
            status_code=422,
        )

    if not await rates_repo.has_as_of_rate(
        session, currency=expense_currency, on_or_before=spent_on
    ):
        raise AppError(
            code="MISSING_EXCHANGE_RATE",
            message=(
                f"No exchange rate for {expense_currency} on or before {spent_on.isoformat()}"
            ),
            status_code=422,
        )

    if not await rates_repo.has_as_of_rate(
        session, currency=base_currency, on_or_before=spent_on
    ):
        raise AppError(
            code="MISSING_EXCHANGE_RATE",
            message=(
                f"No exchange rate for base currency {base_currency} "
                f"on or before {spent_on.isoformat()}"
            ),
            status_code=422,
        )


async def _create_expense_core(
    session: AsyncSession,
    user: User,
    *,
    category_id: uuid.UUID,
    amount: Decimal,
    currency: str,
    description: str | None,
    spent_on: date,
) -> Expense:
    category = await categories_repo.get_by_id_for_user(
        session, category_id=category_id, user_id=user.id
    )
    if category is None:
        # Ownership: other users' categories look like missing resources.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")

    await _validate_spent_on_and_rates(
        session,
        spent_on=spent_on,
        expense_currency=currency,
        base_currency=user.base_currency,
    )

    expense = await expenses_repo.create(
        session,
        user_id=user.id,
        category_id=category_id,
        amount=amount,
        currency=currency,
        description=description,
        spent_on=spent_on,
    )
    await outbox_repo.add_expense_event(
        session,
        event_type="expense.created",
        user_id=user.id,
        category_id=category_id,
        expense_id=expense.id,
        spent_on=spent_on,
    )
    return expense


async def create_expense(
    session: AsyncSession,
    user: User,
    *,
    body: ExpenseCreate,
    idempotency_key: str | None = None,
) -> CreateExpenseResult:
    """
    Create an expense. Optional Idempotency-Key:

    Failures (404/422/…) raise before commit so the whole transaction — including any
    newly inserted idempotency_keys row — rolls back. Failed attempts are not stored;
    the client can fix the input and retry with the same key.
    """
    if idempotency_key is None:
        expense = await _create_expense_core(
            session,
            user,
            category_id=body.category_id,
            amount=body.amount,
            currency=body.currency,
            description=body.description,
            spent_on=body.spent_on,
        )
        await session.commit()
        await session.refresh(expense)
        resp = _expense_response(expense)
        return CreateExpenseResult(
            response=resp,
            replayed=False,
            status_code=status.HTTP_201_CREATED,
            body=resp.model_dump(mode="json"),
        )

    req_hash = request_hash(body)
    await idempotency_repo.delete_expired_for_key(
        session, user_id=user.id, key=idempotency_key
    )

    # See try_claim docstring: ON CONFLICT waits for in-flight holders (race control).
    claimed_id = await idempotency_repo.try_claim(
        session,
        user_id=user.id,
        key=idempotency_key,
        request_hash=req_hash,
        expires_at=datetime.now(UTC) + _IDEMPOTENCY_TTL,
    )

    if claimed_id is None:
        existing = await idempotency_repo.get_by_user_key(
            session, user_id=user.id, key=idempotency_key
        )
        if existing is None:
            # Should be rare (race with cleanup); treat as conflict-free miss.
            raise AppError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="Idempotency key conflict could not be resolved",
                status_code=422,
            )
        if existing.request_hash != req_hash:
            raise AppError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="Idempotency-Key was already used with a different request body",
                status_code=422,
            )
        # Replay winner's stored response (no expense work on the losing path).
        return CreateExpenseResult(
            response=None,
            replayed=True,
            status_code=existing.response_status or status.HTTP_201_CREATED,
            body=existing.response_body or {},
        )

    # We own the key: create expense, then persist the response on the key row.
    expense = await _create_expense_core(
        session,
        user,
        category_id=body.category_id,
        amount=body.amount,
        currency=body.currency,
        description=body.description,
        spent_on=body.spent_on,
    )
    resp = _expense_response(expense)
    body_json = resp.model_dump(mode="json")
    await idempotency_repo.store_response(
        session,
        key_id=claimed_id,
        response_status=status.HTTP_201_CREATED,
        response_body=body_json,
    )
    await session.commit()
    return CreateExpenseResult(
        response=resp,
        replayed=False,
        status_code=status.HTTP_201_CREATED,
        body=body_json,
    )


async def get_expense(
    session: AsyncSession, user: User, expense_id: uuid.UUID
) -> ExpenseResponse:
    expense = await expenses_repo.get_by_id_for_user(
        session, expense_id=expense_id, user_id=user.id
    )
    if expense is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found")
    return _expense_response(expense)


async def delete_expense(
    session: AsyncSession, user: User, expense_id: uuid.UUID
) -> None:
    expense = await expenses_repo.get_by_id_for_user(
        session, expense_id=expense_id, user_id=user.id
    )
    if expense is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found")

    category_id = expense.category_id
    spent_on = expense.spent_on
    receipt_key = expense.receipt_key

    await outbox_repo.add_expense_event(
        session,
        event_type="expense.deleted",
        user_id=user.id,
        category_id=category_id,
        expense_id=expense.id,
        spent_on=spent_on,
    )
    if receipt_key:
        await s3_queue_repo.enqueue(session, s3_key=receipt_key)

    await expenses_repo.delete(session, expense)
    await session.commit()


async def list_expenses(
    session: AsyncSession,
    user: User,
    *,
    limit: int = 20,
    cursor: str | None = None,
    category_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    currency: str | None = None,
    q: str | None = None,
) -> ExpenseListResponse:
    cursor_spent_on: date | None = None
    cursor_id: uuid.UUID | None = None
    if cursor:
        cursor_spent_on, cursor_id = cursors.decode_expense_cursor(cursor)

    currency_n = currency.upper() if currency else None

    rows = await expenses_repo.list_for_user(
        session,
        user_id=user.id,
        limit=limit,
        cursor_spent_on=cursor_spent_on,
        cursor_id=cursor_id,
        category_id=category_id,
        date_from=date_from,
        date_to=date_to,
        min_amount=min_amount,
        max_amount=max_amount,
        currency=currency_n,
        q=q,
    )

    next_cursor: str | None = None
    page = rows
    if len(rows) > limit:
        page = rows[:limit]
        last = page[-1]
        next_cursor = cursors.encode_expense_cursor(
            spent_on=last.spent_on, expense_id=last.id
        )

    return ExpenseListResponse(
        items=[_expense_response(r) for r in page],
        next_cursor=next_cursor,
    )
