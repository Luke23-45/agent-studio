"""
P0-13 multi-worker safety tests:

- two OS processes share queue state through Redis (skip when Redis is down)
- with Redis unreachable, two processes still boot and fall back in-process
- degraded services report DEGRADED health (never silent)
"""

import asyncio
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
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

_BOOT_SCRIPT = textwrap.dedent(
    """
    import asyncio
    import json
    import sys

    from backend.app.infrastructure.queue.manager import Job, init_queue

    async def main():
        url, action = sys.argv[1], sys.argv[2]
        q = init_queue(redis_url=url)
        await q.initialize()
        if action == "enqueue":
            await q.enqueue(Job(type="noop", payload={"from": "proc-a"}))
            print("enqueued")
        elif action == "dequeue":
            job = await q.dequeue(timeout=3)
            print(json.dumps({"job": job.payload if job else None}))
        await q.close()

    asyncio.run(main())
    """
)


def _run_boot_script(url: str, action: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _BOOT_SCRIPT, url, action],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=timeout,
    )


@pytest.mark.skipif(not _REDIS_UP, reason="Redis not running")
def test_two_processes_share_queue_state():
    """Enqueue in process A is visible to process B (shared Redis keyspace)."""
    a = _run_boot_script(REDIS_URL, "enqueue")
    assert a.returncode == 0, a.stderr
    b = _run_boot_script(REDIS_URL, "dequeue")
    assert b.returncode == 0, b.stderr
    assert json.loads(b.stdout)["job"] == {"from": "proc-a"}


@pytest.mark.skipif(not _REDIS_UP, reason="Redis not running")
def test_two_processes_boot_against_same_redis():
    """Both processes initialize cleanly against the same Redis."""
    for _ in range(2):
        p = _run_boot_script(REDIS_URL, "enqueue")
        assert p.returncode == 0, p.stderr


def test_redis_down_both_processes_boot_in_process():
    """Redis unreachable: both processes boot and degrade (never crash)."""
    for _ in range(2):
        p = _run_boot_script(DEAD_REDIS_URL, "enqueue")
        assert p.returncode == 0, p.stderr


def test_degradation_is_visible():
    """Rate limiter and admission gate report DEGRADED without Redis."""
    from backend.app.gateway.admission import AdmissionConfig, ConcurrencyGate
    from backend.app.infrastructure.patterns.health import HealthStatus
    from backend.app.infrastructure.patterns.rate_limiter import (
        RateLimitConfig,
        RateLimiter,
    )

    async def _run():
        limiter = RateLimiter(
            RateLimitConfig(max_requests=10), redis_url=DEAD_REDIS_URL
        )
        await limiter.initialize()
        gate = ConcurrencyGate(AdmissionConfig(redis_url=DEAD_REDIS_URL))
        await gate.initialize()

        assert limiter.degraded is True
        assert (await limiter.health_check()).status is HealthStatus.DEGRADED
        assert (await gate.health_check()).status is HealthStatus.DEGRADED

        await gate.close()

    asyncio.run(_run())
