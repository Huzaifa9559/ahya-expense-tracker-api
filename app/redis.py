from redis.asyncio import Redis

from app.config import get_settings

_redis: Redis | None = None


async def init_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def get_redis() -> Redis:
    if _redis is None:
        return await init_redis()
    return _redis
