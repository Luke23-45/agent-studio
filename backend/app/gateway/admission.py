"""
Admission control (Arch 10, OWASP-LLM10, P0-7).

Bounds concurrent in-flight generations per tenant and platform-wide.
Local asyncio semaphores plus a Redis-shared counter per tenant for
multi-worker safety (the counter carries a TTL lease, so a crashed
worker's slots expire instead of leaking). Excess admissions raise
``AdmissionLimitExceeded`` so the route returns 429 with ``Retry-After``.

When Redis is unavailable the gate degrades to in-process semaphores
only — reported as DEGRADED in health checks, never silent.
"""

import asyncio
import structlog
from dataclasses import dataclass
from typing import Any, Optional

from backend.app.infrastructure.patterns.health import HealthComponent, HealthStatus
from backend.app.infrastructure.patterns.lifecycle import ManagedService

logger = structlog.get_logger(__name__)


class AdmissionLimitExceeded(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"admission_limit_exceeded retry_after={retry_after:.0f}s")


@dataclass
class AdmissionConfig:
    redis_url: str = "redis://localhost:6379"
    prefix: str = "neryva:admission:"
    max_concurrent_per_tenant: int = 5
    max_concurrent_platform: int = 50
    wait_seconds: float = 5.0
    lease_seconds: int = 300


class AdmissionHandle:
    """Releases the tenant/platform/Redis slots on exit."""

    def __init__(
        self,
        gate: "ConcurrencyGate",
        tenant_id: str,
        tenant_semaphore: asyncio.Semaphore,
    ):
        self._gate = gate
        self._tenant_id = tenant_id
        self._tenant_semaphore = tenant_semaphore
        self._released = False

    async def __aenter__(self) -> "AdmissionHandle":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.release()

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._gate._release_redis_slot(self._tenant_id)
        self._gate._platform_semaphore.release()
        self._tenant_semaphore.release()


class ConcurrencyGate(ManagedService):
    def __init__(self, config: Optional[AdmissionConfig] = None):
        super().__init__("admission")
        self.config = config or AdmissionConfig()
        self._redis: Any = None
        self._redis_available = False
        self._platform_semaphore = asyncio.Semaphore(self.config.max_concurrent_platform)
        self._tenant_semaphores: dict[str, asyncio.Semaphore] = {}

    async def _do_initialize(self) -> None:
        try:
            import redis.asyncio as redis

            self._redis = redis.from_url(
                self.config.redis_url, decode_responses=True, retry_on_timeout=True
            )
            await self._redis.ping()
            self._redis_available = True
            logger.info("admission_redis_connected", url=self.config.redis_url)
        except Exception as e:
            self._redis = None
            self._redis_available = False
            logger.warning("admission_redis_unavailable_in_process_only", error=str(e))

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
                message="Redis unavailable; in-process admission only",
            )
        return HealthComponent(
            name=self.name,
            status=HealthStatus.HEALTHY,
            metadata={
                "redis": True,
                "max_concurrent_per_tenant": self.config.max_concurrent_per_tenant,
                "max_concurrent_platform": self.config.max_concurrent_platform,
            },
        )

    def _tenant_semaphore(self, tenant_id: str) -> asyncio.Semaphore:
        sem = self._tenant_semaphores.get(tenant_id)
        if sem is None:
            sem = asyncio.Semaphore(self.config.max_concurrent_per_tenant)
            self._tenant_semaphores[tenant_id] = sem
        return sem

    async def _acquire_redis_slot(self, tenant_id: str) -> bool:
        if not self._redis_available:
            return True
        key = f"{self.config.prefix}{tenant_id}"
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, self.config.lease_seconds)
            count = (await pipe.execute())[0]
        if count > self.config.max_concurrent_per_tenant:
            await self._redis.decr(key)
            return False
        return True

    async def _release_redis_slot(self, tenant_id: str) -> None:
        if not self._redis_available:
            return
        key = f"{self.config.prefix}{tenant_id}"
        await self._redis.decr(key)

    async def admit(self, tenant_id: str) -> AdmissionHandle:
        """Acquire local semaphores and a Redis slot; raise 429 on excess."""
        tenant_sem = self._tenant_semaphore(tenant_id)
        try:
            await asyncio.wait_for(tenant_sem.acquire(), timeout=self.config.wait_seconds)
        except asyncio.TimeoutError:
            raise AdmissionLimitExceeded(self.config.wait_seconds) from None

        try:
            await asyncio.wait_for(
                self._platform_semaphore.acquire(), timeout=self.config.wait_seconds
            )
        except asyncio.TimeoutError:
            tenant_sem.release()
            raise AdmissionLimitExceeded(self.config.wait_seconds) from None

        if not await self._acquire_redis_slot(tenant_id):
            self._platform_semaphore.release()
            tenant_sem.release()
            raise AdmissionLimitExceeded(1.0)

        return AdmissionHandle(self, tenant_id, tenant_sem)


_admission_gate: Optional[ConcurrencyGate] = None


def init_admission(redis_url: str, **kwargs: Any) -> ConcurrencyGate:
    """Initialize the admission gate singleton (called from app lifespan)."""
    global _admission_gate
    config = AdmissionConfig(redis_url=redis_url, **kwargs)
    _admission_gate = ConcurrencyGate(config)
    return _admission_gate


def get_admission_gate() -> ConcurrencyGate:
    if _admission_gate is None:
        raise RuntimeError("Admission gate not initialized")
    return _admission_gate
