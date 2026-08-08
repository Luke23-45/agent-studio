"""
P3-7 — Gateway cache tests (exact + semantic + durable invalidation log).

Exact cache: per-tenant keys, prompt-version invalidation without a scan,
TTL refresh on hit, tenant-scoped purge. Semantic cache: deterministic
embeddings injected (no model load), similarity threshold, per-tenant
isolation, bounded per-tenant index. Facade: ``invalidate_tenant`` /
``invalidate_resource`` purge BOTH layers and append durable
``cache_invalidation_log`` rows. Redis-down: reads miss, writes no-op,
purge returns 0, health DEGRADED.
"""

import asyncio
import json
from uuid import uuid4

import pytest

from backend.app.gateway.cache import (
    CachedEntry,
    ExactCache,
    ExactCacheConfig,
    GatewayCache,
    SemanticCache,
    SemanticCacheConfig,
)
from backend.app.infrastructure.db.manager import DatabaseConfig, DatabaseManager
from backend.app.infrastructure.db.models import Base
from backend.app.infrastructure.db.repositories import CacheInvalidationRepository
from backend.app.infrastructure.patterns.health import HealthStatus


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(
        DatabaseConfig(database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/cache.db")
    )

    async def _init():
        await manager.initialize()
        async with manager._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    yield manager
    asyncio.run(manager.close())


class _FakePipeline:
    def __init__(self, redis):
        self._redis = redis
        self._ops: list[tuple] = []

    def setex(self, key, ttl, value):
        self._ops.append(("setex", key, ttl, value))
        return self

    def zadd(self, key, mapping):
        self._ops.append(("zadd", key, mapping))
        return self

    def zremrangebyrank(self, key, start, stop):
        self._ops.append(("zremrangebyrank", key, start, stop))
        return self

    async def execute(self):
        for op in self._ops:
            if op[0] == "setex":
                self._redis._data[op[1]] = op[3]
                self._redis._ttl[op[1]] = op[2]
            elif op[0] == "zadd":
                self._redis._sorted.setdefault(op[1], {})
                for member, score in op[2].items():
                    self._redis._sorted[op[1]][member] = score
            elif op[0] == "zremrangebyrank":
                self._redis._removerank(op[1], op[2], op[3])
        self._ops.clear()


class _FakeRedis:
    """Minimal async redis stand-in used by both caches."""

    def __init__(self):
        self._data: dict[str, str] = {}
        self._ttl: dict[str, int] = {}
        self._sorted: dict[str, dict] = {}
        self.deleted: list[str] = []

    async def ping(self):
        return True

    async def get(self, key):
        return self._data.get(key)

    async def setex(self, key, ttl, value):
        self._data[key] = value
        self._ttl[key] = ttl

    async def expire(self, key, ttl):
        self._ttl[key] = ttl

    async def delete(self, *keys):
        for k in keys:
            self._data.pop(k, None)
            self._ttl.pop(k, None)
            self._sorted.pop(k, None)
        self.deleted.extend(keys)

    async def scan_iter(self, match="*", count=100):
        import fnmatch

        for key in list(self._data):
            if fnmatch.fnmatch(key, match):
                yield key

    def pipeline(self):
        return _FakePipeline(self)

    async def zadd(self, key, mapping):
        self._sorted.setdefault(key, {}).update(mapping)

    async def zrange(self, key, start, stop):
        ordered = sorted(self._sorted.get(key, {}).items(), key=lambda kv: kv[1])
        return [m for m, _s in ordered]

    async def zrevrange(self, key, start, stop):
        ordered = sorted(self._sorted.get(key, {}).items(), key=lambda kv: kv[1], reverse=True)
        return [m for m, _s in ordered]

    async def zremrangebyrank(self, key, start, stop):
        return self._removerank(key, start, stop)

    def _removerank(self, key, start, stop):
        members = self._sorted.get(key, {})
        ordered = sorted(members.items(), key=lambda kv: kv[1])
        n = len(ordered)
        if start < 0:
            start = max(n + start, 0)
        if stop < 0:
            stop = n + stop
        stop = min(stop, n - 1)
        keep = {m for i, (m, _s) in enumerate(ordered) if i < start or i > stop}
        for m in list(members):
            if m not in keep:
                del members[m]
        return len(members)


def _fake_embed(dim=64):
    """Deterministic normalized bigram bag-of-words embedding (no model load).

    The real encoder emits unit vectors (``normalize_embeddings=True``) so
    ``_cosine``'s raw dot product is a true cosine — the fake must match.
    """

    def _embed(text: str) -> list[float]:
        vector = [0.0] * dim
        for i in range(len(text) - 1):
            idx = hash(text[i : i + 2]) % dim
            vector[idx] = 1.0
        norm = sum(v * v for v in vector) ** 0.5
        if norm:
            vector = [v / norm for v in vector]
        return vector

    return _embed


def _entry(**kwargs) -> CachedEntry:
    base = dict(
        provider="openai",
        model="gpt-4o",
        content="cached answer",
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    base.update(kwargs)
    return CachedEntry(**base)


def _exact(fake: _FakeRedis) -> ExactCache:
    cache = ExactCache(ExactCacheConfig())
    cache._redis = fake
    cache._redis_available = True
    return cache


def _semantic(fake: _FakeRedis) -> SemanticCache:
    cache = SemanticCache(SemanticCacheConfig(similarity_threshold=0.92))
    cache._redis = fake
    cache._redis_available = True
    cache._embed = _fake_embed()
    return cache


def _gateway_cache(fake: _FakeRedis, db=None) -> GatewayCache:
    return GatewayCache(exact=_exact(fake), semantic=_semantic(fake), db=db)


# ----------------------------------------------------------------------
# exact cache
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_hit_miss_and_ttl_refresh():
    fake = _FakeRedis()
    cache = _exact(fake)
    entry = _entry()

    assert await cache.get(tenant_id="t1", prompt_version="v1", canonical_request="q1") is None

    await cache.set(tenant_id="t1", prompt_version="v1", canonical_request="q1", entry=entry)
    hit = await cache.get(tenant_id="t1", prompt_version="v1", canonical_request="q1")
    assert hit is not None and hit.content == "cached answer"

    key = cache._key("t1", "v1", "q1")
    assert key in fake._data
    assert fake._ttl.get(key) == cache.config.ttl_seconds


@pytest.mark.asyncio
async def test_exact_keys_are_tenant_scoped():
    fake = _FakeRedis()
    cache = _exact(fake)

    await cache.set(tenant_id="t1", prompt_version="v1", canonical_request="q", entry=_entry())
    await cache.set(tenant_id="t2", prompt_version="v1", canonical_request="q", entry=_entry())

    keys = set(fake._data)
    assert len(keys) == 2
    assert any(k.startswith("neryva:gateway:exact:t1:") for k in keys)
    assert any(k.startswith("neryva:gateway:exact:t2:") for k in keys)


@pytest.mark.asyncio
async def test_exact_prompt_version_bump_invalidates_without_scan():
    fake = _FakeRedis()
    cache = _exact(fake)

    await cache.set(tenant_id="t1", prompt_version="v1", canonical_request="q", entry=_entry())
    assert await cache.get(tenant_id="t1", prompt_version="v1", canonical_request="q") is not None
    assert await cache.get(tenant_id="t1", prompt_version="v2", canonical_request="q") is None


@pytest.mark.asyncio
async def test_exact_purge_tenant_scoped():
    fake = _FakeRedis()
    cache = _exact(fake)

    await cache.set(tenant_id="t1", prompt_version="v1", canonical_request="q", entry=_entry())
    await cache.set(tenant_id="t1", prompt_version="v2", canonical_request="q2", entry=_entry())
    await cache.set(tenant_id="t2", prompt_version="v1", canonical_request="q", entry=_entry())

    removed = await cache.purge_tenant("t1")
    assert removed == 2
    assert any(k.startswith("neryva:gateway:exact:t2:") for k in fake._data)
    assert not any(k.startswith("neryva:gateway:exact:t1:") for k in fake._data)


# ----------------------------------------------------------------------
# semantic cache
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_similar_hit_and_distinct_miss():
    fake = _FakeRedis()
    cache = _semantic(fake)

    await cache.store(tenant_id="t1", query_text="hello world", entry=_entry())
    assert await cache.find_similar(tenant_id="t1", query_text="hello world") is not None
    assert await cache.find_similar(tenant_id="t1", query_text="banana pancake") is None


@pytest.mark.asyncio
async def test_semantic_per_tenant_isolation():
    fake = _FakeRedis()
    cache = _semantic(fake)

    await cache.store(tenant_id="t1", query_text="hello world", entry=_entry())
    assert await cache.find_similar(tenant_id="t2", query_text="hello world") is None
    assert await cache.find_similar(tenant_id="t1", query_text="hello world") is not None


@pytest.mark.asyncio
async def test_semantic_capacity_bounded_per_tenant():
    fake = _FakeRedis()
    config = SemanticCacheConfig(similarity_threshold=0.92, per_tenant_capacity=3)
    cache = SemanticCache(config)
    cache._redis = fake
    cache._redis_available = True
    cache._embed = _fake_embed()

    for i in range(5):
        await cache.store(
            tenant_id="t1",
            query_text=f"query number {i} unique phrase",
            entry=_entry(content=f"answer {i}"),
        )

    entries = await fake.zrange(cache._index_key("t1"), 0, -1)
    assert len(entries) <= config.per_tenant_capacity


@pytest.mark.asyncio
async def test_semantic_purge_tenant_drops_index_and_entries():
    fake = _FakeRedis()
    cache = _semantic(fake)

    await cache.store(tenant_id="t1", query_text="hello world", entry=_entry())
    await cache.store(tenant_id="t2", query_text="hello world", entry=_entry())

    removed = await cache.purge_tenant("t1")
    assert removed == 1
    assert await cache.find_similar(tenant_id="t1", query_text="hello world") is None
    assert await cache.find_similar(tenant_id="t2", query_text="hello world") is not None


# ----------------------------------------------------------------------
# facade + durable invalidation log
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_tenant_purges_both_layers_and_logs(db):
    fake = _FakeRedis()
    cache = _gateway_cache(fake, db=db)
    tenant = str(uuid4())

    await cache.set_exact(tenant_id=tenant, prompt_version="v1", canonical_request="q", entry=_entry())
    await cache.set_semantic(tenant_id=tenant, query_text="hello world", entry=_entry())
    await cache.set_exact(tenant_id="other", prompt_version="v1", canonical_request="q", entry=_entry())

    removed = await cache.invalidate_tenant(tenant, reason="test.invalidate")

    assert removed == 2
    assert await cache.get_exact(tenant_id=tenant, prompt_version="v1", canonical_request="q") is None
    assert await cache.get_semantic(tenant_id=tenant, query_text="hello world") is None
    assert await cache.get_exact(tenant_id="other", prompt_version="v1", canonical_request="q") is not None

    rows = await CacheInvalidationRepository(db).list_recent(tenant)
    assert len(rows) == 1
    assert rows[0]["scope"] == "tenant"
    assert rows[0]["reason"] == "test.invalidate"


@pytest.mark.asyncio
async def test_invalidate_resource_purges_stale_after_kb_change(db):
    fake = _FakeRedis()
    cache = _gateway_cache(fake, db=db)
    tenant = str(uuid4())
    document_id = f"doc-{uuid4().hex[:8]}"

    await cache.set_exact(tenant_id=tenant, prompt_version="v1", canonical_request="q", entry=_entry())
    await cache.set_semantic(tenant_id=tenant, query_text="hello world", entry=_entry())

    removed = await cache.invalidate_resource(tenant, document_id, reason="ingestion.embed:reindex")

    assert removed == 2
    assert await cache.get_exact(tenant_id=tenant, prompt_version="v1", canonical_request="q") is None
    assert await cache.get_semantic(tenant_id=tenant, query_text="hello world") is None

    rows = await CacheInvalidationRepository(db).list_recent(tenant)
    assert rows[0]["scope"] == "resource"
    assert rows[0]["resource_id"] == document_id


@pytest.mark.asyncio
async def test_invalidation_log_skipped_without_db():
    fake = _FakeRedis()
    cache = _gateway_cache(fake, db=None)
    await cache.invalidate_tenant("t1")  # must not raise


# ----------------------------------------------------------------------
# redis-down degrade
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_down_reads_miss_writes_noop():
    import redis.exceptions  # noqa: F401  (triggers availability of real module)

    class _DeadRedis(_FakeRedis):
        async def ping(self):
            raise ConnectionError("redis down")

    # Initialize through the real lifecycle so `health_check` sees an
    # initialized service whose redis is unreachable (DEGRADED).
    exact = ExactCache(ExactCacheConfig())
    semantic = SemanticCache(SemanticCacheConfig(similarity_threshold=0.92))
    dead = _DeadRedis()
    import redis.asyncio as redis_module

    original_from_url = redis_module.from_url
    redis_module.from_url = lambda *a, **k: dead
    try:
        await exact.initialize()
        await semantic.initialize()
    finally:
        redis_module.from_url = original_from_url

    assert exact._redis_available is False
    assert await exact.get(tenant_id="t1", prompt_version="v1", canonical_request="q") is None
    await exact.set(tenant_id="t1", prompt_version="v1", canonical_request="q", entry=_entry())
    assert await exact.purge_tenant("t1") == 0
    assert await semantic.find_similar(tenant_id="t1", query_text="hello") is None
    await semantic.store(tenant_id="t1", query_text="hello", entry=_entry())
    assert await semantic.purge_tenant("t1") == 0

    health = await exact.health_check()
    assert health.status is HealthStatus.DEGRADED
    health2 = await semantic.health_check()
    assert health2.status is HealthStatus.DEGRADED
    await exact.close()
    await semantic.close()
