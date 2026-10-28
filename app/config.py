from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "ahya-expense-tracker-api"

    database_url: str = Field(
        default="postgresql+asyncpg://ahya:ahya@localhost:5432/expense_tracker"
    )
    test_database_url: str = Field(
        default="postgresql+asyncpg://ahya:ahya@localhost:5432/expense_tracker_test"
    )

    redis_url: str = Field(default="redis://localhost:6379/0")
    test_redis_url: str = Field(default="redis://localhost:6379/15")

    # Placeholder only — override via JWT_SECRET in .env. Never a real production value.
    jwt_secret: str = Field(default="dev-only-not-a-real-secret")
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7

    s3_endpoint_url: str = "http://localhost:9000"
    # Used only when building presigned URLs for clients outside the Docker network.
    s3_public_endpoint: str = "http://localhost:9000"
    s3_access_key_id: str = "test"
    s3_secret_access_key: str = "test"
    s3_bucket: str = "ahya-receipts"
    s3_region: str = "us-east-1"

    cors_origins: list[str] = Field(default_factory=lambda: ["*"])


@lru_cache
def get_settings() -> Settings:
    return Settings()
