"""
Session coordinator (Arch 7.2, P1-3).

Per-thread serialization: at most one in-flight generation per thread.
Waiters queue in sequence order — locally via a per-thread asyncio lock,
across processes via a Redis lock with a TTL lease (SET NX PX + compare-
and-delete release + background renewal). If the holder dies, the lease
expires and the next waiter proceeds (crash-safe). Cancellation releases
the slot through the lease context manager. When Redis is unavailable the
coordinator degrades to local-only serialization — visible in health
checks, never silent.
"""

import asyncio
import structlog
import time
from dataclasses import dataclass
from typing import Any, Optional
from uuid import uuid4

from backend.app.infrastructure.patterns.health import HealthComponent, HealthStatus
from backend.app.infrastructure.patterns.lifecycle import ManagedService

logger = structlog.get_logger(__name__)

_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

_RENEW_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
else
    return 0
end
"""


class CoordinatorBusy(Exception):
    """Could not acquire the thread slot within the wait budget."""


@dataclass
class CoordinatorConfig:
    redis_url: str = "redis://localhost:6379"
    prefix: str = "neryva:threadlock:"
    lease_seconds: int = 120
    wait_seconds: float = 15.0


class CoordinatorLease:
    """Holds the per-thread slot; releases on exit (idempotent)."""

    def __init__(
        self,
        coordinator: "ThreadCoordinator",
        key: str,
        token: str,
        local_lock: asyncio.Lock,
        redis_held: bool,
    ):
        self._coordinator = coordinator
        self._key = key
        self._token = token
        self._local_lock = local_lock
        self._redis_held = redis_held
        self._renewal_task: asyncio.Task | None = None
        self._released = False

    async def __aenter__(self) -> "CoordinatorLease":
        if self._redis_held:
            self._renewal_task = asyncio.create_task(
                self._coordinator._renew_loop(self._key, self._token)
            )
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.release()

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._renewal_task is not None:
            self._renewal_task.cancel()
            try:
                await self._renewal_task
            except asyncio.CancelledError:
                pass
        if self._redis_held:
            await self._coordinator._release_redis_lock(self._key, self._token)
        self._local_lock.release()


class ThreadCoordinator(ManagedService):
    def __init__(self, config: Optional[CoordinatorConfig] = None):
        super().__init__("session_coordinator")
        self.config = config or CoordinatorConfig()
        self._redis: Any = None
        self._redis_available = False
        self._release_script: Any = None
        self._renew_script: Any = None
        self._local_locks: dict[str, asyncio.Lock] = {}

    @property
    def degraded(self) -> bool:
        return not self._redis_available

    def _lock_key(self, tenant_id: str, thread_id: str) -> str:
        return f"{self.config.prefix}{tenant_id}:{thread_id}"

    def _local_lock(self, key: str) -> asyncio.Lock:
        lock = self._local_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._local_locks[key] = lock
        return lock

    async def _do_initialize(self) -> None:
        try:
            import redis.asyncio as redis

            self._redis = redis.from_url(
                self.config.redis_url,
                decode_responses=True,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
            )
            await self._redis.ping()
            self._release_script = self._redis.register_script(_RELEASE_LUA)
            self._renew_script = self._redis.register_script(_RENEW_LUA)
            self._redis_available = True
            logger.info("session_coordinator_redis_connected", url=self.config.redis_url)
        except Exception as e:
            self._redis = None
            self._redis_available = False
            logger.error(
                "session_coordinator_redis_unavailable_local_only",
                error=str(e),
                hint="per-thread serialization is local-only until Redis recovers",
            )

    async def _do_close(self) -> None:
        if self._redis:
            await self._redis.close()
            self._redis = None
            self._redis_available = False

    async def _do_health_check(self) -> HealthComponent:
        if not self._redis_available:
            return HealthComponent(
                name=self.name,
                status=HealthStatus.DEGRADED,
                message="Redis unavailable; per-thread serialization is local-only",
            )
        return HealthComponent(
            name=self.name,
            status=HealthStatus.HEALTHY,
            metadata={"lease_seconds": self.config.lease_seconds},
        )

    async def _renew_loop(self, key: str, token: str) -> None:
        interval = max(1.0, self.config.lease_seconds / 3.0)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._renew_script(
                    keys=[key], args=[token, self.config.lease_seconds * 1000]
                )
                if not int(renewed):
                    logger.warning("thread_lock_lease_lost", key=key)
                    return
            except Exception as e:
                logger.warning("thread_lock_renewal_failed", key=key, error=str(e))
                return

    async def _release_redis_lock(self, key: str, token: str) -> None:
        if not self._redis_available:
            return
        try:
            await self._release_script(keys=[key], args=[token])
        except Exception as e:
            logger.warning("thread_lock_release_failed", key=key, error=str(e))

    async def _acquire_redis_lock(
        self, key: str, token: str, deadline: float
    ) -> bool:
        while time.monotonic() < deadline:
            try:
                acquired = await self._redis.set(
                    key, token, nx=True, px=self.config.lease_seconds * 1000
                )
                if acquired:
                    return True
            except Exception as e:
                if self._redis_available:
                    self._redis_available = False
                    logger.error(
                        "session_coordinator_redis_down_local_only",
                        key=key,
                        error=str(e),
                    )
                return True  # degrade: local lock only
            await asyncio.sleep(0.05)
        return False

    async def acquire(
        self,
        tenant_id: str,
        thread_id: str,
        timeout: float | None = None,
    ) -> CoordinatorLease:
        """Acquire the per-thread slot; raises CoordinatorBusy on timeout."""
        key = self._lock_key(tenant_id, thread_id)
        local_lock = self._local_lock(key)
        wait = timeout if timeout is not None else self.config.wait_seconds
        deadline = time.monotonic() + wait

        try:
            await asyncio.wait_for(local_lock.acquire(), timeout=wait)
        except asyncio.TimeoutError:
            raise CoordinatorBusy(
                f"thread busy: {thread_id} (local queue full within {wait:.0f}s)"
            ) from None

        token = str(uuid4())
        redis_held = await self._acquire_redis_lock(key, token, deadline)
        if not redis_held:
            local_lock.release()
            raise CoordinatorBusy(
                f"thread busy: {thread_id} (locked by another worker)"
            )
        return CoordinatorLease(self, key, token, local_lock, redis_held)


_coordinator: Optional[ThreadCoordinator] = None


def init_thread_coordinator(redis_url: str, **kwargs: Any) -> ThreadCoordinator:
    """Initialize the coordinator singleton (called from app lifespan)."""
    global _coordinator
    config = CoordinatorConfig(redis_url=redis_url, **kwargs)
    _coordinator = ThreadCoordinator(config)
    return _coordinator


def get_thread_coordinator() -> ThreadCoordinator:
    if _coordinator is None:
        raise RuntimeError("Session coordinator not initialized")
    return _coordinator
