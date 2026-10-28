"""Periodic cleanup of expired idempotency keys and refresh tokens."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.repositories import idempotency as idempotency_repo


async def cleanup_expired_in_session(session: AsyncSession) -> dict[str, int]:
    """Delete expired rows; caller commits. Used by the worker and by tests."""
    keys = await idempotency_repo.delete_expired_keys(session)
    tokens = await idempotency_repo.delete_expired_refresh_tokens(session)
    return {"idempotency_keys": keys, "refresh_tokens": tokens}


async def cleanup_expired(_ctx: dict) -> dict[str, int]:
    """arq entrypoint — uses the app engine (DATABASE_URL)."""
    async with AsyncSessionLocal() as session:
        counts = await cleanup_expired_in_session(session)
        await session.commit()
    return counts
