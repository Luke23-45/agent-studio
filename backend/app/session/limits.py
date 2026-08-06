"""
End-user abuse limits (Arch 6.4, P1-8).

Per end-user (within a tenant): request rate (token bucket, shared script
with the API-key limiter), concurrent-session lease counter, and spend
cap (Redis float counter; durable spend lives in spend_events). Every
limit is fail-open on Redis failure with an alert-level log and a
DEGRADED health component — availability never depends on the limiter.
"""

import structlog
from dataclasses import dataclass
from typing import Any, Optional

from backend.app.infrastructure.patterns.health import HealthComponent, HealthStatus
from backend.app.infrastructure.patterns.lifecycle import ManagedService
from backend.app.infrastructure.patterns.rate_limiter import (
    RateLimitConfig,
    RateLimiter,
)

logger = structlog.get_logger(__name__)


@dataclass
class EndUserLimitsConfig:
    redis_url: str = "redis://localhost:6379"
    max_requests: int = 300
    window_seconds: float = 60.0
    max_concurrent_sessions: int = 5
    session_lease_seconds: int = 3600
    spend_cap_tokens: int = 0  # 0 = unlimited
    prefix: str = "neryva:eu:"


class EndUserLimits(ManagedService):
    def __init__(self, config: Optional[EndUserLimitsConfig] = None):
        super().__init__("end_user_limits")
        self.config = config or EndUserLimitsConfig()
        self._redis: Any = None
        self._redis_available = False
        self._limiter = RateLimiter(
            RateLimitConfig(
                max_requests=self.config.max_requests,
                window_seconds=self.config.window_seconds,
                key_prefix="neryva:rl:eu:",
            ),
            redis_url=self.config.redis_url,
        )

    @property
    def degraded(self) -> bool:
        return not self._redis_available or self._limiter.degraded

    def _sessions_key(self, tenant_id: str, end_user_id: str) -> str:
        return f"{self.config.prefix}sessions:{tenant_id}:{end_user_id}"

    def _spend_key(self, tenant_id: str, end_user_id: str) -> str:
        return f"{self.config.prefix}spend:{tenant_id}:{end_user_id}"

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
            self._redis_available = True
            logger.info("end_user_limits_redis_connected", url=self.config.redis_url)
        except Exception as e:
            self._redis = None
            self._redis_available = False
            logger.error(
                "end_user_limits_redis_unavailable_fail_open",
                error=str(e),
                hint="per-user limits are disabled until Redis recovers (fail-open)",
            )
        await self._limiter.initialize()

    async def _do_close(self) -> None:
        if self._redis:
            await self._redis.close()
            self._redis = None
            self._redis_available = False

    async def _do_health_check(self) -> HealthComponent:
        if self.degraded:
            return HealthComponent(
                name=self.name,
                status=HealthStatus.DEGRADED,
                message="Redis unavailable; end-user limits fail-open",
            )
        return HealthComponent(name=self.name, status=HealthStatus.HEALTHY)

    # ---- rate ---------------------------------------------------------

    async def check_rate(self, tenant_id: str, end_user_id: str, tokens: int = 1) -> bool:
        return await self._limiter.acquire(f"{tenant_id}:{end_user_id}", tokens)

    # ---- concurrent sessions ------------------------------------------

    async def enter_session(self, tenant_id: str, end_user_id: str) -> bool:
        """Increment the user's active-session lease; False when over cap."""
        if not self._redis_available:
            return True  # fail-open
        key = self._sessions_key(tenant_id, end_user_id)
        try:
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(
                    key, self.config.session_lease_seconds
                )
            if self.config.max_concurrent_sessions > 0 and count > self.config.max_concurrent_sessions:
                await self._redis.decr(key)
                return False
            return True
        except Exception as e:
            if self._redis_available:
                self._redis_available = False
                logger.error("end_user_limits_redis_down_fail_open", error=str(e))
            return True

    async def leave_session(self, tenant_id: str, end_user_id: str) -> None:
        if not self._redis_available:
            return
        key = self._sessions_key(tenant_id, end_user_id)
        try:
            count = await self._redis.decr(key)
            if count <= 0:
                await self._redis.delete(key)
        except Exception as e:
            if self._redis_available:
                self._redis_available = False
                logger.error("end_user_limits_redis_down_fail_open", error=str(e))

    # ---- spend cap ----------------------------------------------------

    async def record_spend(self, tenant_id: str, end_user_id: str, tokens: float) -> None:
        if not self._redis_available:
            return
        key = self._spend_key(tenant_id, end_user_id)
        try:
            await self._redis.incrbyfloat(key, tokens)
            await self._redis.expire(key, self.config.session_lease_seconds)
        except Exception as e:
            if self._redis_available:
                self._redis_available = False
                logger.error("end_user_limits_redis_down_fail_open", error=str(e))

    async def check_spend_cap(self, tenant_id: str, end_user_id: str) -> bool:
        """True when the user is under the cap (or no cap configured)."""
        if self.config.spend_cap_tokens <= 0:
            return True
        if not self._redis_available:
            return True  # fail-open
        key = self._spend_key(tenant_id, end_user_id)
        try:
            raw = await self._redis.get(key)
            used = float(raw or 0.0)
            return used < self.config.spend_cap_tokens
        except Exception as e:
            if self._redis_available:
                self._redis_available = False
                logger.error("end_user_limits_redis_down_fail_open", error=str(e))
            return True


_limits: Optional[EndUserLimits] = None


def init_end_user_limits(redis_url: str, **kwargs: Any) -> EndUserLimits:
    global _limits
    config = EndUserLimitsConfig(redis_url=redis_url, **kwargs)
    _limits = EndUserLimits(config)
    return _limits


def get_end_user_limits() -> EndUserLimits:
    if _limits is None:
        raise RuntimeError("End-user limits not initialized")
    return _limits
