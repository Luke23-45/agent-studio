import json
import structlog
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ..patterns import ManagedService, HealthComponent, HealthStatus
from ..patterns.retry import AsyncRetry, RetryConfig, ExponentialBackoff
from ..patterns.circuit_breaker import CircuitBreaker, CircuitBreakerConfig

logger = structlog.get_logger(__name__)


class CacheMiss(Exception):
    pass


class LockNotAcquired(Exception):
    def __init__(self, key: str):
        self.key = key
        super().__init__(f"Could not acquire distributed lock: {key}")


@dataclass
class CacheConfig:
    redis_url: str = "redis://localhost:6379"
    default_ttl: int = 3600
    prefix: str = "neryva:"
    max_retries: int = 3
    retry_min_delay: float = 0.1
    connect_timeout: float = 5.0
    operation_timeout: float = 3.0
    circuit_breaker_failures: int = 3
    circuit_breaker_recovery: float = 15.0


@dataclass
class CacheEntry:
    value: Any
    ttl: int
    stored_at: float = field(default_factory=time.monotonic)
    version: int = 1


class CacheManager(ManagedService):
    def __init__(self, config: Optional[CacheConfig] = None):
        super().__init__("cache")
        self.config = config or CacheConfig()
        self._client: Any = None
        self._retry = AsyncRetry(RetryConfig(
            max_attempts=self.config.max_retries,
            backoff=ExponentialBackoff(min_delay=self.config.retry_min_delay),
        ))
        self._circuit_breaker = CircuitBreaker(CircuitBreakerConfig(
            name="cache",
            failure_threshold=self.config.circuit_breaker_failures,
            recovery_timeout=self.config.circuit_breaker_recovery,
        ))
        self._local_cache: Dict[str, CacheEntry] = {}

    async def _do_initialize(self) -> None:
        try:
            import redis.asyncio as redis
            self._client = redis.from_url(
                self.config.redis_url,
                decode_responses=True,
                socket_connect_timeout=self.config.connect_timeout,
                socket_timeout=self.config.operation_timeout,
                retry_on_timeout=True,
                health_check_interval=30,
            )
            await self._client.ping()
            logger.info("cache_initialized", url=self.config.redis_url)
        except Exception as e:
            # Redis missing or unreachable: degrade to the local in-memory
            # cache (single process only) instead of failing startup.
            logger.warning(
                "redis_unavailable_falling_back_to_memory_cache",
                url=self.config.redis_url,
                error=str(e),
            )
            self._client = None

    async def _do_close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None
        self._local_cache.clear()

    def _key(self, key: str) -> str:
        return f"{self.config.prefix}{key}"

    async def get(self, key: str, default: Any = None) -> Any:
        if self._client:
            try:
                result = await self._circuit_breaker.call(self._redis_get, key)
                return result if result is not None else default
            except Exception:
                pass
        return self._local_cache.get(key, CacheEntry(default, 0)).value

    async def _redis_get(self, key: str) -> Any:
        value = await self._client.get(self._key(key))
        if value is None:
            raise CacheMiss(key)
        return json.loads(value)

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        ttl = ttl if ttl is not None else self.config.default_ttl
        if self._client:
            try:
                await self._circuit_breaker.call(self._redis_set, key, value, ttl)
                return True
            except Exception as e:
                logger.error("cache_set_failed", key=key, error=str(e))
        self._local_cache[key] = CacheEntry(value=value, ttl=ttl)
        return True

    async def _redis_set(self, key: str, value: Any, ttl: int) -> None:
        serialized = json.dumps(value, default=str)
        await self._client.setex(self._key(key), ttl, serialized)

    async def delete(self, key: str) -> bool:
        if self._client:
            try:
                await self._circuit_breaker.call(self._redis_delete, key)
                return True
            except Exception:
                pass
        return self._local_cache.pop(key, None) is not None

    async def _redis_delete(self, key: str) -> None:
        await self._client.delete(self._key(key))

    async def exists(self, key: str) -> bool:
        if self._client:
            try:
                result = await self._circuit_breaker.call(self._redis_exists, key)
                return result
            except Exception:
                pass
        return key in self._local_cache

    async def _redis_exists(self, key: str) -> bool:
        return bool(await self._client.exists(self._key(key)))

    async def increment(self, key: str, amount: int = 1) -> Optional[int]:
        if self._client:
            try:
                return await self._circuit_breaker.call(self._redis_increment, key, amount)
            except Exception:
                pass
        return None

    async def _redis_increment(self, key: str, amount: int) -> int:
        return await self._client.incrby(self._key(key), amount)

    async def acquire_lock(self, key: str, ttl: int = 30, retry_delay: float = 0.1, max_retries: int = 10) -> bool:
        if not self._client:
            return True
        lock_key = f"{self._key(key)}:lock"
        for _ in range(max_retries):
            result = await self._client.setnx(lock_key, "1")
            if result:
                await self._client.expire(lock_key, ttl)
                return True
            await time.sleep(retry_delay)
        return False

    async def release_lock(self, key: str) -> None:
        if self._client:
            lock_key = f"{self._key(key)}:lock"
            await self._client.delete(lock_key)

    async def get_or_compute(self, key: str, compute_fn, ttl: Optional[int] = None) -> Any:
        cached = await self.get(key)
        if cached is not None:
            return cached
        value = await compute_fn()
        await self.set(key, value, ttl)
        return value

    async def clear_by_prefix(self, prefix: str) -> int:
        if not self._client:
            count = sum(1 for k in list(self._local_cache.keys()) if k.startswith(prefix))
            self._local_cache = {k: v for k, v in self._local_cache.items() if not k.startswith(prefix)}
            return count
        pattern = f"{self._key(prefix)}*"
        cursor = 0
        deleted = 0
        while True:
            cursor, keys = await self._client.scan(cursor, match=pattern, count=100)
            if keys:
                await self._client.delete(*keys)
                deleted += len(keys)
            if cursor == 0:
                break
        return deleted

    async def _do_health_check(self) -> HealthComponent:
        if self._client:
            try:
                start = time.monotonic()
                await self._client.ping()
                latency = time.monotonic() - start
                info = await self._client.info("memory")
                return HealthComponent(
                    name=self.name,
                    status=HealthStatus.HEALTHY if latency < 0.5 else HealthStatus.DEGRADED,
                    metadata={"latency_ms": latency * 1000, "redis_connected": True, "memory_used": info.get("used_memory_human", "N/A")},
                )
            except Exception as e:
                return HealthComponent(name=self.name, status=HealthStatus.DEGRADED, message=str(e), metadata={"redis_connected": False})
        return HealthComponent(name=self.name, status=HealthStatus.HEALTHY, message="In-memory cache (no Redis)", metadata={"mode": "local"})


_cache_manager: Optional[CacheManager] = None


def get_cache_manager() -> CacheManager:
    if _cache_manager is None:
        raise RuntimeError("Cache manager not initialized")
    return _cache_manager


def init_cache(redis_url: str = "redis://localhost:6379", **kwargs) -> CacheManager:
    global _cache_manager
    config = CacheConfig(redis_url=redis_url, **kwargs)
    _cache_manager = CacheManager(config)
    return _cache_manager
