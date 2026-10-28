import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import S3DeletionQueue


async def enqueue(session: AsyncSession, *, s3_key: str) -> S3DeletionQueue:
    row = S3DeletionQueue(s3_key=s3_key)
    session.add(row)
    await session.flush()
    return row


async def claim_batch(
    session: AsyncSession, *, batch_size: int
) -> list[dict[str, Any]]:
    """
    Claim pending deletes with FOR UPDATE SKIP LOCKED.

    Row locks are held for the rest of the transaction (including S3 calls when
    the worker keeps one txn open). Acceptable with small batches; the alternative
    is a lease column (bump next_attempt_at, commit, then call S3).
    """
    result = await session.execute(
        text(
            """
            SELECT id, s3_key, attempts
            FROM s3_deletion_queue
            WHERE deleted_at IS NULL
              AND next_attempt_at <= now()
            ORDER BY next_attempt_at
            LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
            """
        ),
        {"batch_size": batch_size},
    )
    return [dict(row) for row in result.mappings().all()]


async def mark_deleted(
    session: AsyncSession, *, row_id: uuid.UUID, deleted_at: datetime
) -> None:
    await session.execute(
        text(
            """
            UPDATE s3_deletion_queue
            SET deleted_at = :deleted_at
            WHERE id = :id
            """
        ),
        {"id": row_id, "deleted_at": deleted_at},
    )


async def mark_failure(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
    error: str,
    next_attempt_at: datetime,
) -> None:
    await session.execute(
        text(
            """
            UPDATE s3_deletion_queue
            SET attempts = attempts + 1,
                last_error = :error,
                next_attempt_at = :next_attempt_at
            WHERE id = :id
            """
        ),
        {
            "id": row_id,
            "error": error[:2000],
            "next_attempt_at": next_attempt_at,
        },
    )
