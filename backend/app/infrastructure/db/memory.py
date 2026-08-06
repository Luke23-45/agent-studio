"""
Memory store repository (Arch 8.4, P2-8).

Durable, PII-filtered facts extracted from closed turns, scoped per tenant
and optionally per end-user. Reads never see erased facts or expired facts;
erasure is a soft delete (audit-safe, ties P5-10). Extraction watermarks
derive from ``max(source_seq)`` per thread, so re-running a thread's
extraction is a no-op for turns already covered.
"""

import structlog
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import Boolean, func, select, update

from .manager import DatabaseManager
from .models import MemoryModel, MessageModel, ThreadModel
from .repositories import _row_to_dict

logger = structlog.get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryRepository:
    def __init__(self, db: DatabaseManager):
        self.db = db

    async def add(
        self,
        *,
        tenant_id: str,
        end_user_id: str | None,
        thread_id: str,
        source_seq: int,
        content: str,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Store one PII-filtered fact (called only after PII pass)."""
        async with self.db.get_session() as session:
            row = MemoryModel(
                id=str(uuid4()),
                tenant_id=tenant_id,
                end_user_id=end_user_id,
                thread_id=thread_id,
                source_seq=source_seq,
                content=content,
                version=1,
                expires_at=expires_at,
                erased=False,
            )
            session.add(row)
            await session.flush()
            return _row_to_dict(row)

    async def retrieve(
        self,
        tenant_id: str,
        *,
        end_user_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Live facts (not erased, not expired) for a tenant/end-user.

        ``end_user_id`` provided -> that user's own facts only; omitted ->
        tenant-global facts (API-key sessions without an end-user). Newest
        first. Relevance scoring (top-k on demand) happens in the
        application layer against the current message.
        """
        now = _now()
        async with self.db.get_session() as session:
            stmt = (
                select(MemoryModel)
                .where(
                    MemoryModel.tenant_id == tenant_id,
                    MemoryModel.erased.is_(False),
                )
                .order_by(MemoryModel.created_at.desc())
                .limit(limit)
            )
            if end_user_id is not None:
                stmt = stmt.where(MemoryModel.end_user_id == end_user_id)
            elif end_user_id is None:
                stmt = stmt.where(MemoryModel.end_user_id.is_(None))
            stmt = stmt.where(
                (MemoryModel.expires_at.is_(None))
                | (MemoryModel.expires_at > now)
            )
            result = await session.execute(stmt)
            return [_row_to_dict(r) for r in result.scalars()]

    async def max_source_seq(self, tenant_id: str, thread_id: str) -> int:
        """Extraction watermark: newest source_seq already extracted."""
        async with self.db.get_session() as session:
            result = await session.execute(
                select(func.max(MemoryModel.source_seq)).where(
                    MemoryModel.tenant_id == tenant_id,
                    MemoryModel.thread_id == thread_id,
                )
            )
            return result.scalar() or 0

    async def erase_by_user(self, tenant_id: str, end_user_id: str) -> int:
        """Soft-erase every fact for one end-user (erasure support, P5-10)."""
        async with self.db.get_session() as session:
            result = await session.execute(
                update(MemoryModel)
                .where(
                    MemoryModel.tenant_id == tenant_id,
                    MemoryModel.end_user_id == end_user_id,
                    MemoryModel.erased.is_(False),
                )
                .values(erased=True)
            )
            return result.rowcount or 0

    async def erase_by_thread(self, tenant_id: str, thread_id: str) -> int:
        """Soft-erase every fact extracted from one thread."""
        async with self.db.get_session() as session:
            result = await session.execute(
                update(MemoryModel)
                .where(
                    MemoryModel.tenant_id == tenant_id,
                    MemoryModel.thread_id == thread_id,
                    MemoryModel.erased.is_(False),
                )
                .values(erased=True)
            )
            return result.rowcount or 0

    async def erase_by_id(self, tenant_id: str, memory_id: str) -> bool:
        """Soft-erase one fact."""
        async with self.db.get_session() as session:
            result = await session.execute(
                update(MemoryModel)
                .where(
                    MemoryModel.tenant_id == tenant_id,
                    MemoryModel.id == memory_id,
                    MemoryModel.erased.is_(False),
                )
                .values(erased=True)
            )
            return (result.rowcount or 0) > 0

    async def prune_expired(self, tenant_id: str, *, now: datetime | None = None) -> int:
        """Soft-erase every expired fact for a tenant (retrieval hygiene)."""
        async with self.db.get_session() as session:
            result = await session.execute(
                update(MemoryModel)
                .where(
                    MemoryModel.tenant_id == tenant_id,
                    MemoryModel.erased.is_(False),
                    MemoryModel.expires_at.is_not(None),
                    MemoryModel.expires_at <= (now or _now()),
                )
                .values(erased=True)
            )
            return result.rowcount or 0

    async def list_threads_pending_extraction(self, limit: int = 50) -> list[dict[str, Any]]:
        """Active threads with messages newer than their extraction watermark.

        Sweep source for the ``memory.extract`` job: a thread whose newest
        message outgrew ``max(source_seq)`` of its stored facts has new
        closed turns to extract. Returns thread rows with ``latest_seq``.
        """
        async with self.db.get_session() as session:
            latest = (
                select(
                    MessageModel.thread_id,
                    func.max(MessageModel.seq).label("latest_seq"),
                )
                .group_by(MessageModel.thread_id)
                .subquery()
            )
            extracted = (
                select(
                    MemoryModel.thread_id,
                    func.max(MemoryModel.source_seq).label("extracted_seq"),
                )
                .group_by(MemoryModel.thread_id)
                .subquery()
            )
            stmt = (
                select(ThreadModel, latest.c.latest_seq)
                .join(latest, latest.c.thread_id == ThreadModel.id)
                .outerjoin(extracted, extracted.c.thread_id == ThreadModel.id)
                .where(
                    ThreadModel.status == "active",
                    ThreadModel.archived.is_(False),
                    latest.c.latest_seq
                    > func.coalesce(extracted.c.extracted_seq, 0),
                )
                .order_by(latest.c.latest_seq.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = []
            for thread, latest_seq in result.all():
                row = _row_to_dict(thread)
                row["latest_seq"] = latest_seq
                rows.append(row)
            return rows
