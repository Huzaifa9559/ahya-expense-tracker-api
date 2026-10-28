"""
Drain s3_deletion_queue: delete S3 objects with exponential backoff.

Row locks (FOR UPDATE SKIP LOCKED) are held during S3 calls while the batch
transaction is open — acceptable with small batches. The alternative is a lease
column: claim by setting next_attempt_at forward, commit, then call S3.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import AsyncSessionLocal
from app.repositories import s3_deletion_queue as queue_repo
from app.storage import s3

logger = logging.getLogger(__name__)


def _backoff_minutes(attempts_after_increment: int) -> int:
    # Cap at 60 minutes: least(2^attempts, 60).
    return min(2**attempts_after_increment, 60)


async def process_s3_deletions(ctx: dict, batch_size: int = 50) -> int:
    """Claim due rows, delete_object each; return number successfully marked deleted."""
    session_factory: async_sessionmaker[AsyncSession] = ctx.get(
        "session_factory", AsyncSessionLocal
    )
    deleted = 0

    async with session_factory() as session:
        async with session.begin():
            claimed = await queue_repo.claim_batch(session, batch_size=batch_size)
            if not claimed:
                return 0

            now = datetime.now(UTC)
            for row in claimed:
                try:
                    await s3.delete_object(row["s3_key"])
                    await queue_repo.mark_deleted(
                        session, row_id=row["id"], deleted_at=now
                    )
                    deleted += 1
                except Exception as exc:
                    logger.exception(
                        "s3 delete failed key=%s id=%s", row["s3_key"], row["id"]
                    )
                    next_attempts = int(row["attempts"]) + 1
                    await queue_repo.mark_failure(
                        session,
                        row_id=row["id"],
                        error=f"{type(exc).__name__}: {exc}",
                        next_attempt_at=now
                        + timedelta(minutes=_backoff_minutes(next_attempts)),
                    )

    return deleted
