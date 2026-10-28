"""Receipt upload-url / confirm / download. All S3 I/O via app.storage.s3."""

from __future__ import annotations

import re
import uuid

from fastapi import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models import User
from app.repositories import expenses as expenses_repo
from app.repositories import s3_deletion_queue as s3_queue_repo
from app.schemas.expenses import ExpenseResponse
from app.schemas.receipts import (
    ReceiptConfirmRequest,
    ReceiptDownloadResponse,
    ReceiptUploadUrlRequest,
    ReceiptUploadUrlResponse,
)
from app.services.etags import format_etag
from app.storage import s3

MAX_RECEIPT_BYTES = 5_242_880
UPLOAD_EXPIRES = 600
DOWNLOAD_EXPIRES = 900

_CONTENT_TYPE_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "application/pdf": "pdf",
}

def _key_pattern(user_id: uuid.UUID, expense_id: uuid.UUID) -> re.Pattern[str]:
    return re.compile(
        rf"^receipts/{user_id}/{expense_id}/"
        rf"[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-"
        rf"[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{12}}\.(jpg|png|pdf)$"
    )


def _expense_response(row) -> ExpenseResponse:
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


def _detect_type(prefix: bytes) -> str | None:
    if prefix.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if prefix.startswith(b"%PDF"):
        return "pdf"
    return None


async def create_upload_url(
    session: AsyncSession,
    user: User,
    *,
    expense_id: uuid.UUID,
    body: ReceiptUploadUrlRequest,
) -> ReceiptUploadUrlResponse:
    expense = await expenses_repo.get_by_id_for_user(
        session, expense_id=expense_id, user_id=user.id
    )
    if expense is None:
        raise AppError(code="not_found", message="Expense not found", status_code=404)

    ext = _CONTENT_TYPE_EXT[body.content_type]
    key = f"receipts/{user.id}/{expense_id}/{uuid.uuid4()}.{ext}"
    presigned = await s3.presign_post(
        key,
        body.content_type,
        max_bytes=MAX_RECEIPT_BYTES,
        expires=UPLOAD_EXPIRES,
    )
    return ReceiptUploadUrlResponse(
        url=presigned["url"],
        fields=presigned["fields"],
        key=key,
        expires_in=UPLOAD_EXPIRES,
        max_bytes=MAX_RECEIPT_BYTES,
    )


async def confirm_receipt(
    session: AsyncSession,
    user: User,
    *,
    expense_id: uuid.UUID,
    body: ReceiptConfirmRequest,
    response: Response,
) -> ExpenseResponse:
    expense = await expenses_repo.get_by_id_for_user(
        session, expense_id=expense_id, user_id=user.id
    )
    if expense is None:
        raise AppError(code="not_found", message="Expense not found", status_code=404)

    key = body.key.strip()
    if not _key_pattern(user.id, expense_id).match(key):
        raise AppError(
            code="INVALID_RECEIPT_KEY",
            message="Receipt key does not match this expense",
            status_code=422,
        )

    size = await s3.head(key)
    if size is None:
        raise AppError(
            code="RECEIPT_NOT_UPLOADED",
            message="Receipt object was not found in storage",
            status_code=422,
        )
    if size > MAX_RECEIPT_BYTES:
        await s3.delete_object(key)
        raise AppError(
            code="RECEIPT_TOO_LARGE",
            message="Receipt must be at most 5 MB",
            status_code=422,
        )

    prefix = await s3.read_prefix(key, 8)
    detected = _detect_type(prefix)
    ext = key.rsplit(".", 1)[-1].lower()
    if detected is None or detected != ext:
        await s3.delete_object(key)
        raise AppError(
            code="INVALID_FILE_TYPE",
            message="File type does not match the key extension",
            status_code=422,
        )

    old_key = expense.receipt_key
    if old_key and old_key != key:
        await s3_queue_repo.enqueue(session, s3_key=old_key)

    updated = await expenses_repo.set_receipt_key(
        session,
        expense_id=expense_id,
        user_id=user.id,
        receipt_key=key,
    )
    if updated is None:
        raise AppError(code="not_found", message="Expense not found", status_code=404)

    await session.commit()
    # Confirming a receipt changes the expense → ETag / version bumps.
    response.headers["ETag"] = format_etag(updated.version)
    return _expense_response(updated)


async def download_receipt(
    session: AsyncSession,
    user: User,
    *,
    expense_id: uuid.UUID,
) -> ReceiptDownloadResponse:
    expense = await expenses_repo.get_by_id_for_user(
        session, expense_id=expense_id, user_id=user.id
    )
    if expense is None:
        raise AppError(code="not_found", message="Expense not found", status_code=404)
    if not expense.receipt_key:
        raise AppError(
            code="RECEIPT_NOT_FOUND",
            message="Expense has no receipt",
            status_code=404,
        )

    url = await s3.presign_get(expense.receipt_key, expires=DOWNLOAD_EXPIRES)
    return ReceiptDownloadResponse(url=url, expires_in=DOWNLOAD_EXPIRES)
