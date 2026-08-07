"""Transactional outbox repository (P4-7, EU-AI-Act Art. 12).

Rows are the durable hand-off between a request-side event (recorded at
the request boundary) and the async webhook relay. They stay ``pending``
until the relay has published them exactly once; a crash at any point
leaves the row pending so the next relay pass retries it — events are
never lost and never double-delivered (the webhook publisher keys delivery
by the outbox ``event_id``).
"""

import structlog
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update

from ...infrastructure.db.manager import DatabaseManager
from ...infrastructure.db.models import EventOutboxModel
from ...infrastructure.db.repositories import _row_to_dict

logger = structlog.get_logger(__name__)

STATUS_PENDING = "pending"
STATUS_PUBLISHED = "published"
STATUS_FAILED = "failed"

DEFAULT_OUTBOX_BATCH = 50


class EventOutboxRepository:
    def __init__(self, db: DatabaseManager):
        self.db = db

    async def record(
        self,
        event_id: str,
        tenant_id: str,
        event_type: str,
        payload: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Insert a pending outbox event (idempotent by ``event_id``)."""
        async with self.db.get_session() as session:
            existing = await session.execute(
                select(EventOutboxModel).where(EventOutboxModel.event_id == event_id)
            )
            if existing.scalar_one_or_none() is not None:
                return {"event_id": event_id, "idempotent_replay": True}
            model = EventOutboxModel(
                id=str(uuid4()),
                event_id=event_id,
                tenant_id=str(tenant_id),
                event_type=event_type,
                payload=payload,
                session_id=session_id,
                status=STATUS_PENDING,
                attempts=0,
            )
            session.add(model)
            await session.flush()
            return _row_to_dict(model)

    async def list_pending(self, limit: int = DEFAULT_OUTBOX_BATCH) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(EventOutboxModel)
                .where(EventOutboxModel.status == STATUS_PENDING)
                .order_by(EventOutboxModel.created_at)
                .limit(limit)
            )
            return [_row_to_dict(r) for r in result.scalars()]

    async def mark_published(self, event_id: str) -> None:
        async with self.db.get_session() as session:
            await session.execute(
                update(EventOutboxModel)
                .where(EventOutboxModel.event_id == event_id)
                .values(status=STATUS_PUBLISHED, attempts=EventOutboxModel.attempts + 1)
            )

    async def mark_failed(self, event_id: str, error: str) -> None:
        async with self.db.get_session() as session:
            await session.execute(
                update(EventOutboxModel)
                .where(EventOutboxModel.event_id == event_id)
                .values(
                    status=STATUS_FAILED,
                    attempts=EventOutboxModel.attempts + 1,
                    last_error=error[:512],
                )
            )
            logger.error("outbox_event_failed", event_id=event_id, error=error)

    async def count_pending(self) -> int:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(func.count())
                .select_from(EventOutboxModel)
                .where(EventOutboxModel.status == STATUS_PENDING)
            )
            return int(result.scalar() or 0)


__all__ = [
    "DEFAULT_OUTBOX_BATCH",
    "EventOutboxRepository",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_PUBLISHED",
]