import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IdempotencyKey


async def delete_expired_for_key(
    session: AsyncSession, *, user_id: uuid.UUID, key: str
) -> None:
    """Treat an expired key as absent so the next INSERT can own it again."""
    await session.execute(
        text(
            """
            DELETE FROM idempotency_keys
            WHERE user_id = :uid AND key = :key AND expires_at < now()
            """
        ),
        {"uid": user_id, "key": key},
    )


async def try_claim(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    key: str,
    request_hash: str,
    expires_at: datetime,
) -> uuid.UUID | None:
    """
    INSERT ... ON CONFLICT DO NOTHING RETURNING id.

    Race control: if another transaction already inserted the same (user_id, key)
    and has not committed yet, Postgres blocks this INSERT until that transaction
    ends. After the wait: winner committed → RETURNING empty (we SELECT + replay);
    winner rolled back → our INSERT succeeds and we own the key.
    """
    result = await session.execute(
        text(
            """
            INSERT INTO idempotency_keys (
                user_id, key, request_hash, expires_at
            ) VALUES (
                :uid, :key, :request_hash, :expires_at
            )
            ON CONFLICT (user_id, key) DO NOTHING
            RETURNING id
            """
        ),
        {
            "uid": user_id,
            "key": key,
            "request_hash": request_hash,
            "expires_at": expires_at,
        },
    )
    row = result.first()
    return row[0] if row else None


async def get_by_user_key(
    session: AsyncSession, *, user_id: uuid.UUID, key: str
) -> IdempotencyKey | None:
    result = await session.execute(
        select(IdempotencyKey).where(
            IdempotencyKey.user_id == user_id,
            IdempotencyKey.key == key,
        )
    )
    return result.scalar_one_or_none()


async def store_response(
    session: AsyncSession,
    *,
    key_id: uuid.UUID,
    response_status: int,
    response_body: dict[str, Any],
) -> None:
    await session.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.id == key_id)
        .values(response_status=response_status, response_body=response_body)
    )


async def delete_expired_keys(session: AsyncSession) -> int:
    result = await session.execute(
        text("DELETE FROM idempotency_keys WHERE expires_at < now()")
    )
    return result.rowcount or 0


async def delete_expired_refresh_tokens(session: AsyncSession) -> int:
    result = await session.execute(
        text("DELETE FROM refresh_tokens WHERE expires_at < now()")
    )
    return result.rowcount or 0
