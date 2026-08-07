"""Publish webhook events (matrix 1.7).

The publisher stores every event durably (``webhook_events`` — the replay
source) and enqueues one delivery job per matching active subscription,
with an idempotency key so a crash/re-run can never double-deliver.
"""

import structlog
from typing import Any
from uuid import uuid4

from backend.app.infrastructure.db import (
    DatabaseManager,
    WebhookRepository,
    get_database_manager,
)
from backend.app.infrastructure.queue import Job, get_queue_manager

logger = structlog.get_logger(__name__)

JOB_WEBHOOK_DELIVER = "webhook.deliver"

# Conversation events wired in the request path (matrix 0.3).
EVENT_CONVERSATION_COMPLETED = "conversation.completed"
EVENT_GUARDRAIL_BLOCKED = "guardrail.blocked"
EVENT_CONVERSATION_CREATED = "conversation.created"
EVENT_ESCALATION_RAISED = "escalation.raised"
EVENT_EVAL_FAILED = "eval.failed"


class WebhookPublisher:
    """Durable event publication with per-subscription delivery jobs."""

    def __init__(
        self,
        db: DatabaseManager | None = None,
        repository: WebhookRepository | None = None,
    ):
        self.db = db or get_database_manager()
        self.repository = repository or WebhookRepository(self.db)

    async def publish(
        self,
        event_type: str,
        tenant_id: str,
        data: dict[str, Any],
        session_id: str | None = None,
        event_id: str | None = None,
    ) -> str | None:
        """Persist the event and enqueue deliveries for matching subscriptions.

        Returns the event id, or ``None`` when no subscription matches
        (event is still recorded for replay). An explicit ``event_id`` is
        honored (transactional outbox reuse) so replaying the same outbox
        row never double-publishes.
        """
        tenant_id = str(tenant_id)
        subscriptions = await self.repository.list_subscriptions(tenant_id)
        matching = [s for s in subscriptions if s["active"] and event_type in (s.get("events") or [])]
        if not matching:
            return None

        event_id = event_id or str(uuid4())
        await self.repository.record_event(
            event_id=event_id,
            tenant_id=tenant_id,
            event_type=event_type,
            payload={"session_id": session_id, **data},
        )

        queue = get_queue_manager()
        for subscription in matching:
            await queue.enqueue(
                Job(
                    type=JOB_WEBHOOK_DELIVER,
                    payload={
                        "event_id": event_id,
                        "subscription_id": subscription["id"],
                        "tenant_id": tenant_id,
                    },
                ),
                idempotency_key=f"webhook:{event_id}:{subscription['id']}",
            )
        logger.info(
            "webhook_event_published",
            event_id=event_id,
            event_type=event_type,
            tenant_id=tenant_id,
            subscriptions=len(matching),
        )
        return event_id
