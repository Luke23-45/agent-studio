"""
Quota: USD reservation/reconciliation (Arch 10, P3-6, §6.3.8).

Enforces the budget hierarchy platform > tenant > surface > end-user with
a Redis Lua reservation-and-reconciliation in USD: reserve the estimated
max spend before routing, reconcile the actual spend after completion,
release on failure/cancel. A request is rejected when any level on its
path is over budget. Soft alert at 80%, hard block at 100% of each level's
limit.

Redis is the enforcement fast path (atomic, cross-worker); the durable
``quota_state`` rows are written asynchronously by the cost-ledger worker
from the same spend event (see ``gateway.ledger``), so crash-safe
reconstruction of a window's spend is possible without touching the hot
path. Redis down = enforcement skipped, DEGRADED health, error-level log —
degradation is visible, never silent (codebase convention, P0-9/P0-13).
"""

from __future__ import annotations

import time
import structlog
from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import uuid4

from backend.app.infrastructure.patterns.health import HealthComponent, HealthStatus
from backend.app.infrastructure.patterns.lifecycle import ManagedService

logger = structlog.get_logger(__name__)

_LEVELS = ("platform", "tenant", "surface", "end_user")

_RESERVE_LUA = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local est = tonumber(ARGV[2])
local hash = redis.call('HMGET', key, 'reserved', 'spent')
local reserved = tonumber(hash[1]) or 0
local spent = tonumber(hash[2]) or 0
if limit > 0 and spent + reserved + est > limit then
    return {0, spent + reserved}
end
redis.call('HINCRBYFLOAT', key, 'reserved', est)
return {1, spent + reserved + est}
"""

_RECONCILE_LUA = """
local key = KEYS[1]
local est = tonumber(ARGV[1])
local actual = tonumber(ARGV[2])
local prev = tonumber(redis.call('HGET', key, 'reserved') or 0)
redis.call('HSET', key, 'reserved', tostring(math.max(0, prev - est)))
redis.call('HINCRBYFLOAT', key, 'spent', actual)
return 1
"""


@dataclass(frozen=True)
class QuotaLimits:
    """USD limits per budget level; 0 or None = unlimited level."""

    platform_usd: float = 0.0
    tenant_usd: float = 0.0
    surface_usd: float = 0.0
    end_user_usd: float = 0.0

    def level_limit(self, level: str) -> float:
        return {
            "platform": self.platform_usd,
            "tenant": self.tenant_usd,
            "surface": self.surface_usd,
            "end_user": self.end_user_usd,
        }.get(level, 0.0)


@dataclass
class QuotaConfig:
    redis_url: str = "redis://localhost:6379"
    prefix: str = "neryva:gateway:quota:"
    soft_ratio: float = 0.8
    window_format: str = "%Y-%m"


@dataclass
class QuotaReservation:
    """The reservation state for one request; None-levels mean unlimited.

    ``estimated_usd`` is the max-spend estimate the reservation holds.
    """

    request_id: str
    estimated_usd: float
    levels: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    window: str = ""

    @property
    def limited_levels(self) -> list[str]:
        return list(self.levels)


class QuotaService(ManagedService):
    """Redis Lua USD quota enforcement at the four budget levels."""

    def __init__(self, config: Optional[QuotaConfig] = None):
        super().__init__("quota")
        self.config = config or QuotaConfig()
        self._redis: Any = None
        self._redis_available = False
        self._reserve_script: Any = None
        self._reconcile_script: Any = None
        self._degraded_logged = False

    # -- key layout --------------------------------------------------------

    def _window(self) -> str:
        import datetime

        return datetime.datetime.now(datetime.timezone.utc).strftime(
            self.config.window_format
        )

    def _level_key(self, level: str, tenant_id: str, surface_id: str | None, end_user_id: str | None, window: str) -> str:
        if level == "platform":
            return f"{self.config.prefix}platform:{window}"
        if level == "tenant":
            return f"{self.config.prefix}t:{tenant_id}:{window}"
        if level == "surface":
            return f"{self.config.prefix}t:{tenant_id}:s:{surface_id}:{window}"
        if level == "end_user":
            return f"{self.config.prefix}t:{tenant_id}:u:{end_user_id}:{window}"
        raise ValueError(f"unknown quota level: {level}")

    # -- lifecycle ---------------------------------------------------------

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
            self._reserve_script = self._redis.register_script(_RESERVE_LUA)
            self._reconcile_script = self._redis.register_script(_RECONCILE_LUA)
            self._redis_available = True
            logger.info("quota_connected", url=self.config.redis_url)
        except Exception as e:
            self._redis = None
            self._redis_available = False
            logger.error(
                "quota_redis_unavailable",
                error=str(e),
                hint="quota enforcement is off until Redis recovers (degraded, not silent)",
            )

    async def _do_close(self) -> None:
        if self._redis:
            await self._redis.close()
            self._redis = None
            self._redis_available = False

    # -- enforcement -------------------------------------------------------

    async def reserve(
        self,
        limits: QuotaLimits,
        *,
        tenant_id: str,
        surface_id: str | None = None,
        end_user_id: str | None = None,
        estimated_usd: float = 0.0,
        request_id: str = "",
    ) -> QuotaReservation | None:
        """Reserve estimated max spend across the four levels.

        Returns None when no level is limited or Redis is unavailable
        (enforcement off — DEGRADED health reports it). Raises
        ``QuotaExceededError`` when a level is over budget (prior levels
        are rolled back first). Levels are charged platform -> tenant ->
        surface -> end-user; rejection at any level stops the request.
        """
        from backend.app.gateway.types import GatewayQuotaExceeded

        if estimated_usd <= 0:
            return None
        if not self._redis_available:
            await self._log_degraded_once("redis unavailable")
            return None

        window = self._window()
        request_id = request_id or f"quota-{uuid4()}"
        acquired_levels: list[str] = []
        acquired_keys: list[str] = []
        try:
            for level in _LEVELS:
                limit = limits.level_limit(level)
                if limit <= 0:
                    continue
                key = self._level_key(level, tenant_id, surface_id, end_user_id, window)
                ok, projected = await self._reserve_script(
                    keys=[key], args=[limit, estimated_usd]
                )
                ok = bool(int(ok))
                if not ok:
                    await self._rollback(acquired_keys, estimated_usd)
                    raise GatewayQuotaExceeded(
                        level=level, limit_usd=limit, projected_usd=float(projected)
                    )
                acquired_levels.append(level)
                acquired_keys.append(key)
                if projected >= self.config.soft_ratio * limit:
                    logger.warning(
                        "quota_soft_alert",
                        level=level,
                        limit_usd=limit,
                        projected_usd=float(projected),
                        tenant_id=tenant_id,
                    )
            return QuotaReservation(
                request_id=request_id,
                estimated_usd=estimated_usd,
                levels=acquired_levels,
                keys=acquired_keys,
                window=window,
            )
        except GatewayQuotaExceeded:
            raise
        except Exception as e:
            await self._rollback(acquired_keys, estimated_usd)
            await self._log_degraded_once(f"reserve failed: {e}")
            return None

    async def reconcile(
        self, reservation: QuotaReservation | None, actual_usd: float
    ) -> None:
        """Move the reservation into spent (called after every completion,
        including failures with partial token usage)."""
        if reservation is None or not reservation.keys:
            return
        if not self._redis_available:
            await self._log_degraded_once("redis unavailable")
            return
        try:
            for key in reservation.keys:
                await self._reconcile_script(
                    keys=[key], args=[reservation.estimated_usd, actual_usd]
                )
        except Exception as e:
            await self._log_degraded_once(f"reconcile failed: {e}")

    async def release(self, reservation: QuotaReservation | None) -> None:
        """Return the reservation without spending (failure/cancel path)."""
        if reservation is None or not reservation.keys:
            return
        await self.reconcile(reservation, 0.0)

    async def _rollback(self, keys: list[str], estimated_usd: float) -> None:
        if not self._redis_available or not keys:
            return
        try:
            for key in keys:
                await self._reconcile_script(keys=[key], args=[estimated_usd, 0.0])
        except Exception:
            pass  # rollback is best-effort; the reservation expires with the window

    async def _log_degraded_once(self, reason: str) -> None:
        if not self._degraded_logged:
            self._degraded_logged = True
            logger.error("quota_degraded", reason=reason)

    # -- health ------------------------------------------------------------

    async def _do_health_check(self) -> HealthComponent:
        if not self._redis_available:
            return HealthComponent(
                name=self.name,
                status=HealthStatus.DEGRADED,
                message="Redis unavailable; USD quota enforcement off",
            )
        return HealthComponent(
            name=self.name,
            status=HealthStatus.HEALTHY,
            metadata={
                "redis": True,
                "soft_ratio": self.config.soft_ratio,
                "window_format": self.config.window_format,
            },
        )
