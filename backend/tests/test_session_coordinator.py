"""
P1-3 session coordinator tests:

- per-thread serialization: waiters queue until the lease is released
- different threads never block each other
- the Redis lock is shared across "workers" (raw SET NX blocks the coordinator)
- lease renewal keeps the key alive while held
- release is idempotent and removes the key
- Redis down: local-only fallback still serializes, health reports DEGRADED
"""

import asyncio
import time

import pytest

from backend.app.infrastructure.patterns.health import HealthStatus
from backend.app.session.coordinator import (
    CoordinatorConfig,
    CoordinatorBusy,
    ThreadCoordinator,
)

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


def _config(**kwargs) -> CoordinatorConfig:
    return CoordinatorConfig(
        redis_url=REDIS_URL,
        lease_seconds=kwargs.pop("lease_seconds", 30),
        wait_seconds=kwargs.pop("wait_seconds", 5.0),
        prefix=kwargs.pop("prefix", "neryva:threadlock:test:"),
        **kwargs,
    )


async def _cleanup(key: str) -> None:
    import redis.asyncio as redis

    client = redis.from_url(REDIS_URL)
    try:
        await client.delete(key)
    finally:
        await client.close()


# ---- serialization -------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not _REDIS_UP, reason="Redis not running")
async def test_second_waiter_blocks_until_release():
    coord = ThreadCoordinator(_config())
    await coord.initialize()

    try:
        lease_a = await coord.acquire("t1", "serial", timeout=1.0)
        assert lease_a is not None

        with pytest.raises(CoordinatorBusy):
            await coord.acquire("t1", "serial", timeout=0.3)

        await lease_a.release()

        lease_b = await coord.acquire("t1", "serial", timeout=1.0)
        await lease_b.release()
    finally:
        await coord.close()
        await _cleanup("neryva:threadlock:test:t1:serial")


@pytest.mark.asyncio
@pytest.mark.skipif(not _REDIS_UP, reason="Redis not running")
async def test_different_threads_do_not_block():
    coord = ThreadCoordinator(_config())
    await coord.initialize()

    try:
        a = await coord.acquire("t1", "thread-a", timeout=1.0)
        b = await coord.acquire("t1", "thread-b", timeout=1.0)
        await a.release()
        await b.release()
    finally:
        await coord.close()
        await _cleanup("neryva:threadlock:test:t1:thread-a")
        await _cleanup("neryva:threadlock:test:t1:thread-b")


@pytest.mark.asyncio
@pytest.mark.skipif(not _REDIS_UP, reason="Redis not running")
async def test_redis_lock_is_shared_across_workers():
    """A raw SET NX (simulating another worker) blocks the coordinator."""
    import redis.asyncio as redis

    coord = ThreadCoordinator(_config(lease_seconds=10))
    await coord.initialize()
    key = "neryva:threadlock:test:t1:cross"

    client = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        other_worker_token = "other-worker"
        await client.set(key, other_worker_token, nx=True, px=10_000)

        with pytest.raises(CoordinatorBusy):
            await coord.acquire("t1", "cross", timeout=0.3)
    finally:
        await client.delete(key)
        await client.close()
        await coord.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not _REDIS_UP, reason="Redis not running")
async def test_lease_renewal_keeps_key_alive():
    import redis.asyncio as redis

    coord = ThreadCoordinator(_config(lease_seconds=5, prefix="neryva:threadlock:test:"))
    await coord.initialize()
    key = "neryva:threadlock:test:t1:renew"

    client = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        lease = await coord.acquire("t1", "renew", timeout=1.0)
        token = await client.get(key)
        assert token is not None

        await asyncio.sleep(2.5)  # > one renewal interval (lease/3 = 1.67s)
        assert await client.get(key) == token

        await lease.release()
        assert await client.get(key) is None
    finally:
        await client.delete(key)
        await client.close()
        await coord.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not _REDIS_UP, reason="Redis not running")
async def test_release_is_idempotent():
    coord = ThreadCoordinator(_config())
    await coord.initialize()

    try:
        lease = await coord.acquire("t1", "idem", timeout=1.0)
        await lease.release()
        await lease.release()  # no error
        lease2 = await coord.acquire("t1", "idem", timeout=1.0)
        await lease2.release()
    finally:
        await coord.close()
        await _cleanup("neryva:threadlock:test:t1:idem")


# ---- degradation ---------------------------------------------------------


def test_redis_down_degraded_and_local_only():
    async def _run():
        coord = ThreadCoordinator(
            CoordinatorConfig(redis_url=DEAD_REDIS_URL, wait_seconds=1.0)
        )
        await coord.initialize()
        try:
            assert coord.degraded is True
            status = await coord.health_check()
            assert status.status is HealthStatus.DEGRADED

            first = await coord.acquire("t1", "local", timeout=0.5)
            with pytest.raises(CoordinatorBusy):
                await coord.acquire("t1", "local", timeout=0.2)
            await first.release()

            second = await coord.acquire("t1", "local", timeout=0.5)
            await second.release()
        finally:
            await coord.close()

    asyncio.run(_run())
