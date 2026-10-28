import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ImportError, ImportJob


async def get_by_id_for_user(
    session: AsyncSession, *, job_id: uuid.UUID, user_id: uuid.UUID
) -> ImportJob | None:
    result = await session.execute(
        select(ImportJob).where(ImportJob.id == job_id, ImportJob.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def get_by_file_hash(
    session: AsyncSession, *, user_id: uuid.UUID, file_hash: str
) -> ImportJob | None:
    result = await session.execute(
        select(ImportJob).where(
            ImportJob.user_id == user_id, ImportJob.file_hash == file_hash
        )
    )
    return result.scalar_one_or_none()


async def get_by_id(session: AsyncSession, *, job_id: uuid.UUID) -> ImportJob | None:
    result = await session.execute(select(ImportJob).where(ImportJob.id == job_id))
    return result.scalar_one_or_none()


async def create(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    user_id: uuid.UUID,
    file_hash: str,
    filename: str,
    storage_key: str,
) -> ImportJob:
    row = ImportJob(
        id=job_id,
        user_id=user_id,
        file_hash=file_hash,
        filename=filename,
        storage_key=storage_key,
        status="pending",
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def mark_processing(
    session: AsyncSession, *, job_id: uuid.UUID, started_at: datetime
) -> None:
    await session.execute(
        text(
            """
            UPDATE import_jobs
            SET status = 'processing',
                started_at = :started_at
            WHERE id = :job_id
            """
        ),
        {"job_id": job_id, "started_at": started_at},
    )


async def reset_for_retry(session: AsyncSession, *, job_id: uuid.UUID) -> None:
    """
    Crash mid-job leaves status=processing; arq may retry. Dedupe hashes prevent
    duplicate expenses, but import_errors and counters would double — clear them first.
    """
    await session.execute(
        text("DELETE FROM import_errors WHERE job_id = :job_id"),
        {"job_id": job_id},
    )
    await session.execute(
        text(
            """
            UPDATE import_jobs
            SET processed_rows = 0,
                failed_rows = 0,
                total_rows = 0,
                finished_at = NULL
            WHERE id = :job_id
            """
        ),
        {"job_id": job_id},
    )


async def bump_batch_counts(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    processed_delta: int,
    failed_delta: int,
) -> None:
    await session.execute(
        text(
            """
            UPDATE import_jobs
            SET processed_rows = processed_rows + :processed_delta,
                failed_rows = failed_rows + :failed_delta
            WHERE id = :job_id
            """
        ),
        {
            "job_id": job_id,
            "processed_delta": processed_delta,
            "failed_delta": failed_delta,
        },
    )


async def mark_completed(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    total_rows: int,
    finished_at: datetime,
) -> None:
    await session.execute(
        text(
            """
            UPDATE import_jobs
            SET status = 'completed',
                total_rows = :total_rows,
                finished_at = :finished_at
            WHERE id = :job_id
            """
        ),
        {"job_id": job_id, "total_rows": total_rows, "finished_at": finished_at},
    )


async def mark_failed(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    finished_at: datetime,
) -> None:
    await session.execute(
        text(
            """
            UPDATE import_jobs
            SET status = 'failed',
                finished_at = :finished_at
            WHERE id = :job_id
            """
        ),
        {"job_id": job_id, "finished_at": finished_at},
    )


async def insert_errors(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    errors: list[tuple[int, str]],
) -> None:
    if not errors:
        return
    await session.execute(
        text(
            """
            INSERT INTO import_errors (job_id, row_number, reason)
            VALUES (:job_id, :row_number, :reason)
            """
        ),
        [
            {"job_id": job_id, "row_number": row_number, "reason": reason}
            for row_number, reason in errors
        ],
    )


async def list_errors(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    limit: int,
) -> tuple[list[ImportError], bool]:
    result = await session.execute(
        select(ImportError)
        .where(ImportError.job_id == job_id)
        .order_by(ImportError.row_number)
        .limit(limit + 1)
    )
    rows = list(result.scalars().all())
    truncated = len(rows) > limit
    return rows[:limit], truncated


async def min_valid_on_by_currency(session: AsyncSession) -> dict[str, Any]:
    result = await session.execute(
        text(
            """
            SELECT currency, MIN(valid_on) AS min_valid_on
            FROM exchange_rates
            GROUP BY currency
            """
        )
    )
    return {str(row.currency): row.min_valid_on for row in result}
