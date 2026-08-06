"""
P1-8 end-user limits tests:

- concurrent-session lease counter caps per end user (Redis)
- leaving a session frees the slot; disjoint users are independent
- spend cap blocks after record_spend crosses it; no cap = always allowed
- per-user rate limiting consumes from the user's own bucket
- Redis down: everything fails open, health reports DEGRADED
"""

import asyncio

import pytest

from backend.app.infrastructure.patterns.health import HealthStatus
from backend.app.session.limits import EndUserLimits, EndUserLimitsConfig

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


def _config(**kwargs) -> EndUserLimitsConfig:
    kwargs.setdefault("prefix", "neryva:eu:test:")
    return EndUserLimitsConfig(redis_url=REDIS_URL, **kwargs)


async def _cleanup() -> None:
    import redis.asyncio as redis

    client = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        for key in await client.keys("neryva:eu:test:*"):
            await client.delete(key)
        for key in await client.keys("neryva:rl:eu:*"):
            await client.delete(key)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not _REDIS_UP, reason="Redis not running")
async def test_concurrent_session_cap():
    limits = EndUserLimits(_config(max_concurrent_sessions=2))
    await limits.initialize()
    try:
        assert await limits.enter_session("t1", "eu-1") is True
        assert await limits.enter_session("t1", "eu-1") is True
        assert await limits.enter_session("t1", "eu-1") is False  # over cap

        await limits.leave_session("t1", "eu-1")
        assert await limits.enter_session("t1", "eu-1") is True

        # disjoint users are independent
        assert await limits.enter_session("t1", "eu-2") is True
        assert await limits.enter_session("t1", "eu-2") is True
    finally:
        await limits.close()
        await _cleanup()


@pytest.mark.asyncio
@pytest.mark.skipif(not _REDIS_UP, reason="Redis not running")
async def test_spend_cap_blocks_after_crossing():
    limits = EndUserLimits(_config(spend_cap_tokens=100))
    await limits.initialize()
    try:
        assert await limits.check_spend_cap("t1", "eu-1") is True
        await limits.record_spend("t1", "eu-1", 60.0)
        assert await limits.check_spend_cap("t1", "eu-1") is True
        await limits.record_spend("t1", "eu-1", 50.0)
        assert await limits.check_spend_cap("t1", "eu-1") is False
    finally:
        await limits.close()
        await _cleanup()


@pytest.mark.asyncio
@pytest.mark.skipif(not _REDIS_UP, reason="Redis not running")
async def test_no_cap_always_allowed():
    limits = EndUserLimits(_config(spend_cap_tokens=0))
    await limits.initialize()
    try:
        await limits.record_spend("t1", "eu-1", 10 ** 9)
        assert await limits.check_spend_cap("t1", "eu-1") is True
    finally:
        await limits.close()
        await _cleanup()


@pytest.mark.asyncio
@pytest.mark.skipif(not _REDIS_UP, reason="Redis not running")
async def test_per_user_rate_limit():
    limits = EndUserLimits(_config(max_requests=3, window_seconds=60))
    await limits.initialize()
    try:
        for _ in range(3):
            assert await limits.check_rate("t1", "eu-1") is True
        assert await limits.check_rate("t1", "eu-1") is False  # bucket empty

        # other user has its own bucket
        assert await limits.check_rate("t1", "eu-2") is True
    finally:
        await limits.close()
        await _cleanup()


def test_redis_down_fails_open_and_reports_degraded():
    async def _run():
        limits = EndUserLimits(EndUserLimitsConfig(redis_url=DEAD_REDIS_URL))
        await limits.initialize()
        try:
            assert limits.degraded is True
            assert (await limits.health_check()).status is HealthStatus.DEGRADED

            assert await limits.enter_session("t1", "eu-1") is True  # fail-open
            await limits.leave_session("t1", "eu-1")
            assert await limits.check_spend_cap("t1", "eu-1") is True
            assert await limits.check_rate("t1", "eu-1") is True
        finally:
            await limits.close()

    asyncio.run(_run())
