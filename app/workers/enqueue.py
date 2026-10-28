"""Fire-and-forget arq job enqueue helpers (API process → worker)."""

from __future__ import annotations

import logging
import uuid
from urllib.parse import urlparse

from arq import create_pool
from arq.connections import RedisSettings

from app.config import get_settings

logger = logging.getLogger(__name__)


def _redis_settings() -> RedisSettings:
    parsed = urlparse(get_settings().redis_url)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int((parsed.path or "/0").lstrip("/") or "0"),
        password=parsed.password,
    )


async def enqueue_import_csv(job_id: uuid.UUID) -> None:
    """Best-effort enqueue; tests call import_csv directly if Redis has no worker."""
    redis = await create_pool(_redis_settings())
    try:
        await redis.enqueue_job("import_csv", str(job_id))
    except Exception:
        logger.exception("failed to enqueue import_csv job_id=%s", job_id)
        raise
    finally:
        await redis.aclose()
