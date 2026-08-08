"""
Read replicas and read routing (Arch 11, P8-3, L2 readiness).

The data plane serves history reads from Postgres read replicas while the
single-writer primary keeps taking conversation appends, config, and spend
(OpenAI's own ChatGPT pattern, Arch 11). This module provides the routing
layer:

- ``ReplicaRouter`` (ManagedService) owns one async engine per replica URL,
  a read-your-writes window, and round-robin selection across healthy
  replicas.
- ``get_read_session(*keys)`` yields a session bound to a replica; when no
  replicas are configured, none are healthy, or ``keys`` were written within
  the read-your-writes window, it falls back to the primary -- so enabling
  replicas is a config change, never a behavior fork.
- The window is an in-process (per-API-replica) approximation at L1: a key
  written by *this* process is routed to the primary for
  ``window_seconds``. Cross-process linearizability for the window moves to
  Redis at L2 (see docs/implementation/read-replicas.md).
- Failure semantics: replica connection failures mark the replica
  unavailable and fall back to the primary (reads never hard-fail because a
  replica is down); health reports DEGRADED, never silent.
"""

import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

from ..patterns import HealthComponent, HealthStatus, ManagedService

logger = structlog.get_logger(__name__)

_MAX_TRACKED_WRITE_KEYS = 10_000


class ReplicaRouter(ManagedService):
    def __init__(
        self,
        replica_urls: list[str],
        *,
        window_seconds: float = 5.0,
        max_pool_size: int = 20,
        max_overflow: int = 10,
        pool_recycle: int = 3600,
        echo: bool = False,
        primary: Any = None,
    ):
        super().__init__("replica_router")
        self.replica_urls = [u for u in (replica_urls or []) if u]
        self.window_seconds = window_seconds
        self.max_pool_size = max_pool_size
        self.max_overflow = max_overflow
        self.pool_recycle = pool_recycle
        self.echo = echo
        self._primary: Any = primary  # DatabaseManager, or resolved lazily
        self._replicas: list[AsyncEngine] = []
        self._factory_by_engine: dict[int, async_sessionmaker[AsyncSession]] = {}
        self._unavailable: set[int] = set()
        self._rr_index = 0
        self._last_write: dict[str, float] = {}

    @property
    def configured(self) -> bool:
        return bool(self.replica_urls)

    def bind_primary(self, db: Any) -> None:
        """Bind the fallback primary for read sessions.

        Repositories bind their own ``DatabaseManager`` so fallback reads
        always hit the same database the caller writes to (tests construct
        their own manager; the process singleton is only a last resort).
        """
        self._primary = db

    def _resolve_primary(self) -> Any:
        if self._primary is None:
            from .manager import get_database_manager

            self._primary = get_database_manager()
        return self._primary

    async def _do_initialize(self) -> None:
        for url in self.replica_urls:
            engine = self._create_replica_engine(url)
            self._replicas.append(engine)
            self._factory_by_engine[id(engine)] = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )
            if await self._ping_engine(engine):
                logger.info("replica_connected", url=url)
            else:
                self._unavailable.add(id(engine))
                logger.error(
                    "replica_unavailable_falling_back_to_primary",
                    url=url,
                    hint="reads for this replica fall back to the primary until it recovers",
                )
        if self._replicas:
            logger.info(
                "replica_router_initialized",
                replicas=len(self._replicas),
                unavailable=len(self._unavailable),
                window_seconds=self.window_seconds,
            )

    def _create_replica_engine(self, url: str) -> AsyncEngine:
        is_sqlite = url.startswith("sqlite")
        kwargs: dict[str, Any] = dict(pool_pre_ping=True, echo=self.echo)
        if is_sqlite:
            # NullPool: no pooling for sqlite; pool kwargs are invalid for
            # the NullPool/aiosqlite combination.
            kwargs["poolclass"] = NullPool
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            kwargs.update(
                pool_size=self.max_pool_size,
                max_overflow=self.max_overflow,
                pool_recycle=self.pool_recycle,
                poolclass=AsyncAdaptedQueuePool,
            )
        return create_async_engine(url, **kwargs)

    async def _ping_engine(self, engine: AsyncEngine) -> bool:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as e:  # noqa: BLE001 - connectivity probe
            logger.debug("replica_ping_failed", url=str(engine.url), error=str(e))
            return False

    async def _do_close(self) -> None:
        for engine in self._replicas:
            await engine.dispose()
        self._replicas = []
        self._factory_by_engine = {}
        self._unavailable = set()

    async def _do_health_check(self) -> HealthComponent:
        if not self.configured:
            return HealthComponent(
                name=self.name,
                status=HealthStatus.HEALTHY,
                metadata={"configured": False, "replicas": 0},
            )
        healthy: list[str] = []
        for engine in self._replicas:
            url = str(engine.url)
            if await self._ping_engine(engine):
                self._unavailable.discard(id(engine))
                healthy.append(url)
            else:
                self._unavailable.add(id(engine))
        if healthy:
            return HealthComponent(
                name=self.name,
                status=HealthStatus.HEALTHY,
                metadata={
                    "configured": True,
                    "replicas": len(self._replicas),
                    "healthy": len(healthy),
                    "unavailable": sorted(
                        str(e.url) for e in self._replicas if id(e) in self._unavailable
                    ),
                },
            )
        return HealthComponent(
            name=self.name,
            status=HealthStatus.DEGRADED,
            message="no healthy read replicas; reads fall back to the primary",
            metadata={
                "configured": True,
                "unavailable": sorted(
                    str(e.url) for e in self._replicas if id(e) in self._unavailable
                ),
            },
        )

    # ---- read-your-writes window -----------------------------------------

    def mark_write(self, *keys: str) -> None:
        """Record a write for ``keys`` so reads route to the primary for
        ``window_seconds`` (read-your-writes consistency)."""
        if not self.configured:
            return
        now = time.monotonic()
        for key in keys:
            if not key:
                continue
            self._last_write[key] = now
        if len(self._last_write) > _MAX_TRACKED_WRITE_KEYS:
            for k in list(self._last_write)[: len(self._last_write) - _MAX_TRACKED_WRITE_KEYS]:
                self._last_write.pop(k, None)

    def _within_window(self, *keys: str) -> bool:
        now = time.monotonic()
        for key in keys:
            if not key:
                continue
            written_at = self._last_write.get(key)
            if written_at is not None and (now - written_at) < self.window_seconds:
                return True
        return False

    def replica_eligible(self, *keys: str) -> bool:
        """True when a replica should serve this read (keys not freshly written)."""
        if not self.configured:
            return False
        if not self._replicas or len(self._unavailable) >= len(self._replicas):
            return False
        return not self._within_window(*keys)

    # ---- read sessions ----------------------------------------------------

    def _next_replica(self) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
        candidates = [
            engine for engine in self._replicas if id(engine) not in self._unavailable
        ]
        if not candidates:
            raise RuntimeError("no healthy replica")
        engine = candidates[self._rr_index % len(candidates)]
        self._rr_index += 1
        return engine, self._factory_by_engine[id(engine)]

    @asynccontextmanager
    async def get_read_session(self, *keys: str) -> AsyncGenerator[AsyncSession]:
        """Read-only session bound to a healthy replica (or the primary).

        ``keys`` are read-your-writes markers: reads for a key written
        within the window stay on the primary. Replica selection is
        round-robin over healthy replicas; a failed replica falls back to
        the primary for the request and is marked unavailable for the next
        health cycle (reads never hard-fail on replica outage).
        """
        if self.replica_eligible(*keys):
            engine, factory = self._next_replica()
            session = factory()
            try:
                if not str(engine.url).startswith("sqlite"):
                    try:
                        await session.connection(
                            execution_options={
                                "postgresql_readonly": True,
                                "postgresql_deferrable": True,
                            }
                        )
                    except Exception as e:  # noqa: BLE001 - advisory only
                        logger.debug(
                            "replica_readonly_option_skipped",
                            url=str(engine.url),
                            error=str(e),
                        )
                yield session
                await session.rollback()  # end the read transaction
            except DBAPIError as e:
                # Connection-level failures mark the replica unavailable and
                # surface the error to the caller (the repository), which
                # logs it; the health cycle self-heals when it recovers.
                self._unavailable.add(id(engine))
                logger.error(
                    "replica_read_failed_marked_unavailable",
                    url=str(engine.url),
                    error=str(e),
                )
                await session.rollback()
                raise
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()
        else:
            async with self._resolve_primary().get_session() as session:
                yield session


_router: ReplicaRouter | None = None


def init_replica_router(
    replica_urls: list[str],
    *,
    window_seconds: float = 5.0,
    **kwargs: Any,
) -> ReplicaRouter:
    global _router
    _router = ReplicaRouter(
        replica_urls,
        window_seconds=window_seconds,
        **kwargs,
    )
    return _router


def get_replica_router() -> ReplicaRouter:
    """Shared router; an unconfigured default never raises and routes
    everything to the primary (L1 shape)."""
    if _router is None:
        return _DEFAULT_ROUTER
    return _router


_DEFAULT_ROUTER = ReplicaRouter([])
