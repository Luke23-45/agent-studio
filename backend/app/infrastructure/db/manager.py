import structlog
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine, AsyncEngine
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

from ..patterns import ManagedService, HealthComponent, HealthStatus
from ..patterns.retry import AsyncRetry, RetryConfig, ExponentialBackoff

logger = structlog.get_logger(__name__)


@dataclass
class DatabaseConfig:
    database_url: str
    min_pool_size: int = 5
    max_pool_size: int = 20
    max_overflow: int = 10
    pool_timeout: float = 30.0
    pool_recycle: int = 3600
    echo: bool = False
    retry_max_attempts: int = 3
    retry_min_delay: float = 0.5
    statement_timeout_ms: int = 30000
    use_null_pool: bool = False


class DatabaseManager(ManagedService):
    def __init__(self, config: DatabaseConfig):
        super().__init__("database")
        self.config = config
        self._engine: Optional[AsyncEngine] = None
        self._session_factory: Optional[async_sessionmaker[AsyncSession]] = None
        self._retry = AsyncRetry(RetryConfig(
            max_attempts=config.retry_max_attempts,
            backoff=ExponentialBackoff(min_delay=config.retry_min_delay),
        ))

    async def _do_initialize(self) -> None:
        self._engine = self._create_engine()
        self._session_factory = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        await self._verify_connectivity()
        logger.info("database_initialized",
                    pool_size=self.config.max_pool_size,
                    recycle=self.config.pool_recycle)

    def _create_engine(self) -> AsyncEngine:
        is_sqlite = self.config.database_url.startswith("sqlite")
        if not is_sqlite and not self.config.database_url.startswith("postgresql+asyncpg://"):
            logger.warning(
                "db_url_not_async",
                message="Database URL should use postgresql+asyncpg:// prefix",
            )

        poolclass = NullPool if self.config.use_null_pool else AsyncAdaptedQueuePool
        kwargs: Dict[str, Any] = dict(
            pool_size=self.config.max_pool_size,
            max_overflow=self.config.max_overflow,
            pool_timeout=self.config.pool_timeout,
            pool_recycle=self.config.pool_recycle,
            pool_pre_ping=True,
            echo=self.config.echo,
            poolclass=poolclass,
        )
        if is_sqlite:
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            kwargs["connect_args"] = {
                "statement_timeout": self.config.statement_timeout_ms,
                "command_timeout": self.config.statement_timeout_ms // 1000,
            }
        engine = create_async_engine(self.config.database_url, **kwargs)
        return engine

    async def _verify_connectivity(self) -> None:
        if not self._engine:
            return
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            logger.info("database_connectivity_verified")
        except Exception as e:
            logger.error("database_connectivity_failed", error=str(e))
            raise

    async def _do_close(self) -> None:
        if self._engine:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            logger.info("database_disposed")

    def get_session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Return the session factory for out-of-request-context tasks."""
        if self._session_factory is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._session_factory

    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        if self._session_factory is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        session = self._session_factory()
        try:
            yield session
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error("db_session_error", error=str(e))
            raise
        finally:
            await session.close()

    async def execute(self, statement: Any, params: Optional[Dict[str, Any]] = None) -> Any:
        async with self.get_session() as session:
            result = await session.execute(statement, params or {})
            return result

    async def fetch_all(self, statement: Any, params: Optional[Dict[str, Any]] = None) -> List[Any]:
        async with self.get_session() as session:
            result = await session.execute(statement, params or {})
            return list(result.fetchall())

    async def fetch_one(self, statement: Any, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        async with self.get_session() as session:
            result = await session.execute(statement, params or {})
            return result.fetchone()

    async def _do_health_check(self) -> HealthComponent:
        if not self._engine:
            return HealthComponent(name=self.name, status=HealthStatus.UNHEALTHY, message="Engine not initialized")

        try:
            start = time.monotonic()
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            latency = time.monotonic() - start

            pool_status = {
                "size": self._engine.pool.size() if hasattr(self._engine.pool, "size") else "N/A",
                "checked_in": self._engine.pool.checkedin() if hasattr(self._engine.pool, "checkedin") else "N/A",
                "checked_out": self._engine.pool.checkedout() if hasattr(self._engine.pool, "checkedout") else "N/A",
                "overflow": self._engine.pool.overflow() if hasattr(self._engine.pool, "overflow") else "N/A",
            }

            return HealthComponent(
                name=self.name,
                status=HealthStatus.HEALTHY if latency < 1.0 else HealthStatus.DEGRADED,
                message=f"Query latency: {latency*1000:.1f}ms",
                metadata={**pool_status, "latency_ms": latency * 1000},
            )
        except Exception as e:
            return HealthComponent(
                name=self.name,
                status=HealthStatus.UNHEALTHY,
                message=str(e),
            )

    async def run_migrations(self, migration_path: Optional[str] = None) -> None:
        """Apply pending Alembic migrations. Safe to call on every startup."""
        if not self._engine:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        from alembic import command
        from alembic.config import Config

        backend_root = Path(__file__).resolve().parents[3]
        alembic_root = Path(migration_path) if migration_path else backend_root
        ini_path = alembic_root / "alembic.ini"
        if not ini_path.exists():
            logger.warning(
                "alembic_ini_not_found",
                path=str(ini_path),
                message="Skipping migrations; no alembic.ini present.",
            )
            return

        cfg = Config(str(ini_path))
        cfg.set_main_option("script_location", str(alembic_root / "alembic"))
        cfg.set_main_option(
            "sqlalchemy.url",
            self.config.database_url.replace("+aiosqlite", "").replace("+asyncpg", ""),
        )
        command.upgrade(cfg, "head")
        logger.info("migrations_applied")


_db_manager: Optional[DatabaseManager] = None


def get_database_manager() -> DatabaseManager:
    if _db_manager is None:
        raise RuntimeError("Database manager not initialized")
    return _db_manager


def init_database(database_url: str, **kwargs) -> DatabaseManager:
    global _db_manager
    config = DatabaseConfig(database_url=database_url, **kwargs)
    _db_manager = DatabaseManager(config)
    return _db_manager


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    db = get_database_manager()
    async with db.get_session() as session:
        yield session
