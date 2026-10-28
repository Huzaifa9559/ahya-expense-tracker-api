"""arq worker settings. Real jobs are added in later phases."""

from urllib.parse import urlparse

from arq.connections import RedisSettings

from app.config import get_settings


def _redis_settings() -> RedisSettings:
    parsed = urlparse(get_settings().redis_url)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int((parsed.path or "/0").lstrip("/") or "0"),
        password=parsed.password,
    )


async def ping(_ctx: dict) -> str:
    """Placeholder so arq accepts an empty Phase 0 worker (needs ≥1 function)."""
    return "pong"


class WorkerSettings:
    functions = [ping]
    redis_settings = _redis_settings()
    cron_jobs: list = []
