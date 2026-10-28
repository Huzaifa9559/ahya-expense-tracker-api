import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

_LOGIN_FAIL_PREFIX = "login:fail:"
_WINDOW_SECONDS = 900

# INCR + conditional EXPIRE in one round-trip (multi-server safe).
_INCR_EXPIRE_LUA = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return c
"""


def login_fail_key(email_lower: str) -> str:
    return f"{_LOGIN_FAIL_PREFIX}{email_lower}"


async def get_failure_count(redis: Redis, email_lower: str) -> int | None:
    """
    Return current failure count, or None if Redis is unavailable (fail open).
    """
    try:
        raw = await redis.get(login_fail_key(email_lower))
        return int(raw) if raw is not None else 0
    except RedisError:
        logger.warning("Redis unavailable during login rate-limit check; failing open")
        return None


async def get_retry_after_seconds(redis: Redis, email_lower: str) -> int:
    try:
        ttl = await redis.ttl(login_fail_key(email_lower))
        if ttl is None or ttl < 1:
            return 1
        return int(ttl)
    except RedisError:
        return _WINDOW_SECONDS


async def record_login_failure(redis: Redis, email_lower: str) -> int | None:
    """Atomically INCR (+ EXPIRE on first hit). Returns new count, or None if Redis down."""
    try:
        count = await redis.eval(
            _INCR_EXPIRE_LUA, 1, login_fail_key(email_lower), str(_WINDOW_SECONDS)
        )
        return int(count)
    except RedisError:
        logger.warning("Redis unavailable while recording login failure; failing open")
        return None


async def clear_login_failures(redis: Redis, email_lower: str) -> None:
    try:
        await redis.delete(login_fail_key(email_lower))
    except RedisError:
        logger.warning("Redis unavailable while clearing login failures; failing open")
