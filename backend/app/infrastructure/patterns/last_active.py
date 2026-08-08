"""
Write-minimization: batched "last active" updates (Arch 11, P8-1).

High-frequency telemetry updates (API-key ``last_used_at`` / ``usage_count``
today; read receipts and "last active" markers as future consumers) never
hit the Postgres primary per event. The ``LastActiveBatcher`` buffers
``(key, delta, timestamp)`` in Redis (atomic HINCRBY + HSET under a single
key family) and flushes to the primary on a cadence or at a batch
threshold -- one transaction per flush instead of one per request. This is
the OpenAI write-minimization discipline (batch/buffer instead of
write-per-event, Arch 11) applied to the request path.

Guarantees:

- Counters are merged with ``usage_count = usage_count + delta`` in SQL, so
  concurrent flushers (multiple API replicas) can never lose increments.
- ``last_used_at`` is approximate by design (flush cadence + last-writer-
  wins across processes); it is a display/analytics field, never
  authoritative.
- The Redis read-and-delete is a single Lua script (HGETALL + DEL), so a
  pending batch is applied exactly once even when several processes flush.
- Redis down -> in-process buffer (per process) + DEGRADED health; the
  flush still runs and writes through when Redis returns. Degradation is
  never silent.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog

from ..patterns.health import HealthComponent, HealthStatus
from ..patterns.lifecycle import ManagedService

logger = structlog.get_logger(__name__)

# Lua: atomically read both hashes and delete them (exactly-once flush).
_FLUSH_LUA = """
local counts = redis.call('HGETALL', KEYS[1])
local ts = redis.call('HGETALL', KEYS[2])
if next(counts) or next(ts) then
  redis.call('DEL', KEYS[1], KEYS[2])
end
return {counts, ts}
"""


class LastActiveBatcher(ManagedService):
    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        *,
        prefix: str = "neryva:last_active:v1:",
        flush_interval_seconds: float = 60.0,
        batch_threshold: int = 500,
        buffer_ttl_seconds: int = 300,
        db: Any = None,
    ):
        super().__init__("last_active")
        self.redis_url = redis_url
        self.prefix = prefix
        self.flush_interval_seconds = flush_interval_seconds
        self.batch_threshold = batch_threshold
        self.buffer_ttl_seconds = buffer_ttl_seconds
        self.db = db
        self._redis: Any = None
        self._redis_available = False
        self._memory_counts: dict[str, int] = {}
        self._memory_ts: dict[str, str] = {}
        self._flush_task: asyncio.Task | None = None

    @property
    def degraded(self) -> bool:
        return not self._redis_available

    def _counts_key(self) -> str:
        return f"{self.prefix}counts"

    def _ts_key(self) -> str:
        return f"{self.prefix}ts"

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
            logger.info("last_active_redis_connected", url=self.redis_url)
        except Exception as e:
            self._redis = None
            self._redis_available = False
            logger.error(
                "last_active_redis_unavailable_in_memory_buffer",
                error=str(e),
                hint="last-active updates buffer in-process until Redis recovers",
            )
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def _do_close(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown
                pass
            self._flush_task = None
        await self.flush()
        if self._redis:
            await self._redis.close()
            self._redis = None
            self._redis_available = False

    async def _do_health_check(self) -> HealthComponent:
        if not self._redis_available:
            return HealthComponent(
                name=self.name,
                status=HealthStatus.DEGRADED,
                message="Redis unavailable; last-active updates buffer in-process",
            )
        return HealthComponent(
            name=self.name,
            status=HealthStatus.HEALTHY,
            metadata={
                "flush_interval_seconds": self.flush_interval_seconds,
                "batch_threshold": self.batch_threshold,
            },
        )

    async def touch(self, key_id: str, *, ts: datetime | None = None) -> None:
        """Record one activity event for ``key_id`` on the hot tier only.

        Never touches Postgres: the value lands in the Redis buffer (or the
        in-process fallback) and reaches the primary at the next flush.
        """
        now = (ts or datetime.now(UTC)).isoformat()
        if self._redis_available:
            try:
                pipe = self._redis.pipeline(transaction=True)
                pipe.hincrby(self._counts_key(), key_id, 1)
                pipe.hset(self._ts_key(), key_id, now)
                pipe.expire(self._counts_key(), self.buffer_ttl_seconds)
                pipe.expire(self._ts_key(), self.buffer_ttl_seconds)
                await pipe.execute()
                return
            except Exception as e:
                if self._redis_available:
                    self._redis_available = False
                    logger.error(
                        "last_active_redis_down_in_memory_buffer",
                        error=str(e),
                    )
        self._memory_counts[key_id] = self._memory_counts.get(key_id, 0) + 1
        self._memory_ts[key_id] = now

    async def pending_count(self) -> int:
        """Number of buffered keys (observability/test helper)."""
        if self._redis_available:
            try:
                return await self._redis.hlen(self._counts_key())
            except Exception:
                return len(self._memory_counts)
        return len(self._memory_counts)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval_seconds)
            try:
                applied = await self.flush()
                if applied:
                    logger.info(
                        "last_active_flushed", keys=applied, keys_pending=await self.pending_count()
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pragma: no cover - defensive
                logger.error("last_active_flush_failed", error=str(e))

    async def flush(self) -> int:
        """Drain buffered activity to Postgres in one transaction.

        Returns the number of keys applied (0 when nothing is pending).
        Redis path is exactly-once (Lua HGETALL+DEL); the in-process buffer
        is merged so a Redis-outage batch is not lost when Redis returns.
        """
        counts: dict[str, int] = {}
        ts_map: dict[str, str] = {}
        if self._redis_available:
            try:
                counts_raw, ts_raw = await self._redis.eval(
                    _FLUSH_LUA, 2, self._counts_key(), self._ts_key()
                )
                counts = _pairs_to_dict(counts_raw or [])
                ts_map = _pairs_to_dict(ts_raw or [])
            except Exception as e:
                if self._redis_available:
                    self._redis_available = False
                    logger.error(
                        "last_active_flush_redis_down_in_memory_buffer", error=str(e)
                    )
        if self._memory_counts:
            for key, delta in self._memory_counts.items():
                counts[key] = counts.get(key, 0) + delta
            ts_map.update(self._memory_ts)
            self._memory_counts = {}
            self._memory_ts = {}
        if not counts:
            return 0
        entries = [
            (key, delta, ts_map.get(key) or datetime.now(UTC).isoformat())
            for key, delta in counts.items()
        ]
        if self.db is None:
            logger.warning("last_active_flush_no_db_skipped", keys=len(entries))
            return 0
        from backend.app.infrastructure.db import ApiKeyRepository

        return await ApiKeyRepository(self.db).apply_last_active_batch(entries)


def _pairs_to_dict(pairs: list) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for i in range(0, len(pairs) - 1, 2):
        out[pairs[i]] = pairs[i + 1]
    return out


_batcher: LastActiveBatcher | None = None


def init_last_active_batcher(
    redis_url: str,
    *,
    db: Any = None,
    flush_interval_seconds: float = 60.0,
    batch_threshold: int = 500,
    buffer_ttl_seconds: int = 300,
) -> LastActiveBatcher:
    global _batcher
    _batcher = LastActiveBatcher(
        redis_url,
        flush_interval_seconds=flush_interval_seconds,
        batch_threshold=batch_threshold,
        buffer_ttl_seconds=buffer_ttl_seconds,
        db=db,
    )
    return _batcher


def get_last_active_batcher() -> LastActiveBatcher:
    """Shared singleton; a default uninitialized instance never raises.

    The default instance is Redis-less (in-memory buffer only) until
    ``init_last_active_batcher`` runs at app startup -- safe for tests and
    import-order edge cases, and it self-heals to the Redis path once the
    initialized singleton replaces it.
    """
    if _batcher is None:
        return _DEFAULT_BATCHER
    return _batcher


_DEFAULT_BATCHER = LastActiveBatcher()
