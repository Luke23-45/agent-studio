"""
Cache infrastructure layer.

Provides Redis-based caching for sessions, results, and rate limiting.
"""

import json
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class CacheManager:
    """Manages Redis cache connections and operations."""

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        default_ttl: int = 3600,
        prefix: str = "neryva:",
    ):
        self.redis_url = redis_url
        self.default_ttl = default_ttl
        self.prefix = prefix
        self._client: Any | None = None

    async def initialize(self) -> None:
        """Initialize Redis connection."""
        try:
            import redis.asyncio as redis

            self._client = redis.from_url(
                self.redis_url,
                decode_responses=True,
            )
            await self._client.ping()
            logger.info("cache_initialized", url=self.redis_url)
        except ImportError:
            logger.warning("redis_not_available", message="Redis client not installed")
        except Exception as e:
            logger.error("cache_init_error", error=str(e))

    async def close(self) -> None:
        """Close Redis connection."""
        if self._client:
            await self._client.close()
            self._client = None
            logger.info("cache_closed")

    def _key(self, key: str) -> str:
        """Generate prefixed cache key."""
        return f"{self.prefix}{key}"

    async def get(self, key: str) -> Any | None:
        """Get value from cache."""
        if self._client is None:
            return None

        try:
            value = await self._client.get(self._key(key))
            if value is None:
                return None
            return json.loads(value)
        except Exception as e:
            logger.error("cache_get_error", key=key, error=str(e))
            return None

    async def set(
        self,
        key: str,
        value: Any,
        ttl: int | None = None,
    ) -> bool:
        """Set value in cache with optional TTL."""
        if self._client is None:
            return False

        try:
            serialized = json.dumps(value)
            ttl = ttl or self.default_ttl
            await self._client.setex(self._key(key), ttl, serialized)
            return True
        except Exception as e:
            logger.error("cache_set_error", key=key, error=str(e))
            return False

    async def delete(self, key: str) -> bool:
        """Delete value from cache."""
        if self._client is None:
            return False

        try:
            await self._client.delete(self._key(key))
            return True
        except Exception as e:
            logger.error("cache_delete_error", key=key, error=str(e))
            return False

    async def exists(self, key: str) -> bool:
        """Check if key exists in cache."""
        if self._client is None:
            return False

        try:
            return bool(await self._client.exists(self._key(key)))
        except Exception as e:
            logger.error("cache_exists_error", key=key, error=str(e))
            return False

    async def increment(self, key: str, amount: int = 1) -> int | None:
        """Increment a counter in cache."""
        if self._client is None:
            return None

        try:
            return await self._client.incrby(self._key(key), amount)
        except Exception as e:
            logger.error("cache_increment_error", key=key, error=str(e))
            return None

    async def health_check(self) -> bool:
        """Check cache connectivity."""
        if self._client is None:
            return False

        try:
            await self._client.ping()
            return True
        except Exception as e:
            logger.error("cache_health_check_failed", error=str(e))
            return False


# Global cache instance
_cache_manager: CacheManager | None = None


def get_cache_manager() -> CacheManager:
    """Get the global cache manager instance."""
    if _cache_manager is None:
        raise RuntimeError("Cache manager not initialized")
    return _cache_manager


def init_cache(redis_url: str = "redis://localhost:6379", **kwargs) -> CacheManager:
    """Initialize the global cache manager."""
    global _cache_manager
    _cache_manager = CacheManager(redis_url, **kwargs)
    return _cache_manager