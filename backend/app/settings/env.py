"""
Environment settings for Neryva Agent Studio.

Uses pydantic-settings for environment variable management.
"""

from functools import lru_cache
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    APP_NAME: str = "Neryva Agent Studio"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # LLM provider API keys
    OPENAI_API_KEY: str | None = None
    ANTHROPIC_API_KEY: str | None = None
    GOOGLE_API_KEY: str | None = None
    AZURE_API_KEY: str | None = None
    CUSTOM_LLM_API_KEY: str | None = None

    # LLM provider endpoints (azure / custom use OpenAI-compatible APIs)
    AZURE_OPENAI_ENDPOINT: str | None = None
    AZURE_OPENAI_API_VERSION: str = "2024-06-01"
    CUSTOM_LLM_BASE_URL: str | None = None
    CUSTOM_LLM_MODEL: str | None = None

    # Tenant configuration storage
    TENANT_CONFIG_PATH: str = "tenant_configs"

    # Database
    # Dev default is SQLite (zero-config). Production must use Postgres:
    # postgresql+asyncpg://user:pass@host:5432/neryva
    DATABASE_URL: str = "sqlite+aiosqlite:///./neryva.db"
    DB_ECHO: bool = False
    # Migrations are applied explicitly in deployment (alembic upgrade head).
    # Enable only in dev/local environments.
    RUN_MIGRATIONS_ON_STARTUP: bool = False

    # Vector store
    # auto: pgvector when DATABASE_URL is Postgres, in-memory fallback otherwise
    # pgvector | memory: force a specific backend
    VECTOR_STORE: str = "auto"
    VECTOR_STORE_TABLE: str = "embeddings"
    EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"

    # CORS (admin console + widget origins; "*" only for development)
    CORS_ORIGINS: list[str] = ["*"]

    # Redis
    REDIS_URL: str = "redis://localhost:6379"

    # Escalation / handoff
    TICKETING_WEBHOOK_URL: str | None = None

    # Notifications (email via SMTP, SMS via provider webhook)
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM: str | None = None
    SMTP_STARTTLS: bool = True
    SMS_WEBHOOK_URL: str | None = None
    # Operator notifications for escalations (webhook | email | slack | teams).
    # When NOTIFICATION_TARGET is empty, escalation notifications are skipped.
    NOTIFICATION_CHANNEL: str = "webhook"
    NOTIFICATION_TARGET: str | None = None

    # Langfuse
    LANGFUSE_PUBLIC_KEY: str | None = None
    LANGFUSE_SECRET_KEY: str | None = None
    LANGFUSE_HOST: str | None = None

    # Security
    API_KEY_HEADER: str = "X-API-Key"
    # Master switch for API authentication (disable only for local development)
    AUTH_ENABLED: bool = True
    # Bootstrap super-admin key created on first startup when none exist.
    # If unset, a random key is generated and logged once at startup.
    BOOTSTRAP_API_KEY: str | None = None
    # API-level rate limiting per key (in-memory token bucket)
    RATE_LIMIT_MAX_REQUESTS: int = 300
    RATE_LIMIT_WINDOW_SECONDS: float = 60.0

    @property
    def is_production(self) -> bool:
        return not self.DEBUG


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()
