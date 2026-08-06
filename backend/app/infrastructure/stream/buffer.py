"""
Server-side streaming chunk buffer (Arch 9.1, Phase 4).

Live token deltas are *live-only* fragments until the turn completes — but
the connection tier appends each delta to this buffer as it emits it, so a
client reconnect with ``Last-Event-ID`` can replay every chunk it already
acked and then receive the terminal ``result``/``error`` event, without
re-generating the answer (the durable message was already persisted once).

Storage: one Redis key per turn stream (``neryva:stream:{tenant}:{stream}``)
holding the ordered chunk list JSON, with a TTL. Redis down — or an
in-memory fallback — keeps the buffer working in-process and reports
DEGRADED health, matching the hot-tier/cache convention (never silent,
self-healing on reconnection).
"""

import json
import structlog
import time
from typing import Any, Optional

from backend.app.infrastructure.patterns.health import HealthComponent, HealthStatus
from backend.app.infrastructure.patterns.lifecycle import ManagedService

logger = structlog.get_logger(__name__)


class StreamBuffer(ManagedService):
    """Durable per-turn chunk buffer with replay and terminal markers."""

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        *,
        prefix: str = "neryva:stream:buffer:",
        ttl_seconds: int = 3600,
    ):
        super().__init__("stream_buffer")
        self.redis_url = redis_url
        self.prefix = prefix
        self.ttl_seconds = ttl_seconds
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

    def _key(self, tenant_id: str, stream_id: str) -> str:
        return f"{self.prefix}{tenant_id}:{stream_id}"

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

    # -- public API -----------------------------------------------------------

    async def append(
        self, tenant_id: str, stream_id: str, event_type: str, payload: dict[str, Any]
    ) -> int:
        """Append a chunk/event and return its monotonic sequence id.

        The first append seeds the buffer; each call bumps the TTL. Returns
        the id the client should send back as ``Last-Event-ID``.
        """
        self._seq += 1
        seq = self._seq
        record = {"id": seq, "type": event_type, "data": payload}
        key = self._key(tenant_id, stream_id)
        if not self._redis_available:
            return self._mem_append(key, record)
        try:
            await self._redis.rpush(key, json.dumps(record, separators=(",", ":")))
            await self._redis.expire(key, self.ttl_seconds)
            return seq
        except Exception as e:
            self._mark_redis_down(key, e)
            return self._mem_append(key, record)

    async def replay(
        self, tenant_id: str, stream_id: str, after_event_id: int = 0
    ) -> list[dict[str, Any]]:
        """Ordered chunks with ``id`` strictly greater than ``after_event_id``."""
        key = self._key(tenant_id, stream_id)
        if not self._redis_available:
            return self._mem_read(key, after_event_id)
        try:
            raw = await self._redis.lrange(key, 0, -1)
            if raw:
                await self._redis.expire(key, self.ttl_seconds)  # promote
            records = [json.loads(r) for r in raw]
            return [r for r in records if r.get("id", 0) > after_event_id]
        except Exception as e:
            self._mark_redis_down(key, e)
            return self._mem_read(key, after_event_id)

    async def mark_terminal(
        self, tenant_id: str, stream_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        """Record the turn's terminal event (``result`` or ``error``)."""
        await self.append(tenant_id, stream_id, event_type, payload)

    async def get_terminal(
        self, tenant_id: str, stream_id: str
    ) -> dict[str, Any] | None:
        """The terminal (``result``/``error``) event, or None while the
        stream is still running / unknown."""
        chunks = await self.replay(tenant_id, stream_id)
        for record in reversed(chunks):
            if record["type"] in ("result", "error"):
                return record
        return None

    async def clear(self, tenant_id: str, stream_id: str) -> bool:
        key = self._key(tenant_id, stream_id)
        if not self._redis_available:
            self._memory.pop(key, None)
            return True
        try:
            await self._redis.delete(key)
            self._memory.pop(key, None)
            return True
        except Exception as e:
            self._mark_redis_down(key, e)
            self._memory.pop(key, None)
            return False

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