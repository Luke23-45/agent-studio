import asyncio
import structlog
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = structlog.get_logger(__name__)


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
    """Rate limiter with a distributed (Redis) fast path.

    When Redis is available a fixed-window counter is shared across all
    worker processes (multi-worker safe). Without Redis (or on Redis
    failure) it degrades to the per-process token bucket so a limiter
    outage can never take the API down.
    """

    def __init__(self, config: RateLimitConfig, redis_url: Optional[str] = None):
        self.config = config
        self._redis_url = redis_url
        self._redis: Any = None
        self._buckets: Dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

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
            logger.info("rate_limiter_initialized", url=self._redis_url)
        except Exception as e:
            logger.warning(
                "rate_limiter_falling_back_to_memory",
                url=self._redis_url,
                error=str(e),
            )
            self._redis = None

    def _key(self, key: str) -> str:
        return f"{self.config.key_prefix}{key}"

    async def acquire(self, key: str, tokens: int = 1) -> bool:
        if self._redis:
            try:
                return await self._redis_acquire(key, tokens)
            except Exception as e:
                logger.warning("rate_limiter_redis_error_fallback", key=key, error=str(e))
        return await self._memory_acquire(key, tokens)

    async def _redis_acquire(self, key: str, tokens: int = 1) -> bool:
        full_key = self._key(key)
        count = await self._redis.incrby(full_key, tokens)
        if count == tokens:
            await self._redis.expire(full_key, int(self.config.window_seconds))
        return count <= self.config.max_requests

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
                count = await self._redis.get(self._key(key))
                if count is None:
                    return float(self.config.max_requests)
                return max(0.0, float(self.config.max_requests) - float(count))
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
