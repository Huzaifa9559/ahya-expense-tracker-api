import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RefreshToken


async def create(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    token_hash: str,
    family_id: uuid.UUID,
    expires_at: datetime,
) -> RefreshToken:
    row = RefreshToken(
        user_id=user_id,
        token_hash=token_hash,
        family_id=family_id,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_by_hash(session: AsyncSession, token_hash: str) -> RefreshToken | None:
    result = await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    return result.scalar_one_or_none()


async def rotate(
    session: AsyncSession,
    *,
    old_id: uuid.UUID,
    new_id: uuid.UUID,
) -> uuid.UUID | None:
    """
    Atomically revoke old token and point it at the replacement.
    Returns old id if this caller won the race; None if already revoked.
    """
    result = await session.execute(
        text(
            """
            UPDATE refresh_tokens
            SET revoked_at = now(), replaced_by = :new_id
            WHERE id = :old_id AND revoked_at IS NULL
            RETURNING id
            """
        ),
        {"new_id": new_id, "old_id": old_id},
    )
    row = result.first()
    return row[0] if row else None


async def revoke_all_for_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def revoke_one(session: AsyncSession, token: RefreshToken) -> None:
    if token.revoked_at is None:
        token.revoked_at = datetime.now(UTC)
        await session.flush()
