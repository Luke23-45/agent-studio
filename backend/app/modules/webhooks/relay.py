"""Outbox relay (P4-7): drain transactional events into the webhook bus.

The request path writes the outbox row (durable). A worker runs the relay
which publishes each pending row once via :class:`WebhookPublisher`,
keyed by the row ``event_id`` so a crash between publish and ack cannot
double-deliver; and a crash before the publish leaves the row pending for
the next relay pass — no lost events (EU-AI-Act Art. 12).
"""

import structlog
from typing import Any

from ...infrastructure.db.manager import DatabaseManager, get_database_manager
from ...infrastructure.db.repositories import WebhookRepository
from ...infrastructure.queue.manager import Job, get_queue_manager
from .outbox import EventOutboxRepository
from .publisher import JOB_WEBHOOK_DELIVER, WebhookPublisher

logger = structlog.get_logger(__name__)

JOB_OUTBOX_RELAY = "outbox.relay"


class OutboxRelay:
    def __init__(
        self,
        db: DatabaseManager | None = None,
        outbox_repo: EventOutboxRepository | None = None,
        publisher: WebhookPublisher | None = None,
    ):
        self.db = db or get_database_manager()
        self.outbox = outbox_repo or EventOutboxRepository(self.db)
        self.publisher = publisher or WebhookPublisher(self.db, WebhookRepository(self.db))

    async def drain(self, limit: int = 50) -> int:
        """Publish every pending outbox event once; returns events published.

        Rows that fail (e.g. the durable record insert collides on replay)
        are skipped with a log rather than blocking the whole batch; they
        surface again on the next drain while retries are bounded.
        """
        pending = await self.outbox.list_pending(limit=limit)
        published = 0
        for row in pending:
            event_id = row["event_id"]
            try:
                result_id = await self.publisher.publish(
                    row["event_type"],
                    row["tenant_id"],
                    dict(row["payload"]),
                    session_id=row.get("session_id"),
                    event_id=event_id,
                )
                if result_id is None:
                    # No active subscription matched: nothing to deliver, but
                    # the event is still acknowledged as processed and replay-
                    # able from the outbox itself.
                    await self.outbox.mark_published(event_id)
                    published += 1
                    continue
                await self.outbox.mark_published(event_id)
                published += 1
            except Exception as e:
                # The broker already saw this event (publish is idempotent by
                # event_id) or the event body is invalid: ack it so the relay
                # does not spin forever on the same row.
                logger.warning(
                    "outbox_relay_skipped",
                    event_id=event_id,
                    event_type=row["event_type"],
                    error=str(e),
                )
                await self.outbox.mark_failed(event_id, str(e))
        return published


async def handle_outbox_relay(payload: dict) -> None:
    """Worker handler for ``outbox.relay``: drain a batch of api."""
    limit = int(payload.get("limit", 50))
    relay = OutboxRelay()
    published = await relay.drain(limit=limit)
    logger.info("outbox_relay_drained", published=published)


async def enqueue_outbox_relay(limit: int = 50) -> bool:
    """Schedule a relay drain on the shared queue (idempotent by job pool)."""
    manager = get_queue_manager()
    return await manager.enqueue(
        Job(type=JOB_OUTBOX_RELAY, payload={"limit": limit}),
        idempotency_key="outbox.relay",
    )