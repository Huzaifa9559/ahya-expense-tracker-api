import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings
from app.main import create_app

# Tables appear in later phases; truncate whatever exists in public schema.
_TRUNCATE_SQL = text(
    """
    DO $$
    DECLARE
        stmts text;
    BEGIN
        SELECT coalesce(
            string_agg(format('TRUNCATE TABLE %I.%I RESTART IDENTITY CASCADE', schemaname, tablename), '; '),
            ''
        )
        INTO stmts
        FROM pg_tables
        WHERE schemaname = 'public';

        IF stmts <> '' THEN
            EXECUTE stmts;
        END IF;
    END $$;
    """
)


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest_asyncio.fixture(scope="session")
async def engine(settings) -> AsyncEngine:
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
    """
    Clean Postgres + Redis between tests.

    Not a wrapping transaction: that would break asyncio.gather concurrency tests
    that need committed rows visible across connections.
    """
    async with engine.begin() as conn:
        await conn.execute(_TRUNCATE_SQL)
    await redis_client.flushdb()
    yield


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
