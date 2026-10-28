import asyncio

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()

# Precomputed once so unknown-email logins still pay argon2 verify cost (timing).
_DUMMY_HASH = _hasher.hash("__not-a-real-password-for-timing__")


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


def _verify_sync(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


async def verify_password(password_hash: str, password: str) -> bool:
    return await asyncio.to_thread(_verify_sync, password_hash, password)


async def verify_dummy_password(password: str) -> bool:
    """Burn CPU on a fixed hash so missing users match real-user timing."""
    return await asyncio.to_thread(_verify_sync, _DUMMY_HASH, password)
