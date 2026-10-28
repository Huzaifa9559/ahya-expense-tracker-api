"""arq worker settings."""

from urllib.parse import urlparse

from arq import cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.workers.cleanup import cleanup_expired


def _redis_settings() -> RedisSettings:
    parsed = urlparse(get_settings().redis_url)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int((parsed.path or "/0").lstrip("/") or "0"),
        password=parsed.password,
    )


async def ping(_ctx: dict) -> str:
    """Health probe so the worker always has ≥1 function."""
    return "pong"


class WorkerSettings:
    functions = [ping, cleanup_expired]
    redis_settings = _redis_settings()
    # Hourly sweep of expired idempotency keys and refresh tokens.
    cron_jobs = [cron(cleanup_expired, hour=None, minute={0})]
