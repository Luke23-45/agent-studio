"""
P1-6 hot tier (Redis thread tail) tests:

- cache + get round-trip, tail capped to tail_size
- reads promote the TTL (session timeout alignment)
- summary block survives tail updates when not replaced (stable position)
- invalidate removes the key
- Redis down: writes no-op, reads miss, health DEGRADED (Postgres fallback)
"""

import asyncio

import pytest

from backend.app.infrastructure.patterns.health import HealthStatus
from backend.app.session.hot_tier import ThreadTailCache

REDIS_URL = "redis://127.0.0.1:6379"
DEAD_REDIS_URL = "redis://127.0.0.1:1"  # nothing listens here


def _redis_available() -> bool:
    try:
        import redis.asyncio as redis

        async def _ping() -> bool:
            client = redis.from_url(
                REDIS_URL, socket_connect_timeout=0.5, socket_timeout=0.5
            )
            try:
                await client.ping()
                return True
            finally:
                await client.close()

        return asyncio.run(_ping())
    except Exception:
        return False


_REDIS_UP = _redis_available()

_PREFIX = "neryva:thread:tail:test:"
_KEY = "neryva:thread:tail:test:t1:thread-1"


async def _cleanup() -> None:
    import redis.asyncio as redis

    client = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        for key in await client.keys("neryva:thread:tail:test:*"):
            await client.delete(key)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not _REDIS_UP, reason="Redis not running")
async def test_cache_get_round_trip():
    cache = ThreadTailCache(REDIS_URL, prefix=_PREFIX, ttl_seconds=60)
    await cache.initialize()
    try:
        assert await cache.cache_tail(
            "t1", "thread-1", [{"seq": 1, "role": "user", "content": "hi"}]
        ) is True
        got = await cache.get_tail("t1", "thread-1")
        assert got["tail"] == [{"seq": 1, "role": "user", "content": "hi"}]
        assert got["summary"] is None
    finally:
        await cache.close()
        await _cleanup()


@pytest.mark.asyncio
@pytest.mark.skipif(not _REDIS_UP, reason="Redis not running")
async def test_tail_capped_to_tail_size():
    cache = ThreadTailCache(REDIS_URL, prefix=_PREFIX, ttl_seconds=60, tail_size=3)
    await cache.initialize()
    try:
        tail = [{"seq": i, "content": f"m{i}"} for i in range(1, 6)]
        await cache.cache_tail("t1", "thread-1", tail)
        got = await cache.get_tail("t1", "thread-1")
        assert [m["seq"] for m in got["tail"]] == [3, 4, 5]
    finally:
        await cache.close()
        await _cleanup()


@pytest.mark.asyncio
@pytest.mark.skipif(not _REDIS_UP, reason="Redis not running")
async def test_read_promotes_ttl():
    import redis.asyncio as redis

    cache = ThreadTailCache(REDIS_URL, prefix=_PREFIX, ttl_seconds=60)
    await cache.initialize()
    client = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await cache.cache_tail("t1", "thread-1", [{"seq": 1}])
        await client.expire(_KEY, 1)  # shrink TTL
        await asyncio.sleep(0.2)
        assert (await client.ttl(_KEY)) < 60

        await cache.get_tail("t1", "thread-1")  # promote
        assert (await client.ttl(_KEY)) > 55
    finally:
        await client.close()
        await cache.close()
        await _cleanup()


@pytest.mark.asyncio
@pytest.mark.skipif(not _REDIS_UP, reason="Redis not running")
async def test_summary_survives_tail_updates():
    cache = ThreadTailCache(REDIS_URL, prefix=_PREFIX, ttl_seconds=60)
    await cache.initialize()
    try:
        await cache.cache_tail(
            "t1", "thread-1",
            [{"seq": 1}],
            summary={"text": "running summary", "position": 1},
        )
        await cache.cache_tail("t1", "thread-1", [{"seq": 1}, {"seq": 2}])
        got = await cache.get_tail("t1", "thread-1")
        assert got["summary"] == {"text": "running summary", "position": 1}

        await cache.cache_tail(
            "t1", "thread-1", [{"seq": 3}], summary={"text": "v2", "position": 3}
        )
        got = await cache.get_tail("t1", "thread-1")
        assert got["summary"] == {"text": "v2", "position": 3}
    finally:
        await cache.close()
        await _cleanup()


@pytest.mark.asyncio
@pytest.mark.skipif(not _REDIS_UP, reason="Redis not running")
async def test_invalidate_removes_key():
    import redis.asyncio as redis

    cache = ThreadTailCache(REDIS_URL, prefix=_PREFIX, ttl_seconds=60)
    await cache.initialize()
    client = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await cache.cache_tail("t1", "thread-1", [{"seq": 1}])
        assert await client.exists(_KEY) == 1
        await cache.invalidate("t1", "thread-1")
        assert await client.exists(_KEY) == 0
        assert await cache.get_tail("t1", "thread-1") is None
    finally:
        await client.close()
        await cache.close()
        await _cleanup()


def test_redis_down_degraded_with_postgres_fallback():
    async def _run():
        cache = ThreadTailCache(DEAD_REDIS_URL, prefix=_PREFIX)
        await cache.initialize()
        try:
            assert cache.degraded is True
            assert (await cache.health_check()).status is HealthStatus.DEGRADED
            assert await cache.cache_tail("t1", "thread-1", [{"seq": 1}]) is False
            assert await cache.get_tail("t1", "thread-1") is None
            assert await cache.invalidate("t1", "thread-1") is False
        finally:
            await cache.close()

    asyncio.run(_run())
