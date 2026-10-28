"""arq worker settings."""

from urllib.parse import urlparse

from arq import cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.workers.cleanup import cleanup_expired
from app.workers.import_csv import import_csv
from app.workers.outbox import process_outbox_batch, purge_processed_outbox


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
    functions = [
        ping,
        cleanup_expired,
        process_outbox_batch,
        purge_processed_outbox,
        import_csv,
    ]
    redis_settings = _redis_settings()
    cron_jobs = [
        # Hourly sweep of expired idempotency keys and refresh tokens.
        cron(cleanup_expired, hour=None, minute={0}),
        # Drain outbox every 5 seconds.
        cron(process_outbox_batch, second={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        # Daily retention of processed outbox rows older than 7 days.
        cron(purge_processed_outbox, hour={3}, minute={15}),
    ]
