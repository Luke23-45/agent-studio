"""Postgres overflow tier for the server-side stream buffer (P4-2).

The hot tier is a Redis list (or an in-process memory fallback). When a
stream grows past its in-Redis cap, or Redis is unavailable so the
in-memory fallback is the only hot copy, the oldest chunks are persisted
here so a reconnect — including one landing on a different process after a
restart — still replays the identical ordered bytes. Rows use the same
``(id, type, data)`` shape as the runtime tier and are keyed by the full
stream scope; ``seq`` is the monotonic event id used for ``Last-Event-ID``
resume.
"""

import uuid
from typing import Any

from sqlalchemy import delete, select

from backend.app.infrastructure.db.manager import DatabaseManager
from backend.app.infrastructure.db.models import StreamBufferChunkModel


class StreamBufferOverflowRepository:
    """Read/write access to the durable overflow tier for one buffer."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def write_chunks(
        self,
        *,
        tenant_id: str,
        thread_id: str,
        stream_id: str,
        chunks: list[dict[str, Any]],
    ) -> int:
        """Persist buffered chunks (idempotent by scope+seq). Returns count."""
        if not chunks:
            return 0
        async with self.db.get_session() as session:
            for chunk in chunks:
                seq = int(chunk["id"])
                row = StreamBufferChunkModel(
                    id=self._row_id(tenant_id, thread_id, stream_id, seq),
                    tenant_id=tenant_id,
                    thread_id=thread_id,
                    stream_id=stream_id,
                    seq=seq,
                    event_type=chunk.get("type", ""),
                    payload=chunk.get("data", {}),
                )
                await session.merge(row)
            await session.flush()
            return len(chunks)

    async def read_chunks(
        self,
        *,
        tenant_id: str,
        thread_id: str,
        stream_id: str,
        after_seq: int = 0,
    ) -> list[dict[str, Any]]:
        """Overflow chunks with ``seq`` > ``after_seq``, ordered ascending."""
        async with self.db.get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(StreamBufferChunkModel)
                        .where(
                            StreamBufferChunkModel.tenant_id == tenant_id,
                            StreamBufferChunkModel.thread_id == thread_id,
                            StreamBufferChunkModel.stream_id == stream_id,
                            StreamBufferChunkModel.seq > after_seq,
                        )
                        .order_by(StreamBufferChunkModel.seq.asc())
                    )
                )
                .scalars()
                .all()
            )
            return [
                {"id": row.seq, "type": row.event_type, "data": row.payload}
                for row in rows
            ]

    async def clear(
        self, *, tenant_id: str, thread_id: str, stream_id: str
    ) -> int:
        """Drop all overflow rows for a stream; returns rows deleted."""
        async with self.db.get_session() as session:
            result = await session.execute(
                delete(StreamBufferChunkModel).where(
                    StreamBufferChunkModel.tenant_id == tenant_id,
                    StreamBufferChunkModel.thread_id == thread_id,
                    StreamBufferChunkModel.stream_id == stream_id,
                )
            )
            await session.flush()
            return result.rowcount or 0

    @staticmethod
    def _row_id(tenant_id: str, thread_id: str, stream_id: str, seq: int) -> str:
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_DNS,
                f"stream-buffer:{tenant_id}:{thread_id}:{stream_id}:{seq}",
            )
        )