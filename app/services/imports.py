"""CSV import accept + job status (worker lives in app.workers.import_csv)."""

from __future__ import annotations

import hashlib
import logging
import uuid
from csv import reader as csv_reader
from io import StringIO

from fastapi import UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models import User
from app.repositories import imports as imports_repo
from app.schemas.imports import ImportErrorItem, ImportJobAccepted, ImportJobStatus
from app.storage import s3
from app.workers.enqueue import enqueue_import_csv

logger = logging.getLogger(__name__)

MAX_IMPORT_BYTES = 20 * 1024 * 1024
HASH_CHUNK = 1024 * 1024
EXPECTED_HEADER = ["date", "amount", "currency", "category", "description"]
# Cap GET /imports/{id} error list; tests may monkeypatch this.
ERRORS_LIST_CAP = 1000


def _require_csv_filename(filename: str | None) -> str:
    name = (filename or "").strip() or "upload.csv"
    if not name.lower().endswith(".csv"):
        raise AppError(
            code="validation_error",
            message="Only .csv files are accepted",
            status_code=422,
        )
    return name


async def _read_and_validate_header(upload: UploadFile) -> None:
    """Read the first line only; wrong header → 422 before any S3 work."""
    await upload.seek(0)
    buf = b""
    while b"\n" not in buf:
        chunk = await upload.read(4096)
        if not chunk:
            break
        buf += chunk
    await upload.seek(0)

    if not buf:
        raise AppError(
            code="validation_error",
            message="CSV header must be exactly: date,amount,currency,category,description",
            status_code=422,
        )

    first_line = buf.split(b"\n", 1)[0].decode("utf-8-sig").rstrip("\r")
    parsed = next(csv_reader(StringIO(first_line)), None)
    if parsed != EXPECTED_HEADER:
        raise AppError(
            code="validation_error",
            message="CSV header must be exactly: date,amount,currency,category,description",
            status_code=422,
            details=[{"got": parsed}],
        )


async def _sha256_upload(upload: UploadFile) -> str:
    """Hash in 1 MB chunks; never load the whole file into memory."""
    await upload.seek(0)
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = await upload.read(HASH_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_IMPORT_BYTES:
            raise AppError(
                code="payload_too_large",
                message="CSV must be at most 20 MB",
                status_code=413,
            )
        digest.update(chunk)
    await upload.seek(0)
    return digest.hexdigest()


async def accept_csv_import(
    session: AsyncSession,
    user: User,
    *,
    upload: UploadFile,
) -> ImportJobAccepted:
    filename = _require_csv_filename(upload.filename)
    await _read_and_validate_header(upload)
    file_hash = await _sha256_upload(upload)

    existing = await imports_repo.get_by_file_hash(
        session, user_id=user.id, file_hash=file_hash
    )
    if existing is not None:
        return ImportJobAccepted(job_id=existing.id, status=existing.status)

    job_id = uuid.uuid4()
    storage_key = f"imports/{user.id}/{job_id}.csv"
    # UploadFile.file is a SpooledTemporaryFile (sync BinaryIO) — seekable for aioboto3.
    await upload.seek(0)
    await s3.put_fileobj(storage_key, upload.file)

    try:
        job = await imports_repo.create(
            session,
            job_id=job_id,
            user_id=user.id,
            file_hash=file_hash,
            filename=filename,
            storage_key=storage_key,
        )
        await session.commit()
    except IntegrityError:
        # UNIQUE(user_id, file_hash) race: another request won — drop orphan object.
        await session.rollback()
        await s3.delete_object(storage_key)
        winner = await imports_repo.get_by_file_hash(
            session, user_id=user.id, file_hash=file_hash
        )
        if winner is None:
            raise AppError(
                code="conflict",
                message="Import job conflict could not be resolved",
                status_code=409,
            ) from None
        return ImportJobAccepted(job_id=winner.id, status=winner.status)

    try:
        await enqueue_import_csv(job.id)
    except Exception:
        # Job row is durable; a worker or test can still process it later.
        logger.exception("enqueue failed for import job %s; left pending", job.id)

    return ImportJobAccepted(job_id=job.id, status=job.status)


async def get_import_job(
    session: AsyncSession,
    user: User,
    *,
    job_id: uuid.UUID,
) -> ImportJobStatus:
    job = await imports_repo.get_by_id_for_user(
        session, job_id=job_id, user_id=user.id
    )
    if job is None:
        raise AppError(code="not_found", message="Import job not found", status_code=404)

    errors, truncated = await imports_repo.list_errors(
        session, job_id=job.id, limit=ERRORS_LIST_CAP
    )
    succeeded = max(job.processed_rows - job.failed_rows, 0)
    return ImportJobStatus(
        job_id=job.id,
        status=job.status,
        total_rows=job.total_rows,
        processed_rows=job.processed_rows,
        failed_rows=job.failed_rows,
        succeeded_rows=succeeded,
        started_at=job.started_at,
        finished_at=job.finished_at,
        errors=[
            ImportErrorItem(row_number=e.row_number, reason=e.reason) for e in errors
        ],
        errors_truncated=truncated,
    )
