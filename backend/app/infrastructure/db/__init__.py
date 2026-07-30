"""
Database infrastructure layer.

Provides PostgreSQL connection management and session handling.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
    AsyncEngine,
)
from sqlalchemy.pool import NullPool

logger = structlog.get_logger(__name__)


class DatabaseManager:
    """Manages database connections and sessions."""

    def __init__(
        self,
        database_url: str,
        pool_size: int = 10,
        max_overflow: int = 20,
        echo: bool = False,
    ):
        self.database_url = database_url
        self.pool_size = pool_size
        self.max_overflow = max_overflow
        self.echo = echo
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    def _create_engine(self) -> AsyncEngine:
        """Create async SQLAlchemy engine."""
        # Use asyncpg for PostgreSQL
        if not self.database_url.startswith("postgresql+asyncpg://"):
            logger.warning(
                "db_url_not_async",
                message="Database URL should use postgresql+asyncpg:// prefix",
            )

        engine = create_async_engine(
            self.database_url,
            pool_size=self.pool_size,
            max_overflow=self.max_overflow,
            echo=self.echo,
            poolclass=NullPool,  # Use null pool for serverless deployments
        )

        logger.info(
            "database_engine_created",
            url=self.database_url.split("@")[-1],  # Hide credentials
            pool_size=self.pool_size,
        )

        return engine

    async def initialize(self) -> None:
        """Initialize database connection."""
        if self._engine is None:
            self._engine = self._create_engine()
            self._session_factory = async_sessionmaker(
                self._engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )
            logger.info("database_initialized")

    async def close(self) -> None:
        """Close database connections."""
        if self._engine:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            logger.info("database_disposed")

    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """Get a database session context manager."""
        if self._session_factory is None:
            await self.initialize()

        if self._session_factory is None:
            raise RuntimeError("Database not initialized")

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

    async def health_check(self) -> bool:
        """Check database connectivity."""
        if self._engine is None:
            return False

        try:
            async with self._engine.connect() as conn:
                await conn.execute(asyncio.sleep(0))  # Basic connectivity test
            return True
        except Exception as e:
            logger.error("db_health_check_failed", error=str(e))
            return False


# Global database instance
_db_manager: DatabaseManager | None = None


def get_database_manager() -> DatabaseManager:
    """Get the global database manager instance."""
    if _db_manager is None:
        raise RuntimeError("Database manager not initialized")
    return _db_manager


def init_database(database_url: str, **kwargs) -> DatabaseManager:
    """Initialize the global database manager."""
    global _db_manager
    _db_manager = DatabaseManager(database_url, **kwargs)
    return _db_manager


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Get a database session from the global manager."""
    db_manager = get_database_manager()
    async with db_manager.get_session() as session:
        yield session