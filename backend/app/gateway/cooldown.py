"""
Resilience (Arch 10, P3-4): two distinct mechanisms, mirroring LiteLLM's
verified design (the architecture's §10 correction):

1. ``RedisCooldownCache`` — per-deployment (provider+model) cooldown state
   in Redis, shared across replicas (LiteLLM ``cooldown_cache.py``
   pattern): ``allowed_fails`` failures within ``cooldown_time`` mark the
   deployment cooling down; the router skips it. This is the Redis-shared
   circuit state v2 wanted from "the v1.48.0 fix" — real, versionless,
   and ours.

2. ``RedisDependencyBreaker`` — protects the *gateway itself* from a
   failing Redis (LiteLLM v1.82.0 semantics): after N consecutive Redis
   failures the breaker opens and Redis-dependent features fast-fail at
   0ms instead of hanging on timeouts; a half-open probe after the
   recovery timeout lets Redis prove it is back. While OPEN the gateway
   still serves LLM calls (cooldown checks fail open) but reports
   DEGRADED — never silently.

Both are ManagedServices: Redis down at startup degrades gracefully,
health is reported honestly, and both self-heal on reconnection.
"""

from __future__ import annotations

import asyncio
import time
import structlog
from dataclasses import dataclass
from typing import Any, Optional

from backend.app.infrastructure.patterns.health import HealthComponent, HealthStatus
from backend.app.infrastructure.patterns.lifecycle import ManagedService

logger = structlog.get_logger(__name__)

_COOLDOWN_FAIL_LUA = """
local key = KEYS[1]
local cooldown_ms = tonumber(ARGV[1])
local count = redis.call('INCR', key)
if count == 1 then
    redis.call('PEXPIRE', key, cooldown_ms)
end
return count
"""


@dataclass
class CooldownConfig:
    redis_url: str = "redis://localhost:6379"
    prefix: str = "neryva:gateway:cooldown:"
    allowed_fails: int = 5
    cooldown_time_seconds: float = 60.0


class RedisCooldownCache(ManagedService):
    """Redis-shared per-deployment failure cooldown (Arch 10, P3-4)."""

    def __init__(self, config: Optional[CooldownConfig] = None):
        super().__init__("gateway_cooldowns")
        self.config = config or CooldownConfig()
        self._redis: Any = None
        self._redis_available = False
        self._fail_script: Any = None
        self._degraded_logged = False

    def _key(self, provider: str, model: str) -> str:
        return f"{self.config.prefix}{provider.strip().lower()}:{model.strip().lower()}"

    async def _do_initialize(self) -> None:
        try:
            import redis.asyncio as redis

            self._redis = redis.from_url(
                self.config.redis_url,
                decode_responses=True,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
                retry_on_timeout=True,
            )
            await self._redis.ping()
            self._fail_script = self._redis.register_script(_COOLDOWN_FAIL_LUA)
            self._redis_available = True
            logger.info("gateway_cooldowns_connected", url=self.config.redis_url)
        except Exception as e:
            self._redis = None
            self._redis_available = False
            logger.warning("gateway_cooldowns_redis_unavailable", error=str(e))

    async def _do_close(self) -> None:
        if self._redis:
            await self._redis.close()
            self._redis = None
            self._redis_available = False

    async def record_failure(self, provider: str, model: str) -> int:
        """INCR the deployment's failure counter; returns the new count."""
        if not self._redis_available or self._redis is None:
            return self.config.allowed_fails + 1
        try:
            count = await self._fail_script(
                keys=[self._key(provider, model)],
                args=[int(self.config.cooldown_time_seconds * 1000)],
            )
            return int(count)
        except Exception as e:
            await self._log_degraded_once(e)
            return self.config.allowed_fails + 1

    async def record_success(self, provider: str, model: str) -> None:
        """Reset the deployment's failure counter on success."""
        if not self._redis_available or self._redis is None:
            return
        try:
            await self._redis.delete(self._key(provider, model))
        except Exception as e:
            await self._log_degraded_once(e)

    async def is_available(self, provider: str, model: str) -> bool:
        """True when the deployment is not cooling down. Redis down = true
        (availability checks fail open; degradation is reported, not
        silent)."""
        if not self._redis_available or self._redis is None:
            return True
        try:
            value = await self._redis.get(self._key(provider, model))
            if value is None:
                return True
            return int(value) < self.config.allowed_fails
        except Exception as e:
            await self._log_degraded_once(e)
            return True

    async def snapshot(self) -> dict[str, int]:
        """Current failure counts for every deployment, keyed
        ``"provider:model"`` (P7-4 circuit-breaker console)."""
        if not self._redis_available or self._redis is None:
            return {}
        try:
            keys = await self._redis.keys(f"{self.config.prefix}*")
            if not keys:
                return {}
            values = await self._redis.mget(keys)
            snapshot: dict[str, int] = {}
            for key, value in zip(keys, values):
                if value is None:
                    continue
                name = key[len(self.config.prefix):]
                provider, _, model = name.partition(":")
                snapshot[f"{provider}:{model}"] = int(value)
            return snapshot
        except Exception as e:
            await self._log_degraded_once(e)
            return {}

    async def reset(
        self,
        provider: str | None = None,
        model: str | None = None,
    ) -> int:
        """Clear cooldown state. Scoped to one deployment when both
        provider/model are given, otherwise clears every matching key
        (provider-scoped when only provider is given). Returns the number
        of deployments cleared."""
        if not self._redis_available or self._redis is None:
            return 0
        try:
            if provider and model:
                await self._redis.delete(self._key(provider, model))
                return 1
            pattern = f"{self.config.prefix}*"
            if provider:
                pattern = f"{self.config.prefix}{provider.strip().lower()}:*"
            keys = await self._redis.keys(pattern)
            if not keys:
                return 0
            return int(await self._redis.delete(*keys))
        except Exception as e:
            await self._log_degraded_once(e)
            return 0

    async def cooldown_ttl(self, provider: str, model: str) -> float | None:
        """Remaining cooldown for a deployment in seconds, or None when the
        deployment has no failure record (Redis down also returns None)."""
        if not self._redis_available or self._redis is None:
            return None
        try:
            ttl_ms = await self._redis.pttl(self._key(provider, model))
            if ttl_ms is None or ttl_ms < 0:
                return None
            return ttl_ms / 1000.0
        except Exception as e:
            await self._log_degraded_once(e)
            return None

    async def _log_degraded_once(self, error: Exception) -> None:
        if not self._degraded_logged:
            self._degraded_logged = True
            logger.error(
                "gateway_cooldowns_degraded",
                error=str(error),
                hint="cooldown checks fail open while Redis is unavailable",
            )

    async def _do_health_check(self) -> HealthComponent:
        if not self._redis_available:
            return HealthComponent(
                name=self.name,
                status=HealthStatus.DEGRADED,
                message="Redis unavailable; cooldown enforcement off",
            )
        return HealthComponent(
            name=self.name,
            status=HealthStatus.HEALTHY,
            metadata={
                "redis": True,
                "allowed_fails": self.config.allowed_fails,
                "cooldown_time_seconds": self.config.cooldown_time_seconds,
            },
        )


@dataclass
class DependencyBreakerConfig:
    failure_threshold: int = 5
    recovery_timeout_seconds: float = 60.0


class RedisDependencyBreaker(ManagedService):
    """Protects the gateway from a failing Redis (LiteLLM v1.82.0 pattern).

    Every Redis operation in the gateway reports through
    ``record_redis_failure`` / ``record_redis_success``. After
    ``failure_threshold`` consecutive failures the breaker opens: gateway
    Redis features fast-fail (``fast_fail``) at 0ms. After
    ``recovery_timeout_seconds`` a half-open probe re-pings Redis; a
    success closes the breaker, a failure reopens it. State is reported in
    health checks (DEGRADED while open) — never silent.
    """

    def __init__(self, config: Optional[DependencyBreakerConfig] = None):
        super().__init__("gateway_redis_breaker")
        self.config = config or DependencyBreakerConfig()
        self._consecutive_failures = 0
        self._open_since: float = 0.0
        self._lock = asyncio.Lock()
        self._probe_ok: bool | None = None

    @property
    def is_open(self) -> bool:
        if self._consecutive_failures < self.config.failure_threshold:
            return False
        if time.monotonic() - self._open_since < self.config.recovery_timeout_seconds:
            return True
        return not self._probe_ok

    @property
    def fast_fail(self) -> bool:
        """Callers skip Redis entirely when the breaker is open (0ms)."""
        return self.is_open

    async def record_redis_failure(self, error: Exception) -> None:
        async with self._lock:
            was_open = self.is_open
            self._consecutive_failures += 1
            self._probe_ok = None
            if self._consecutive_failures >= self.config.failure_threshold and not was_open:
                self._open_since = time.monotonic()
                logger.error(
                    "gateway_redis_breaker_opened",
                    failures=self._consecutive_failures,
                    error=str(error),
                    hint="gateway Redis features fast-fail until Redis recovers",
                )

    async def record_redis_success(self) -> None:
        async with self._lock:
            if self._consecutive_failures >= self.config.failure_threshold:
                logger.info("gateway_redis_breaker_closed")
            self._consecutive_failures = 0
            self._probe_ok = None
            self._open_since = 0.0

    async def probe(self, ping: Any) -> bool:
        """Half-open probe: ping Redis once; True closes the breaker."""
        try:
            await ping()
            await self.record_redis_success()
            return True
        except Exception as e:
            async with self._lock:
                self._probe_ok = False
                self._open_since = time.monotonic()
            logger.warning("gateway_redis_breaker_probe_failed", error=str(e))
            return False

    async def _do_health_check(self) -> HealthComponent:
        return HealthComponent(
            name=self.name,
            status=(
                HealthStatus.HEALTHY
                if not self.is_open
                else HealthStatus.DEGRADED
            ),
            message=(
                "Redis healthy"
                if not self.is_open
                else "Redis breaker OPEN; gateway Redis features fast-fail"
            ),
            metadata={
                "consecutive_failures": self._consecutive_failures,
                "threshold": self.config.failure_threshold,
                "recovery_timeout_seconds": self.config.recovery_timeout_seconds,
            },
        )

    async def _do_initialize(self) -> None:
        pass

    async def _do_close(self) -> None:
        pass
