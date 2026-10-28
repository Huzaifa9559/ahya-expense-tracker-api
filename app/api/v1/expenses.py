import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, File, Header, Query, Response, UploadFile, status
from fastapi.responses import JSONResponse

from app.deps import CurrentUser, DbSession
from app.errors import AppError
from app.schemas.expenses import (
    ExpenseCreate,
    ExpenseListResponse,
    ExpenseResponse,
    ExpenseUpdate,
)
from app.schemas.imports import ImportJobAccepted
from app.schemas.money import parse_decimal
from app.schemas.receipts import (
    ReceiptConfirmRequest,
    ReceiptDownloadResponse,
    ReceiptUploadUrlRequest,
    ReceiptUploadUrlResponse,
)
from app.services import expenses as expenses_service
from app.services import imports as imports_service
from app.services import receipts as receipts_service
from app.services.etags import format_etag, parse_if_match

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


@router.post("/import", response_model=ImportJobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def import_expenses_csv(
    session: DbSession,
    user: CurrentUser,
    file: UploadFile = File(..., description="CSV with header date,amount,currency,category,description"),
) -> ImportJobAccepted:
    return await imports_service.accept_csv_import(session, user, upload=file)


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


@router.post(
    "/{expense_id}/receipt/upload-url",
    response_model=ReceiptUploadUrlResponse,
)
async def receipt_upload_url(
    expense_id: uuid.UUID,
    body: ReceiptUploadUrlRequest,
    session: DbSession,
    user: CurrentUser,
) -> ReceiptUploadUrlResponse:
    return await receipts_service.create_upload_url(
        session, user, expense_id=expense_id, body=body
    )


@router.post("/{expense_id}/receipt/confirm", response_model=ExpenseResponse)
async def receipt_confirm(
    expense_id: uuid.UUID,
    body: ReceiptConfirmRequest,
    session: DbSession,
    user: CurrentUser,
    response: Response,
) -> ExpenseResponse:
    return await receipts_service.confirm_receipt(
        session, user, expense_id=expense_id, body=body, response=response
    )


@router.get("/{expense_id}/receipt", response_model=ReceiptDownloadResponse)
async def receipt_download(
    expense_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> ReceiptDownloadResponse:
    return await receipts_service.download_receipt(
        session, user, expense_id=expense_id
    )


@router.get("/{expense_id}", response_model=ExpenseResponse)
async def get_expense(
    expense_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    response: Response,
) -> ExpenseResponse:
    expense = await expenses_service.get_expense(session, user, expense_id)
    response.headers["ETag"] = format_etag(expense.version)
    return expense


@router.patch("/{expense_id}", response_model=ExpenseResponse)
async def patch_expense(
    expense_id: uuid.UUID,
    body: ExpenseUpdate,
    session: DbSession,
    user: CurrentUser,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ExpenseResponse:
    expected = parse_if_match(if_match)
    expense, etag = await expenses_service.patch_expense(
        session,
        user,
        expense_id=expense_id,
        body=body,
        expected_version=expected,
    )
    response.headers["ETag"] = etag
    return expense


@router.delete("/{expense_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_expense(
    expense_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> Response:
    await expenses_service.delete_expense(session, user, expense_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
