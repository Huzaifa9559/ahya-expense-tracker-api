import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt

from app.config import Settings


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_refresh_token() -> tuple[str, str]:
    """Return (raw_opaque_token, sha256_hex)."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_refresh_token(raw)


def create_access_token(*, user_id: uuid.UUID, settings: Settings) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
        "iat": now,
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str, settings: Settings) -> uuid.UUID:
    """Decode and validate an access JWT. Raises jwt.PyJWTError on failure."""
    payload = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        options={"require": ["exp", "sub", "type"]},
    )
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("not an access token")
    return uuid.UUID(payload["sub"])
