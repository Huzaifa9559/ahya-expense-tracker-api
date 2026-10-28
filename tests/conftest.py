import os
from collections.abc import AsyncIterator, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from moto.server import ThreadedMotoServer
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings
from app.db import get_db
from app.main import create_app
from app.redis import get_redis

ROOT = Path(__file__).resolve().parents[1]
_MOTO_PORT = 35566

# Truncate every public table (including exchange_rates). Not a wrapping transaction —
# asyncio.gather concurrency tests need committed rows visible across connections.
_TRUNCATE_SQL = text(
    """
    DO $$
    DECLARE
        stmts text;
    BEGIN
        SELECT coalesce(
            string_agg(
                format('TRUNCATE TABLE %I.%I RESTART IDENTITY CASCADE', schemaname, tablename),
                '; '
            ),
            ''
        )
        INTO stmts
        FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename <> 'alembic_version';

        IF stmts <> '' THEN
            EXECUTE stmts;
        END IF;
    END $$;
    """
)


@pytest.fixture(scope="session")
def moto_s3() -> Iterator[str]:
    """
    Session-scoped ThreadedMotoServer; points Settings at it and creates the bucket.
    All S3 I/O goes through app.storage.s3 (aioboto3) against this endpoint.
    """
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=_MOTO_PORT, verbose=False)
    server.start()
    endpoint = f"http://127.0.0.1:{_MOTO_PORT}"
    os.environ["S3_ENDPOINT_URL"] = endpoint
    os.environ["S3_PUBLIC_ENDPOINT"] = endpoint
    get_settings.cache_clear()
    settings = get_settings()
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region,
    )
    try:
        client.create_bucket(Bucket=settings.s3_bucket)
    except client.exceptions.BucketAlreadyOwnedByYou:
        pass
    yield endpoint
    server.stop()
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def settings(moto_s3):
    return get_settings()


@pytest.fixture(scope="session")
def apply_migrations(settings) -> None:
    """Run Alembic against the test DB once per session (exercises real migrations)."""
    os.environ["ALEMBIC_DATABASE_URL"] = settings.test_database_url
    get_settings.cache_clear()
    cfg = Config(str(ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")


@pytest_asyncio.fixture(scope="session")
async def engine(settings, apply_migrations) -> AsyncEngine:
    # pool_size >= 5: asyncio.gather concurrency tests need multiple connections.
    eng = create_async_engine(
        settings.test_database_url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
    )
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(scope="session")
async def session_factory(engine: AsyncEngine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(scope="session")
async def redis_client(settings) -> Redis:
    client = Redis.from_url(settings.test_redis_url, decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture(autouse=True)
async def clean_state(engine: AsyncEngine, redis_client: Redis):
    async with engine.begin() as conn:
        await conn.execute(_TRUNCATE_SQL)
    await redis_client.flushdb()
    yield


@pytest_asyncio.fixture
async def rates(engine: AsyncEngine) -> dict:
    """
    Small deterministic rates for expense/report tests.

    Includes a known gap day (2026-03-03 has no EUR row) so as-of LATERAL
    fallback can be asserted with exact numbers. Also seeds the current UTC
    month so GET /categories "this month" spend tests have as-of rates.
    """
    from datetime import UTC, datetime

    rows = [
        ("USD", Decimal("1.00000000"), date(2026, 3, 1)),
        ("USD", Decimal("1.00000000"), date(2026, 3, 2)),
        ("USD", Decimal("1.00000000"), date(2026, 3, 3)),
        ("USD", Decimal("1.00000000"), date(2026, 3, 4)),
        ("EUR", Decimal("1.10000000"), date(2026, 3, 1)),
        ("EUR", Decimal("1.10000000"), date(2026, 3, 2)),
        # gap: no EUR on 2026-03-03
        ("EUR", Decimal("1.20000000"), date(2026, 3, 4)),
        ("PKR", Decimal("0.00350000"), date(2026, 3, 1)),
        ("PKR", Decimal("0.00350000"), date(2026, 3, 2)),
        ("PKR", Decimal("0.00350000"), date(2026, 3, 3)),
        ("PKR", Decimal("0.00350000"), date(2026, 3, 4)),
        ("AED", Decimal("0.27000000"), date(2026, 3, 1)),
        ("AED", Decimal("0.27000000"), date(2026, 3, 2)),
        ("AED", Decimal("0.27000000"), date(2026, 3, 3)),
        ("AED", Decimal("0.27000000"), date(2026, 3, 4)),
    ]

    today = datetime.now(UTC).date()
    month_start = date(today.year, today.month, 1)
    # Seed first-of-month + today for each currency (as-of covers the whole month).
    current_month_rates = {
        "USD": Decimal("1.00000000"),
        "EUR": Decimal("1.10000000"),
        "PKR": Decimal("0.00350000"),
        "AED": Decimal("0.27000000"),
    }
    for currency, rate in current_month_rates.items():
        rows.append((currency, rate, month_start))
        if today != month_start:
            rows.append((currency, rate, today))

    async with engine.begin() as conn:
        for currency, rate, valid_on in rows:
            await conn.execute(
                text(
                    """
                    INSERT INTO exchange_rates (currency, rate_to_usd, valid_on)
                    VALUES (:currency, :rate, :valid_on)
                    ON CONFLICT (currency, valid_on) DO NOTHING
                    """
                ),
                {"currency": currency, "rate": rate, "valid_on": valid_on},
            )
    return {
        "gap_day": date(2026, 3, 3),
        "eur_before_gap": Decimal("1.10000000"),
        "eur_after_gap": Decimal("1.20000000"),
        "anchor": date(2026, 3, 1),
        "window_end": date(2026, 3, 4),
        "today": today,
        "month_start": month_start,
    }


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[AsyncClient]:
    """HTTP client wired to the test DB + Redis (not the app process defaults)."""
    app = create_app()

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def _override_redis() -> Redis:
        return redis_client

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
