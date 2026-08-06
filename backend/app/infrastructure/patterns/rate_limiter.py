import asyncio
import structlog
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.app.infrastructure.patterns.health import (
    HealthComponent,
    HealthStatus,
)

logger = structlog.get_logger(__name__)

# Distributed token bucket (single key -> Redis Cluster safe). Refills
# continuously at max/window; returns {allowed, remaining}.
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local max = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local now = tonumber(ARGV[4])

local bucket = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(bucket[1])
local ts = tonumber(bucket[2])

if tokens == nil or ts == nil or now - ts >= window then
    tokens = max
else
    tokens = math.min(max, tokens + (now - ts) * (max / window))
end

if tokens >= cost then
    tokens = tokens - cost
    redis.call('HMSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
    redis.call('PEXPIRE', key, math.floor(window * 1000))
    return {1, tokens}
else
    redis.call('HMSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
    redis.call('PEXPIRE', key, math.floor(window * 1000))
    return {0, tokens}
end
"""


class RateLimitExceeded(Exception):
    def __init__(self, key: str, limit: int, reset_at: float):
        self.key = key
        self.limit = limit
        self.reset_at = reset_at
        super().__init__(f"Rate limit exceeded for '{key}': {limit} per window, resets at {reset_at}")


@dataclass
class RateLimitConfig:
    max_requests: int = 100
    window_seconds: float = 60.0
    burst_multiplier: float = 1.5
    key_prefix: str = "neryva:rl:"


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class RateLimiter:
    """Distributed token-bucket rate limiter with a visible fallback.

    Redis path (P0-9): a Lua token bucket shared across worker processes,
    refilling continuously and Cluster-safe (one key per bucket). When
    Redis is unavailable or fails, the limiter degrades to the per-process
    token bucket — never taking the API down — but the degradation is
    always visible: an alert-level log on the transition and a DEGRADED
    health component (never silent).
    """

    def __init__(self, config: RateLimitConfig, redis_url: Optional[str] = None):
        self.config = config
        self._redis_url = redis_url
        self._redis: Any = None
        self._script: Any = None
        self._buckets: Dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()
        self._degraded = False

    @property
    def degraded(self) -> bool:
        """True while the limiter runs on the per-process fallback."""
        return self._degraded

    async def initialize(self) -> None:
        if not self._redis_url:
            return
        try:
            import redis.asyncio as redis
            self._redis = redis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
            )
            await self._redis.ping()
            self._script = self._redis.register_script(_TOKEN_BUCKET_LUA)
            self._degraded = False
            logger.info("rate_limiter_initialized", url=self._redis_url)
        except Exception as e:
            self._redis = None
            self._script = None
            self._degraded = True
            logger.error(
                "rate_limiter_redis_unavailable_degraded",
                url=self._redis_url,
                error=str(e),
                hint="rate limits are per-process until Redis recovers",
            )

    async def _try_redis_reconnect(self) -> None:
        """Self-heal: re-ping once while degraded; clear the flag on success."""
        if not self._redis or not self._degraded:
            return
        try:
            await self._redis.ping()
            self._script = self._redis.register_script(_TOKEN_BUCKET_LUA)
            self._degraded = False
            logger.info("rate_limiter_recovered", url=self._redis_url)
        except Exception:
            pass

    def _key(self, key: str) -> str:
        return f"{self.config.key_prefix}{key}"

    async def acquire(self, key: str, tokens: int = 1) -> bool:
        if self._redis:
            await self._try_redis_reconnect()
            try:
                return await self._redis_acquire(key, tokens)
            except Exception as e:
                if not self._degraded:
                    self._degraded = True
                    logger.error(
                        "rate_limiter_redis_down_degraded",
                        key=key,
                        error=str(e),
                        hint="rate limits are per-process until Redis recovers",
                    )
        return await self._memory_acquire(key, tokens)

    async def _redis_acquire(self, key: str, tokens: int = 1) -> bool:
        now_ms = time.time() * 1000.0
        allowed, _remaining = await self._script(
            keys=[self._key(key)],
            args=[
                self.config.max_requests,
                self.config.window_seconds,
                tokens,
                now_ms,
            ],
        )
        # decode_responses=True: elements arrive as strings.
        return int(allowed) == 1

    async def _memory_acquire(self, key: str, tokens: int = 1) -> bool:
        async with self._lock:
            now = time.monotonic()
            bucket = self._buckets.get(key)

            if bucket is None:
                bucket = _Bucket(tokens=self.config.max_requests, last_refill=now)
                self._buckets[key] = bucket

            elapsed = now - bucket.last_refill
            refill = elapsed * (self.config.max_requests / self.config.window_seconds)
            bucket.tokens = min(
                bucket.tokens + refill,
                self.config.max_requests * self.config.burst_multiplier,
            )
            bucket.last_refill = now

            if bucket.tokens >= tokens:
                bucket.tokens -= tokens
                return True
            return False

    async def acquire_or_raise(self, key: str, tokens: int = 1) -> None:
        allowed = await self.acquire(key, tokens)
        if not allowed:
            raise RateLimitExceeded(
                key, self.config.max_requests, time.monotonic() + self.config.window_seconds
            )

    async def acquire_multi_or_raise(self, keys: List[str], tokens: int = 1) -> None:
        """All keys must pass; raises with the first key that fails."""
        for key in keys:
            await self.acquire_or_raise(key, tokens)

    async def get_remaining(self, key: str) -> float:
        if self._redis:
            try:
                bucket = await self._redis.hgetall(self._key(key))
                if not bucket:
                    return float(self.config.max_requests)
                tokens = float(bucket.get("tokens", 0.0))
                ts = float(bucket.get("ts", 0.0))
                now_ms = time.time() * 1000.0
                if now_ms - ts >= self.config.window_seconds * 1000.0:
                    return float(self.config.max_requests)
                return max(0.0, min(float(self.config.max_requests), tokens))
            except Exception:
                pass
        bucket = self._buckets.get(key)
        if bucket is None:
            return float(self.config.max_requests)
        return bucket.tokens

    async def reset(self, key: str) -> None:
        if self._redis:
            try:
                await self._redis.delete(self._key(key))
            except Exception:
                pass
        async with self._lock:
            self._buckets.pop(key, None)

    async def health_check(self) -> HealthComponent:
        if self._degraded:
            return HealthComponent(
                name="rate_limiter",
                status=HealthStatus.DEGRADED,
                message="Redis unavailable; limits are per-process only",
            )
        return HealthComponent(name="rate_limiter", status=HealthStatus.HEALTHY)
