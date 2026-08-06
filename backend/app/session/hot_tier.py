"""
Hot tier: Redis thread tail cache (Arch 7.3, P1-6).

Key ``neryva:thread:tail:{tenant_id}:{thread_id}`` holds the last N turns
plus an optional running summary block, JSON-encoded, with a TTL aligned
to the session timeout. Reads promote the key (TTL refresh). Summary
position is stable: passing ``summary=None`` on an update keeps the
existing block untouched.

Redis down → cache is bypassed (writes no-op, reads miss) and the caller
falls back to the Postgres tail; the health check reports DEGRADED, never
silent. Every call re-pings through the client, so the cache self-heals
when Redis returns.
"""

import json
import structlog
from typing import Any, Optional

from backend.app.infrastructure.patterns.health import HealthComponent, HealthStatus
from backend.app.infrastructure.patterns.lifecycle import ManagedService

logger = structlog.get_logger(__name__)


class ThreadTailCache(ManagedService):
    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        *,
        prefix: str = "neryva:thread:tail:",
        ttl_seconds: int = 86400,
        tail_size: int = 10,
    ):
        super().__init__("thread_tail_cache")
        self.redis_url = redis_url
        self.prefix = prefix
        self.ttl_seconds = ttl_seconds
        self.tail_size = tail_size
        self._redis: Any = None
        self._redis_available = False

    @property
    def degraded(self) -> bool:
        return not self._redis_available

    def _key(self, tenant_id: str, thread_id: str) -> str:
        return f"{self.prefix}{tenant_id}:{thread_id}"

    async def _do_initialize(self) -> None:
        try:
            import redis.asyncio as redis

            self._redis = redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
            )
            await self._redis.ping()
            self._redis_available = True
            logger.info(
                "thread_tail_cache_redis_connected", url=self.redis_url
            )
        except Exception as e:
            self._redis = None
            self._redis_available = False
            logger.error(
                "thread_tail_cache_redis_unavailable_postgres_fallback",
                error=str(e),
                hint="tail reads fall back to Postgres until Redis recovers",
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
                message="Redis unavailable; tail reads fall back to Postgres",
            )
        return HealthComponent(
            name=self.name,
            status=HealthStatus.HEALTHY,
            metadata={"ttl_seconds": self.ttl_seconds},
        )

    async def cache_tail(
        self,
        tenant_id: str,
        thread_id: str,
        tail: list[dict[str, Any]],
        *,
        summary: dict[str, Any] | None = None,
    ) -> bool:
        """Write/refresh the hot tail. Returns False when Redis is down.

        ``summary=None`` keeps the existing summary block (stable position,
        ties P2-6); pass a dict to set/replace it. Tail is capped to
        ``tail_size`` newest messages.
        """
        if not self._redis_available:
            return False
        key = self._key(tenant_id, thread_id)
        window = tail[-self.tail_size :]
        try:
            existing: dict[str, Any] | None = None
            if summary is None:
                raw = await self._redis.get(key)
                if raw:
                    existing = json.loads(raw)
            payload = {
                "tail": window,
                "summary": summary
                if summary is not None
                else (existing or {}).get("summary"),
            }
            await self._redis.set(
                key, json.dumps(payload), ex=self.ttl_seconds
            )
            return True
        except Exception as e:
            if self._redis_available:
                self._redis_available = False
                logger.error(
                    "thread_tail_cache_redis_down_postgres_fallback",
                    key=key,
                    error=str(e),
                )
            return False

    async def get_tail(
        self, tenant_id: str, thread_id: str
    ) -> dict[str, Any] | None:
        """Return the cached tail+summary, promoting its TTL on hit."""
        if not self._redis_available:
            return None
        key = self._key(tenant_id, thread_id)
        try:
            raw = await self._redis.get(key)
            if raw is None:
                return None
            await self._redis.expire(key, self.ttl_seconds)  # promote
            return json.loads(raw)
        except Exception as e:
            if self._redis_available:
                self._redis_available = False
                logger.error(
                    "thread_tail_cache_redis_down_postgres_fallback",
                    key=key,
                    error=str(e),
                )
            return None

    async def invalidate(self, tenant_id: str, thread_id: str) -> bool:
        if not self._redis_available:
            return False
        try:
            await self._redis.delete(self._key(tenant_id, thread_id))
            return True
        except Exception as e:
            if self._redis_available:
                self._redis_available = False
                logger.error(
                    "thread_tail_cache_redis_down_postgres_fallback",
                    error=str(e),
                )
            return False


_cache: Optional[ThreadTailCache] = None


def init_thread_tail_cache(redis_url: str, **kwargs: Any) -> ThreadTailCache:
    global _cache
    _cache = ThreadTailCache(redis_url, **kwargs)
    return _cache


def get_thread_tail_cache() -> ThreadTailCache:
    if _cache is None:
        raise RuntimeError("Thread tail cache not initialized")
    return _cache
