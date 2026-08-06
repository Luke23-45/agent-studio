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

    # Admission control (concurrent in-flight generations per tenant)
    ADMISSION_MAX_CONCURRENT_PER_TENANT: int = 5
    ADMISSION_MAX_CONCURRENT_PLATFORM: int = 50
    ADMISSION_WAIT_SECONDS: float = 5.0
    ADMISSION_LEASE_SECONDS: int = 300

    # Session coordinator (per-thread serialization; queueing budget)
    SESSION_LEASE_SECONDS: int = 120
    SESSION_WAIT_SECONDS: float = 15.0

    # Context assembler (Arch 8.1, P2-1): token reserve kept free for model
    # output when budgeting the prompt (mirrors OpenCode v2's output buffer).
    CONTEXT_OUTPUT_RESERVE_TOKENS: int = 4096

    # Prompt-cache discipline (Arch 8.2, P2-6): when enabled, the assembler
    # marks the stable prefix (system + immutable summary layers) with
    # cache_control metadata; providers that support markers (Anthropic)
    # receive them on the wire, others cache automatically.
    PROMPT_CACHE_MARKERS_ENABLED: bool = True

    # Hot tier (Redis thread tail): TTL = session timeout, tail size
    SESSION_TTL_SECONDS: int = 86400
    SESSION_TAIL_SIZE: int = 10

    # Streaming durability and moderation (Arch 9.1, Phase 4):
    # Server-side chunk buffer TTL — long enough for reconnect replay of a
    # completed turn; short enough not to accumulate idle streams.
    STREAM_BUFFER_TTL_SECONDS: int = 3600
    # Rolling-window output moderation holds this many chars before release.
    # Larger window = stricter context for the validator, more added latency.
    STREAM_MODERATION_WINDOW_CHARS: int = 400
    # SSE keepalive: the connection tier emits a heartbeat frame when no
    # delta has been written for this many seconds (proxy/NAT idle timeouts,
    # P4-1).
    STREAM_HEARTBEAT_SECONDS: int = 15

    # End-user session tokens (Arch 6.4): Fernet bearer tokens, DB-revoked
    SESSION_TOKEN_TTL_SECONDS: int = 43200
    SESSION_TOKEN_ENCRYPTION_KEY: str | None = None
    SESSION_TOKEN_ENCRYPTION_KEY_FILE: str = "session_token.key"

    # End-user abuse limits (Redis; per-user rate/spend/concurrency)
    END_USER_RATE_MAX_REQUESTS: int = 300
    END_USER_RATE_WINDOW_SECONDS: float = 60.0
    END_USER_MAX_CONCURRENT_SESSIONS: int = 5
    END_USER_SESSION_LEASE_SECONDS: int = 3600
    # 0 = unlimited by default; enforce when > 0 (per tenant can override)
    END_USER_SPEND_CAP_TOKENS: int = 0

    # Provider credential encryption (envelope, KMS-ready)
    # Fernet key (urlsafe base64); KMS-injected in production.
    PROVIDER_KEY_ENCRYPTION_KEY: str | None = None
    # Dev fallback: file holding the Fernet key, auto-created on first use.
    PROVIDER_KEY_ENCRYPTION_KEY_FILE: str = "provider_master.key"
    # Platform-managed (global) provider keys allowed as fallback when a
    # tenant has no key row. Production must set False (BYOK only).
    PLATFORM_MANAGED_KEYS_ENABLED: bool = True

    @property
    def is_production(self) -> bool:
        return not self.DEBUG


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()
