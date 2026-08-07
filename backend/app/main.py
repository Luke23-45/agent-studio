"""
Neryva Agent Studio - Backend Application

This is the main entry point for the FastAPI application.
"""

import structlog
from contextlib import asynccontextmanager
from pathlib import Path

import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.api.dependencies.auth import (
    generate_api_key,
    hash_api_key,
    get_rate_limiter,
    initialize_rate_limiter,
)
from backend.app.api.middleware import (
    CorrelationIdMiddleware,
    ErrorHandlingMiddleware,
    LoggingMiddleware,
)
from backend.app.api.routes import (
    conversations_router,
    model_catalog_router,
    operations_router,
    sessions_router,
    surfaces_router,
    threads_router,
    webhooks_router,
)
from backend.app.gateway.admission import get_admission_gate, init_admission
from backend.app.gateway.service import get_gateway, init_gateway
from backend.app.infrastructure.cache import get_cache_manager, init_cache
from backend.app.infrastructure.db import (
    ApiKeyRepository,
    AuditRepository,
    get_database_manager,
    init_database,
)
from backend.app.infrastructure.queue import get_queue_manager, init_queue
from backend.app.infrastructure.storage import get_storage_manager, init_storage
from backend.app.infrastructure.stream import get_stream_buffer, init_stream_buffer
from backend.app.modules.tenant_config import configure_tenant_config_service
from backend.app.session.coordinator import get_thread_coordinator, init_thread_coordinator
from backend.app.session.hot_tier import get_thread_tail_cache, init_thread_tail_cache
from backend.app.session.limits import get_end_user_limits, init_end_user_limits
from backend.app.session.tokens import get_session_token_service, init_session_token_service
from backend.app.settings.env import settings

logger = structlog.get_logger(__name__)


async def _bootstrap_api_key() -> None:
    """Create a super-admin key on first startup when none exist."""
    if not settings.AUTH_ENABLED:
        logger.info("auth_disabled_bootstrap_skipped")
        return

    db = get_database_manager()
    repo = ApiKeyRepository(db)

    if await repo.count_active() > 0:
        return

    if settings.BOOTSTRAP_API_KEY:
        raw = settings.BOOTSTRAP_API_KEY
        prefix = raw[:24]
        key_hash = hash_api_key(raw)
    else:
        raw, prefix, key_hash = generate_api_key()

    await repo.create(
        name="bootstrap",
        key_hash=key_hash,
        prefix=prefix,
        role="super_admin",
    )
    await AuditRepository(db).add(
        action="api_key.created",
        resource_type="api_key",
        actor_type="system",
        details={"name": "bootstrap", "role": "super_admin"},
    )

    if settings.BOOTSTRAP_API_KEY:
        logger.info("bootstrap_api_key_configured", prefix=prefix)
    else:
        logger.warning(
            "bootstrap_api_key_generated",
            key=raw,
            message="Store this key securely. It will not be shown again.",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: wire up shared services at startup."""
    configure_tenant_config_service(Path(settings.TENANT_CONFIG_PATH))

    db = init_database(settings.DATABASE_URL, echo=settings.DB_ECHO)
    await db.initialize()
    if settings.RUN_MIGRATIONS_ON_STARTUP:
        await db.run_migrations()
    await _bootstrap_api_key()

    # Cache / queue / storage managers (Redis/S3 unavailable degrades to
    # in-memory / local-fs fallbacks; startup never fails on external deps)
    cache = init_cache(settings.REDIS_URL)
    queue = init_queue(settings.REDIS_URL)
    storage = init_storage()
    admission = init_admission(
        settings.REDIS_URL,
        max_concurrent_per_tenant=settings.ADMISSION_MAX_CONCURRENT_PER_TENANT,
        max_concurrent_platform=settings.ADMISSION_MAX_CONCURRENT_PLATFORM,
        wait_seconds=settings.ADMISSION_WAIT_SECONDS,
        lease_seconds=settings.ADMISSION_LEASE_SECONDS,
    )
    coordinator = init_thread_coordinator(
        settings.REDIS_URL,
        lease_seconds=settings.SESSION_LEASE_SECONDS,
        wait_seconds=settings.SESSION_WAIT_SECONDS,
    )
    tail_cache = init_thread_tail_cache(
        settings.REDIS_URL,
        ttl_seconds=settings.SESSION_TTL_SECONDS,
        tail_size=settings.SESSION_TAIL_SIZE,
    )
    init_session_token_service(db)
    stream_buffer = init_stream_buffer(
        settings.REDIS_URL,
        ttl_seconds=settings.STREAM_BUFFER_TTL_SECONDS,
        db=db,
        overflow_max_chunks=settings.STREAM_BUFFER_OVERFLOW_CHUNKS,
    )
    end_user_limits = init_end_user_limits(
        settings.REDIS_URL,
        max_requests=settings.END_USER_RATE_MAX_REQUESTS,
        window_seconds=settings.END_USER_RATE_WINDOW_SECONDS,
        max_concurrent_sessions=settings.END_USER_MAX_CONCURRENT_SESSIONS,
        session_lease_seconds=settings.END_USER_SESSION_LEASE_SECONDS,
        spend_cap_tokens=settings.END_USER_SPEND_CAP_TOKENS,
    )
    # P3-9: the LLM gateway (cooldowns, quota, cache, ledger) — the only
    # path to providers. Redis-backed pieces degrade to in-memory fallbacks
    # exactly like the other managers.
    gateway = init_gateway(db=db, queue=queue)
    await asyncio.gather(
        cache.initialize(),
        queue.initialize(),
        storage.initialize(),
        admission.initialize(),
        coordinator.initialize(),
        tail_cache.initialize(),
        stream_buffer.initialize(),
        end_user_limits.initialize(),
        gateway.initialize(),
        initialize_rate_limiter(),
    )

    logger.info(
        "application_startup",
        app=settings.APP_NAME,
        debug=settings.DEBUG,
        tenant_config_path=settings.TENANT_CONFIG_PATH,
        database_url=settings.DATABASE_URL,
        auth_enabled=settings.AUTH_ENABLED,
    )
    yield
    await asyncio.gather(
        db.close(),
        cache.close(),
        queue.close(),
        storage.close(),
        admission.close(),
        coordinator.close(),
        tail_cache.close(),
        stream_buffer.close(),
        end_user_limits.close(),
        gateway.close(),
    )
    logger.info("application_shutdown")


def create_application() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Neryva Agent Studio",
        description="Governance and control layer for LLM applications",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # Register middleware (order: correlation id outermost, error handling next)
    app.add_middleware(ErrorHandlingMiddleware)
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    # CORS for the admin console and widget origins.
    # "*" is the dev default; production deployments must set explicit
    # CORS_ORIGINS (credentials are only allowed with explicit origins).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_origin_regex=None,
        allow_credentials=("*" not in settings.CORS_ORIGINS),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routes
    app.include_router(conversations_router, prefix="/api/v1")
    app.include_router(sessions_router, prefix="/api/v1")
    app.include_router(surfaces_router, prefix="/api/v1")
    app.include_router(threads_router, prefix="/api/v1")
    app.include_router(webhooks_router, prefix="/api/v1")
    app.include_router(model_catalog_router, prefix="/api/v1")
    app.include_router(operations_router, prefix="/api/v1")

    @app.get("/health/live")
    async def liveness_check():
        """Liveness: process + ASGI reachable, no external dependencies."""
        return {"status": "alive"}

    @app.get("/health")
    async def health_check():
        from backend.app.adapters.tracing import get_langfuse_adapter
        from backend.app.infrastructure.cache import get_cache_manager
        from backend.app.infrastructure.queue import get_queue_manager
        from backend.app.infrastructure.storage import get_storage_manager
        from backend.app.modules.guardrails.config import GuardrailsModuleConfig
        from backend.app.modules.guardrails.errors import GuardrailConfigurationError
        from backend.app.settings.feature_flags import feature_flags

        components: dict[str, dict] = {}

        db = get_database_manager()
        dbc = await db.health_check()
        components["database"] = dbc.to_dict()

        # Managed infrastructure: each degrades to an in-process fallback
        # (memory / local fs) when its backend is unavailable, which the
        # endpoint reports honestly as DEGRADED.
        for name, manager in (
            ("queue", get_queue_manager()),
            ("cache", get_cache_manager()),
            ("storage", get_storage_manager()),
            ("admission", get_admission_gate()),
            ("rate_limiter", get_rate_limiter()),
            ("session_coordinator", get_thread_coordinator()),
            ("thread_tail_cache", get_thread_tail_cache()),
            ("stream_buffer", get_stream_buffer()),
            ("end_user_limits", get_end_user_limits()),
            ("gateway", get_gateway()),
        ):
            components[name] = (await manager.health_check()).to_dict()

        # Guardrails: configuration must validate (fail-closed contract).
        # Engines are lazy per-tenant; config validity is the startup check.
        try:
            GuardrailsModuleConfig().validate()
            components["guardrails"] = {
                "name": "guardrails",
                "status": "HEALTHY",
                "metadata": {
                    "nemo": feature_flags.ENABLE_NEMO_GUARDRAILS,
                    "guardrails_ai": feature_flags.ENABLE_GUARDRAILS_AI,
                    "presidio": feature_flags.ENABLE_PRESIDIO,
                },
            }
        except GuardrailConfigurationError as e:
            components["guardrails"] = {
                "name": "guardrails",
                "status": "UNHEALTHY",
                "message": str(e),
            }

        adapter = get_langfuse_adapter()
        # No credentials = no-op tracing (dev default), not a failure.
        # UNHEALTHY is reserved for flag on + credentials present + client
        # construction failing.
        if not feature_flags.ENABLE_LANGFUSE_TRACING or not adapter.is_enabled:
            langfuse_status = "HEALTHY"
            langfuse_message = ""
        elif adapter._get_client() is not None:
            langfuse_status = "HEALTHY"
            langfuse_message = ""
        else:
            langfuse_status = "UNHEALTHY"
            langfuse_message = "tracing client unavailable"
        components["langfuse"] = {
            "name": "langfuse",
            "status": langfuse_status,
            "message": langfuse_message,
            "metadata": {
                "enabled": adapter.is_enabled,
                "tracing_flag": feature_flags.ENABLE_LANGFUSE_TRACING,
            },
        }

        _severity = {"HEALTHY": 0, "DEGRADED": 1, "UNHEALTHY": 2}
        overall = max(_severity.get(c["status"], 2) for c in components.values())
        status = "healthy" if overall == 0 else "degraded" if overall == 1 else "unhealthy"
        return {
            "status": status,
            "database": components["database"]["status"].lower(),
            "auth_enabled": settings.AUTH_ENABLED,
            "components": components,
        }

    return app


app = create_application()


def run() -> None:
    """Console-script entrypoint for the API server."""
    import uvicorn

    uvicorn.run(
        "backend.app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level="info",
    )


if __name__ == "__main__":
    run()
