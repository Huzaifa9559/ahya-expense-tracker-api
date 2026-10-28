import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import User
from app.repositories import exchange_rates as rates_repo
from app.repositories import refresh_tokens as token_repo
from app.repositories import users as users_repo
from app.schemas.auth import TokenResponse, UserResponse
from app.security import passwords, rate_limit, tokens

_INVALID_CREDENTIALS = "Invalid email or password"
_INVALID_REFRESH = "Invalid refresh token"
_INVALID_ACCESS = "Could not validate credentials"


def _user_response(user: User) -> UserResponse:
    return UserResponse(
        id=str(user.id),
        email=user.email,
        base_currency=user.base_currency,
        created_at=user.created_at.isoformat(),
    )


def unauthorized(detail: str = _INVALID_ACCESS) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _rate_limited(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many failed login attempts. Try again later.",
        headers={"Retry-After": str(max(1, retry_after))},
    )


async def register(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    base_currency: str,
) -> UserResponse:
    email_l = email.lower().strip()
    currency = base_currency.upper().strip()

    if not await rates_repo.currency_exists(session, currency):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="base_currency is not a known currency",
        )

    if await users_repo.get_by_email(session, email_l) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    password_hash = await passwords.hash_password(password)
    try:
        user = await users_repo.create_user(
            session,
            email=email_l,
            password_hash=password_hash,
            base_currency=currency,
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        ) from None

    return _user_response(user)


async def _issue_token_pair(
    session: AsyncSession,
    *,
    user: User,
    settings: Settings,
    family_id: uuid.UUID | None = None,
) -> TokenResponse:
    family = family_id or uuid.uuid4()
    raw_refresh, refresh_hash = tokens.generate_refresh_token()
    expires_at = datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days)
    await token_repo.create(
        session,
        user_id=user.id,
        token_hash=refresh_hash,
        family_id=family,
        expires_at=expires_at,
    )
    access = tokens.create_access_token(user_id=user.id, settings=settings)
    return TokenResponse(access_token=access, refresh_token=raw_refresh)


async def login(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    email: str,
    password: str,
) -> TokenResponse:
    email_l = email.lower().strip()

    # Check BEFORE argon2 so locked accounts don't pay hash cost.
    count = await rate_limit.get_failure_count(redis, email_l)
    if count is not None and count >= 5:
        retry_after = await rate_limit.get_retry_after_seconds(redis, email_l)
        raise _rate_limited(retry_after)

    user = await users_repo.get_by_email(session, email_l)
    if user is None:
        await passwords.verify_dummy_password(password)
        await rate_limit.record_login_failure(redis, email_l)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_CREDENTIALS,
        )

    ok = await passwords.verify_password(user.password_hash, password)
    if not ok:
        await rate_limit.record_login_failure(redis, email_l)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_CREDENTIALS,
        )

    await rate_limit.clear_login_failures(redis, email_l)
    pair = await _issue_token_pair(session, user=user, settings=settings)
    await session.commit()
    return pair


async def refresh(
    session: AsyncSession,
    settings: Settings,
    *,
    raw_refresh: str,
) -> TokenResponse:
    token_hash = tokens.hash_refresh_token(raw_refresh)
    existing = await token_repo.get_by_hash(session, token_hash)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_INVALID_REFRESH)

    now = datetime.now(UTC)
    expires = existing.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)

    # Expired is not theft — just reject.
    if expires < now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_INVALID_REFRESH)

    # Already revoked/replaced → treat as reuse / theft.
    if existing.revoked_at is not None:
        await token_repo.revoke_all_for_user(session, existing.user_id)
        await session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_INVALID_REFRESH)

    user = await users_repo.get_by_id(session, existing.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_INVALID_REFRESH)

    new_raw, new_hash = tokens.generate_refresh_token()
    new_expires = datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days)
    new_row = await token_repo.create(
        session,
        user_id=user.id,
        token_hash=new_hash,
        family_id=existing.family_id,
        expires_at=new_expires,
    )

    won = await token_repo.rotate(session, old_id=existing.id, new_id=new_row.id)
    if won is None:
        # Lost the race (or concurrent reuse): revoke everything for this user.
        await token_repo.revoke_all_for_user(session, existing.user_id)
        await session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_INVALID_REFRESH)

    access = tokens.create_access_token(user_id=user.id, settings=settings)
    await session.commit()
    return TokenResponse(access_token=access, refresh_token=new_raw)


async def logout(session: AsyncSession, *, raw_refresh: str) -> None:
    token_hash = tokens.hash_refresh_token(raw_refresh)
    existing = await token_repo.get_by_hash(session, token_hash)
    if existing is not None:
        await token_repo.revoke_one(session, existing)
    await session.commit()


async def get_user_from_access_token(
    session: AsyncSession,
    settings: Settings,
    token: str,
) -> User:
    try:
        user_id = tokens.decode_access_token(token, settings)
    except Exception:
        raise unauthorized() from None

    user = await users_repo.get_by_id(session, user_id)
    if user is None:
        raise unauthorized()
    return user
