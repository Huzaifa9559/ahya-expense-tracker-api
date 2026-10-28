import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status
from fastapi.responses import JSONResponse

from app.deps import CurrentUser, DbSession
from app.errors import AppError
from app.schemas.expenses import ExpenseCreate, ExpenseListResponse, ExpenseResponse
from app.schemas.money import parse_decimal
from app.services import expenses as expenses_service

router = APIRouter(prefix="/expenses", tags=["expenses"])


def _optional_money(value: str | None) -> Decimal | None:
    if value is None:
        return None
    return parse_decimal(value)


@router.post("", response_model=None, status_code=status.HTTP_201_CREATED)
async def create_expense(
    body: ExpenseCreate,
    session: DbSession,
    user: CurrentUser,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", description="Optional; 1..255 chars; scoped per user"),
    ] = None,
) -> ExpenseResponse | JSONResponse:
    if idempotency_key is not None and not (1 <= len(idempotency_key) <= 255):
        raise AppError(
            code="validation_error",
            message="Idempotency-Key must be 1..255 characters",
            status_code=422,
        )

    result = await expenses_service.create_expense(
        session,
        user,
        body=body,
        idempotency_key=idempotency_key,
    )
    if result.replayed:
        return JSONResponse(
            status_code=result.status_code,
            content=result.body,
            headers={"Idempotent-Replayed": "true"},
        )
    assert result.response is not None
    return result.response


@router.get("", response_model=ExpenseListResponse)
async def list_expenses(
    session: DbSession,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
    category_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    min_amount: Annotated[
        str | None,
        Query(
            description=(
                "Minimum expense amount in the expense's own currency "
                "(not converted to base). Compare against amount as stored."
            )
        ),
    ] = None,
    max_amount: Annotated[
        str | None,
        Query(
            description=(
                "Maximum expense amount in the expense's own currency "
                "(not converted to base). Compare against amount as stored."
            )
        ),
    ] = None,
    currency: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    q: str | None = None,
) -> ExpenseListResponse:
    return await expenses_service.list_expenses(
        session,
        user,
        limit=limit,
        cursor=cursor,
        category_id=category_id,
        date_from=date_from,
        date_to=date_to,
        min_amount=_optional_money(min_amount),
        max_amount=_optional_money(max_amount),
        currency=currency,
        q=q,
    )


@router.get("/{expense_id}", response_model=ExpenseResponse)
async def get_expense(
    expense_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> ExpenseResponse:
    return await expenses_service.get_expense(session, user, expense_id)


@router.delete("/{expense_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_expense(
    expense_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> Response:
    await expenses_service.delete_expense(session, user, expense_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
