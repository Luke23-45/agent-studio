"""
Server-side streaming chunk buffer (Arch 9.1, Phase 4).

Live token deltas are *live-only* fragments until the turn completes — but
the connection tier appends each delta to this buffer as it emits it, so a
client reconnect with ``Last-Event-ID`` can replay every chunk it already
acked and then receive the terminal ``result``/``error`` event, without
re-generating the answer (the durable message was already persisted once).

Storage: one Redis key per turn stream (``neryva:stream:{tenant}:{thread}:{stream}``)
holding the ordered chunk list JSON, with a TTL. Redis down — or an
in-memory fallback — keeps the buffer working in-process and reports
DEGRADED health, matching the hot-tier/cache convention (never silent,
self-healing on reconnection).
"""

import json
import structlog
import time
from typing import Any, Optional

from backend.app.infrastructure.db.manager import DatabaseManager
from backend.app.infrastructure.patterns.health import HealthComponent, HealthStatus
from backend.app.infrastructure.patterns.lifecycle import ManagedService
from backend.app.infrastructure.stream.overflow import StreamBufferOverflowRepository

logger = structlog.get_logger(__name__)


class StreamBuffer(ManagedService):
    """Durable per-turn chunk buffer with replay and terminal markers.

    ``overflow_max_chunks`` caps the hot tier (Redis list, or the process
    memory fallback) per stream; any chunk beyond the cap is persisted to
    the Postgres overflow tier (if ``db`` was provided) instead of being
    dropped, so a reconnect replays the same ordered bytes even after the
    hot tier overflowed or Redis came back down.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        *,
        prefix: str = "neryva:stream:buffer:",
        ttl_seconds: int = 3600,
        db: DatabaseManager | None = None,
        overflow_max_chunks: int = 0,
    ):
        super().__init__("stream_buffer")
        self.redis_url = redis_url
        self.prefix = prefix
        self.ttl_seconds = ttl_seconds
        self.db = db
        self.overflow_max_chunks = max(0, int(overflow_max_chunks or 0))
        self._overflow: StreamBufferOverflowRepository | None = None
        self._redis: Any = None
        self._redis_available = False
        # In-memory fallback: {key: {"seq": int, "entries": [...], "ttl": float}}
        self._memory: dict[str, dict[str, Any]] = {}
        # Per-process monotonic event id (strictly increasing; the client
        # sends these back as ``Last-Event-ID``).
        self._seq = int(time.monotonic() * 1000)

    @property
    def degraded(self) -> bool:
        return not self._redis_available

    def _key(self, tenant_id: str, thread_id: str, stream_id: str) -> str:
        return f"{self.prefix}{tenant_id}:{thread_id}:{stream_id}"

    async def _do_initialize(self) -> None:
        if self.db is not None:
            self._overflow = StreamBufferOverflowRepository(self.db)
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
            logger.info("stream_buffer_redis_connected", url=self.redis_url)
        except Exception as e:
            self._redis = None
            self._redis_available = False
            logger.error(
                "stream_buffer_redis_unavailable_in_memory_fallback",
                error=str(e),
                hint="chunk buffers stay in this process until Redis recovers",
            )

    async def _do_close(self) -> None:
        if self._redis:
            await self._redis.close()
            self._redis = None
            self._redis_available = False
        self._memory.clear()

    async def _do_health_check(self) -> HealthComponent:
        if not self._redis_available:
            return HealthComponent(
                name=self.name,
                status=HealthStatus.DEGRADED,
                message="Redis unavailable; stream buffers are process-local",
            )
        return HealthComponent(
            name=self.name,
            status=HealthStatus.HEALTHY,
            metadata={"ttl_seconds": self.ttl_seconds},
        )

    # -- in-memory helpers ----------------------------------------------------

    def _mem_get(self, key: str) -> dict[str, Any] | None:
        entry = self._memory.get(key)
        if entry is None:
            return None
        if time.monotonic() > entry["ttl"]:
            self._memory.pop(key, None)
            return None
        return entry

    def _mem_append(self, key: str, record: dict[str, Any]) -> int:
        entry = self._mem_get(key)
        if entry is None:
            entry = {"seq": record["id"], "entries": [], "ttl": time.monotonic() + self.ttl_seconds}
            self._memory[key] = entry
        entry.setdefault("entries", []).append(record)
        entry["ttl"] = time.monotonic() + self.ttl_seconds
        return record["id"]

    def _mem_read(self, key: str, after: int) -> list[dict[str, Any]]:
        entry = self._mem_get(key)
        if entry is None:
            return []
        return [r for r in entry["entries"] if r["id"] > after]

    # -- overflow helpers -----------------------------------------------------

    async def _spill_overflow(
        self, tenant_id: str, thread_id: str, stream_id: str,
        spills: list[dict[str, Any]], *, reason: str,
    ) -> None:
        """Move chunks out of the hot tier into the Postgres overflow."""
        if not spills:
            return
        if self._overflow is None:
            # No durable overflow configured: the oldest chunks are dropped
            # (logged, never silent) — the hot tier still holds the window.
            logger.warning(
                "stream_buffer_overflow_no_db_dropping",
                reason=reason,
                dropped=len(spills),
            )
            return
        try:
            await self._overflow.write_chunks(
                tenant_id=tenant_id,
                thread_id=thread_id,
                stream_id=stream_id,
                chunks=spills,
            )
        except Exception as e:
            # Overflow persistence is belt-and-braces; the hot tier keeps the
            # valid window. Log, never raise into the request path.
            logger.error("stream_buffer_overflow_write_failed", reason=reason, error=str(e))

    # -- public API -----------------------------------------------------------

    async def append(
        self, tenant_id: str, thread_id: str, stream_id: str, event_type: str, payload: dict[str, Any]
    ) -> int:
        """Append a chunk/event and return its monotonic sequence id.

        The first append seeds the buffer; each call bumps the TTL. Ingress
        over ``overflow_max_chunks`` spills the oldest chunks to the durable
        Postgres tier (if configured). Returns the id the client should send
        back as ``Last-Event-ID``.
        """
        self._seq += 1
        seq = self._seq
        record = {"id": seq, "type": event_type, "data": payload}
        key = self._key(tenant_id, thread_id, stream_id)
        if not self._redis_available:
            self._mem_append(key, record)
            entry = self._mem_get(key)
            spills: list[dict[str, Any]] = []
            if (
                self.overflow_max_chunks
                and entry is not None
                and len(entry["entries"]) > self.overflow_max_chunks
            ):
                excess = entry["entries"][: len(entry["entries"]) - self.overflow_max_chunks]
                entry["entries"] = entry["entries"][len(excess):]
                spills = excess
            await self._spill_overflow(
                tenant_id, thread_id, stream_id, spills,
                reason="memory-cap",
            )
            return seq
        try:
            await self._redis.rpush(key, json.dumps(record, separators=(",", ":")))
            await self._redis.expire(key, self.ttl_seconds)
            if self.overflow_max_chunks:
                size = await self._redis.llen(key)
                if size and size > self.overflow_max_chunks:
                    excess = size - self.overflow_max_chunks
                    spill_raw = await self._redis.lrange(key, 0, excess - 1)
                    await self._redis.ltrim(key, excess, -1)
                    await self._spill_overflow(
                        tenant_id, thread_id, stream_id,
                        [json.loads(r) for r in spill_raw],
                        reason="redis_cap",
                    )
            return seq
        except Exception as e:
            self._mark_redis_down(key, e)
            self._mem_append(key, record)
            return seq

    async def replay(
        self, tenant_id: str, thread_id: str, stream_id: str, after_event_id: int = 0
    ) -> list[dict[str, Any]]:
        """Ordered chunks with ``id`` strictly greater than ``after_event_id``."""
        key = self._key(tenant_id, thread_id, stream_id)
        hot: list[dict[str, Any]] = []
        if not self._redis_available:
            hot = self._mem_read(key, after_event_id)
        else:
            try:
                raw = await self._redis.lrange(key, 0, -1)
                if raw:
                    await self._redis.expire(key, self.ttl_seconds)  # promote
                hot = [json.loads(r) for r in raw]
            except Exception as e:
                self._mark_redis_down(key, e)
                hot = self._mem_read(key, after_event_id)

        overflow: list[dict[str, Any]] = []
        if self._overflow is not None:
            try:
                overflow = await self._overflow.read_chunks(
                    tenant_id=tenant_id,
                    thread_id=thread_id,
                    stream_id=stream_id,
                    after_seq=after_event_id,
                )
            except Exception:
                logger.error("stream_buffer_overflow_read_failed", key=key)

        seen: dict[int, dict[str, Any]] = {r["id"]: r for r in overflow}
        for r in hot:
            seen.setdefault(r["id"], r)
        return sorted(seen.values(), key=lambda r: r["id"])

    async def mark_terminal(
        self, tenant_id: str, thread_id: str, stream_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        """Record the turn's terminal event (``result`` or ``error``)."""
        await self.append(tenant_id, thread_id, stream_id, event_type, payload)

    async def get_terminal(
        self, tenant_id: str, thread_id: str, stream_id: str
    ) -> dict[str, Any] | None:
        """The terminal (``result``/``error``) event, or None while the
        stream is still running / unknown."""
        chunks = await self.replay(tenant_id, thread_id, stream_id)
        for record in reversed(chunks):
            if record["type"] in ("result", "error"):
                return record
        return None

    async def clear(self, tenant_id: str, thread_id: str, stream_id: str) -> bool:
        key = self._key(tenant_id, thread_id, stream_id)
        ok = True
        if not self._redis_available:
            self._memory.pop(key, None)
        else:
            try:
                await self._redis.delete(key)
                self._memory.pop(key, None)
            except Exception as e:
                self._mark_redis_down(key, e)
                self._memory.pop(key, None)
                ok = False
        if self._overflow is not None:
            try:
                await self._overflow.clear(
                    tenant_id=tenant_id, thread_id=thread_id, stream_id=stream_id
                )
            except Exception:
                logger.error("stream_buffer_overflow_clear_failed", key=key)
                ok = False
        return ok

    # -- degraded bookkeeping -------------------------------------------------

    def _mark_redis_down(self, key: str, error: Exception) -> None:
        """Flip Redis availability off once per outage (logged, never silent)."""
        if self._redis_available:
            self._redis_available = False
            logger.error(
                "stream_buffer_redis_down_in_memory_fallback", key=key, error=str(error)
            )


_buffer: Optional[StreamBuffer] = None


def init_stream_buffer(redis_url: str, **kwargs: Any) -> StreamBuffer:
    global _buffer
    _buffer = StreamBuffer(redis_url, **kwargs)
    return _buffer


def get_stream_buffer() -> StreamBuffer:
    if _buffer is None:
        raise RuntimeError("stream buffer not initialized")
    return _buffer