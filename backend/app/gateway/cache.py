"""
Gateway caches (Arch 10, P3-7).

Two layers, both strictly per-tenant (tenant in every key):

- ``ExactCache``    : exact-match cache keyed by the canonical request
                      (messages + prompt version + model intent). Prompt
                      version is the stable-prefix marker (ties P2-6): a
                      config publish bumps it and the old entries simply
                      never match again — invalidation without a scan.
- ``SemanticCache`` : per-tenant similarity cache over the last user
                      message (sentence-transformers embeddings, cosine,
                      bounded per-tenant index in Redis). Separate from the
                      pgvector knowledge store — cache entries never pollute
                      RAG results.

Invalidation is explicit and durable: ``invalidate_tenant`` /
``invalidate_resource`` DEL the affected keys (exact via prefix scan,
semantic by purging the tenant index) and append a row to the durable
``cache_invalidation_log`` (models.py:634, P3-7). Stale results after a KB
edit are a product bug (§17) — the invalidation API exists for the edit
path (P0-11 wiring at P3-9) and is test-proven.

Redis down -> reads miss, writes no-op, once-per-transition log, health
DEGRADED (codebase convention: degrade visibly, never silent).
"""

from __future__ import annotations

import hashlib
import json
import time
import structlog
from dataclasses import asdict, dataclass
from typing import Any, Optional

from backend.app.infrastructure.patterns.health import HealthComponent, HealthStatus
from backend.app.infrastructure.patterns.lifecycle import ManagedService

logger = structlog.get_logger(__name__)


@dataclass
class CachedEntry:
    """Serializable subset of a GatewayResult for cache storage."""

    provider: str
    model: str
    content: str
    usage: dict[str, int]
    finish_reason: str | None = None
    tool_calls: list[dict[str, Any]] | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "CachedEntry":
        data = json.loads(raw)
        return cls(**data)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class ExactCacheConfig:
    redis_url: str = "redis://localhost:6379"
    prefix: str = "neryva:gateway:exact:"
    ttl_seconds: int = 300


class ExactCache(ManagedService):
    """Per-tenant exact-match response cache (Arch 10, P3-7)."""

    def __init__(self, config: Optional[ExactCacheConfig] = None):
        super().__init__("gateway_exact_cache")
        self.config = config or ExactCacheConfig()
        self._redis: Any = None
        self._redis_available = False
        self._degraded_logged = False

    def _key(self, tenant_id: str, prompt_version: str, canonical_request: str) -> str:
        digest = _sha256(f"{tenant_id}:{prompt_version}:{canonical_request}")[:32]
        return f"{self.config.prefix}{tenant_id}:{digest}"

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
            self._redis_available = True
            logger.info("gateway_exact_cache_connected", url=self.config.redis_url)
        except Exception as e:
            self._redis = None
            self._redis_available = False
            logger.warning("gateway_exact_cache_redis_unavailable", error=str(e))

    async def _do_close(self) -> None:
        if self._redis:
            await self._redis.close()
            self._redis = None
            self._redis_available = False

    async def get(
        self, *, tenant_id: str, prompt_version: str, canonical_request: str
    ) -> CachedEntry | None:
        if not self._redis_available or self._redis is None:
            return None
        try:
            raw = await self._redis.get(
                self._key(tenant_id, prompt_version, canonical_request)
            )
            if raw is None:
                return None
            await self._redis.expire(
                self._key(tenant_id, prompt_version, canonical_request),
                self.config.ttl_seconds,
            )
            return CachedEntry.from_json(raw)
        except Exception as e:
            await self._log_degraded_once(e)
            return None

    async def set(
        self,
        *,
        tenant_id: str,
        prompt_version: str,
        canonical_request: str,
        entry: CachedEntry,
    ) -> None:
        if not self._redis_available or self._redis is None:
            return
        try:
            await self._redis.setex(
                self._key(tenant_id, prompt_version, canonical_request),
                self.config.ttl_seconds,
                entry.to_json(),
            )
        except Exception as e:
            await self._log_degraded_once(e)

    async def purge_tenant(self, tenant_id: str) -> int:
        """Drop every exact-cache key of a tenant (invalidation); count."""
        if not self._redis_available or self._redis is None:
            return 0
        try:
            pattern = f"{self.config.prefix}{tenant_id}:*"
            keys = []
            async for key in self._redis.scan_iter(match=pattern, count=100):
                keys.append(key)
            if keys:
                await self._redis.delete(*keys)
            return len(keys)
        except Exception as e:
            await self._log_degraded_once(e)
            return 0

    async def _log_degraded_once(self, error: Exception) -> None:
        if not self._degraded_logged:
            self._degraded_logged = True
            logger.error(
                "gateway_exact_cache_degraded",
                error=str(error),
                hint="cache reads miss and writes no-op until Redis recovers",
            )

    async def _do_health_check(self) -> HealthComponent:
        if not self._redis_available:
            return HealthComponent(
                name=self.name,
                status=HealthStatus.DEGRADED,
                message="Redis unavailable; exact cache off",
            )
        return HealthComponent(
            name=self.name,
            status=HealthStatus.HEALTHY,
            metadata={"ttl_seconds": self.config.ttl_seconds},
        )


@dataclass
class SemanticCacheConfig:
    redis_url: str = "redis://localhost:6379"
    prefix: str = "neryva:gateway:semantic:"
    embedding_model: str = "all-MiniLM-L6-v2"
    similarity_threshold: float = 0.92
    per_tenant_capacity: int = 500
    ttl_seconds: int = 300


class SemanticCache(ManagedService):
    """Per-tenant semantic cache (Arch 10, P3-7): embeddings + cosine over
    the last user message, bounded index per tenant, isolated from the
    knowledge vector store."""

    def __init__(self, config: Optional[SemanticCacheConfig] = None):
        super().__init__("gateway_semantic_cache")
        self.config = config or SemanticCacheConfig()
        self._redis: Any = None
        self._redis_available = False
        self._degraded_logged = False
        self._encoder: Any = None

    def _index_key(self, tenant_id: str) -> str:
        return f"{self.config.prefix}{tenant_id}:index"

    def _entry_key(self, tenant_id: str, entry_id: str) -> str:
        return f"{self.config.prefix}{tenant_id}:entry:{entry_id}"

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
            self._redis_available = True
            logger.info("gateway_semantic_cache_connected", url=self.config.redis_url)
        except Exception as e:
            self._redis = None
            self._redis_available = False
            logger.warning("gateway_semantic_cache_redis_unavailable", error=str(e))

    async def _do_close(self) -> None:
        if self._redis:
            await self._redis.close()
            self._redis = None
            self._redis_available = False

    def _get_encoder(self) -> Any:
        """Lazy-load the sentence-transformers encoder (heavy import)."""
        if self._encoder is None:
            from sentence_transformers import SentenceTransformer

            self._encoder = SentenceTransformer(self.config.embedding_model)
        return self._encoder

    def _embed(self, text: str) -> list[float]:
        import numpy as np

        vector = self._get_encoder().encode(text, normalize_embeddings=True)
        return np.asarray(vector, dtype=np.float32).tolist()

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        import numpy as np

        if not a or not b or len(a) != len(b):
            return 0.0
        va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
        return float(np.dot(va, vb))

    async def find_similar(
        self, *, tenant_id: str, query_text: str
    ) -> CachedEntry | None:
        """Best semantic match for the last user message, or None."""
        if not self._redis_available or self._redis is None:
            return None
        try:
            query_vector = self._embed(query_text)
            candidates = await self._redis.zrevrange(
                self._index_key(tenant_id), 0, -1
            )
            best: tuple[float, str] | None = None
            for entry_id in candidates:
                raw = await self._redis.get(self._entry_key(tenant_id, entry_id))
                if raw is None:
                    continue
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                score = self._cosine(query_vector, data.get("vector", []))
                if score >= self.config.similarity_threshold:
                    if best is None or score > best[0]:
                        best = (score, entry_id)
            if best is None:
                return None
            data = json.loads(
                await self._redis.get(self._entry_key(tenant_id, best[1])) or "{}"
            )
            await self._redis.expire(
                self._entry_key(tenant_id, best[1]), self.config.ttl_seconds
            )
            await self._redis.expire(self._index_key(tenant_id), self.config.ttl_seconds)
            return CachedEntry.from_json(json.dumps(data["entry"]))
        except Exception as e:
            await self._log_degraded_once(e)
            return None

    async def store(
        self,
        *,
        tenant_id: str,
        query_text: str,
        entry: CachedEntry,
    ) -> None:
        if not self._redis_available or self._redis is None:
            return
        try:
            vector = self._embed(query_text)
            entry_id = _sha256(query_text)[:16]
            payload = json.dumps({"vector": vector, "entry": asdict(entry)})
            pipe = self._redis.pipeline()
            pipe.setex(
                self._entry_key(tenant_id, entry_id), self.config.ttl_seconds, payload
            )
            pipe.zadd(self._index_key(tenant_id), {entry_id: time.time()})
            pipe.zremrangebyrank(
                self._index_key(tenant_id), 0, -self.config.per_tenant_capacity - 1
            )
            await pipe.execute()
        except Exception as e:
            await self._log_degraded_once(e)

    async def purge_tenant(self, tenant_id: str) -> int:
        """Drop every entry of a tenant (invalidation); returns count."""
        if not self._redis_available or self._redis is None:
            return 0
        try:
            entries = await self._redis.zrange(self._index_key(tenant_id), 0, -1)
            if entries:
                await self._redis.delete(*[self._entry_key(tenant_id, e) for e in entries])
            await self._redis.delete(self._index_key(tenant_id))
            return len(entries)
        except Exception as e:
            await self._log_degraded_once(e)
            return 0

    async def _log_degraded_once(self, error: Exception) -> None:
        if not self._degraded_logged:
            self._degraded_logged = True
            logger.error(
                "gateway_semantic_cache_degraded",
                error=str(error),
                hint="semantic cache reads miss and writes no-op until Redis recovers",
            )

    async def _do_health_check(self) -> HealthComponent:
        if not self._redis_available:
            return HealthComponent(
                name=self.name,
                status=HealthStatus.DEGRADED,
                message="Redis unavailable; semantic cache off",
            )
        return HealthComponent(
            name=self.name,
            status=HealthStatus.HEALTHY,
            metadata={
                "embedding_model": self.config.embedding_model,
                "similarity_threshold": self.config.similarity_threshold,
                "per_tenant_capacity": self.config.per_tenant_capacity,
            },
        )


class GatewayCache:
    """Facade over the exact + semantic caches plus durable invalidation
    logging (Arch 10, P3-7). Wire one instance with the ManagedService
    lifecycle in ``main.py``."""

    def __init__(
        self,
        exact: ExactCache | None = None,
        semantic: SemanticCache | None = None,
        db: Any = None,
    ):
        self.exact = exact or ExactCache()
        self.semantic = semantic or SemanticCache()
        self._db = db

    async def get_exact(
        self, *, tenant_id: str, prompt_version: str, canonical_request: str
    ) -> CachedEntry | None:
        return await self.exact.get(
            tenant_id=tenant_id, prompt_version=prompt_version, canonical_request=canonical_request
        )

    async def set_exact(
        self,
        *,
        tenant_id: str,
        prompt_version: str,
        canonical_request: str,
        entry: CachedEntry,
    ) -> None:
        await self.exact.set(
            tenant_id=tenant_id,
            prompt_version=prompt_version,
            canonical_request=canonical_request,
            entry=entry,
        )

    async def get_semantic(self, *, tenant_id: str, query_text: str) -> CachedEntry | None:
        return await self.semantic.find_similar(tenant_id=tenant_id, query_text=query_text)

    async def set_semantic(self, *, tenant_id: str, query_text: str, entry: CachedEntry) -> None:
        await self.semantic.store(tenant_id=tenant_id, query_text=query_text, entry=entry)

    async def invalidate_tenant(self, tenant_id: str, reason: str = "tenant invalidate") -> int:
        """Purge all of a tenant's cached entries (exact + semantic) + log durably."""
        removed = await self.exact.purge_tenant(tenant_id)
        removed += await self.semantic.purge_tenant(tenant_id)
        await self._log_invalidation(tenant_id, "tenant", None, reason)
        logger.info("gateway_cache_tenant_invalidated", tenant_id=tenant_id, removed=removed)
        return removed

    async def invalidate_resource(
        self, tenant_id: str, resource_id: str, reason: str = "resource invalidate"
    ) -> int:
        """Drop a tenant's cached responses after a knowledge/config change.

        Both layers are purged: the semantic index fully, and the exact
        keys via prefix scan — a KB edit must not keep serving cached
        answers built from the pre-edit knowledge (§17 stale-result rule).
        """
        removed = await self.exact.purge_tenant(tenant_id)
        removed += await self.semantic.purge_tenant(tenant_id)
        await self._log_invalidation(tenant_id, "resource", resource_id, reason)
        logger.info(
            "gateway_cache_resource_invalidated",
            tenant_id=tenant_id,
            resource_id=resource_id,
            removed=removed,
        )
        return removed

    async def _log_invalidation(
        self, tenant_id: str, scope: str, resource_id: str | None, reason: str
    ) -> None:
        if self._db is None:
            return
        try:
            from backend.app.infrastructure.db.repositories import (
                CacheInvalidationRepository,
            )

            await CacheInvalidationRepository(self._db).add(
                tenant_id=tenant_id, scope=scope, resource_id=resource_id, reason=reason
            )
        except Exception as e:
            logger.error("gateway_cache_invalidation_log_failed", error=str(e))
