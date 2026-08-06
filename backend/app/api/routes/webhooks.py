"""
Webhook subscription management + event replay (matrix 1.7).

Subscriptions are tenant-scoped. Delivering signed payloads to subscriber
URLs happens asynchronously via the ``webhook.deliver`` worker job;
``POST /webhooks/events/{event_id}/replay`` re-enqueues delivery.
"""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.app.api.dependencies.auth import (
    ApiKeyPrincipal,
    assert_tenant_access,
    require_permission,
)
from backend.app.infrastructure.db import (
    WebhookRepository,
    get_database_manager,
)
from backend.app.infrastructure.queue import Job, get_queue_manager
from backend.app.modules.webhooks import JOB_WEBHOOK_DELIVER

logger = structlog.get_logger(__name__)

router = APIRouter()


class WebhookSubscriptionRequest(BaseModel):
    """Register (or reactivate) a webhook subscription for a tenant."""

    tenant_id: str
    url: str = Field(min_length=5, max_length=1024)
    secret: str = Field(min_length=16, max_length=256)
    events: list[str] = Field(min_length=1)
    active: bool = True


class WebhookSubscriptionResponse(BaseModel):
    id: str
    tenant_id: str
    url: str
    events: list[str]
    active: bool


@router.get(
    "/webhooks/subscriptions",
    response_model=list[WebhookSubscriptionResponse],
)
async def list_webhook_subscriptions(
    tenant_id: str,
    principal: ApiKeyPrincipal = Depends(require_permission("webhooks:read")),
):
    assert_tenant_access(principal, tenant_id)
    rows = await WebhookRepository(get_database_manager()).list_subscriptions(tenant_id)
    return [
        WebhookSubscriptionResponse(
            id=r["id"],
            tenant_id=r["tenant_id"],
            url=r["url"],
            events=r["events"],
            active=r["active"],
        )
        for r in rows
    ]


@router.post(
    "/webhooks/subscriptions",
    response_model=WebhookSubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_webhook_subscription(
    request: WebhookSubscriptionRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("webhooks:write")),
):
    assert_tenant_access(principal, request.tenant_id)
    row = await WebhookRepository(get_database_manager()).create_subscription(
        tenant_id=request.tenant_id,
        url=request.url,
        secret=request.secret,
        events=request.events,
    )
    return WebhookSubscriptionResponse(
        id=row["id"],
        tenant_id=row["tenant_id"],
        url=row["url"],
        events=row["events"],
        active=row["active"],
    )


@router.delete("/webhooks/subscriptions/{subscription_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook_subscription(
    subscription_id: str,
    tenant_id: str,
    principal: ApiKeyPrincipal = Depends(require_permission("webhooks:write")),
):
    assert_tenant_access(principal, tenant_id)
    deleted = await WebhookRepository(get_database_manager()).delete_subscription(
        subscription_id, tenant_id
    )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    return None


@router.get("/webhooks/events")
async def list_webhook_events(
    tenant_id: str | None = None,
    event_type: str | None = None,
    limit: int = 100,
    principal: ApiKeyPrincipal = Depends(require_permission("webhooks:read")),
):
    repo = WebhookRepository(get_database_manager())
    if tenant_id:
        assert_tenant_access(principal, tenant_id)
    return await repo.list_events(tenant_id=tenant_id, event_type=event_type, limit=limit)


@router.get("/webhooks/events/{event_id}/deliveries")
async def list_webhook_deliveries(
    event_id: str,
    principal: ApiKeyPrincipal = Depends(require_permission("webhooks:read")),
):
    repo = WebhookRepository(get_database_manager())
    event = await repo.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    if event["tenant_id"]:
        assert_tenant_access(principal, event["tenant_id"])
    return await repo.list_deliveries(event_id=event_id)


@router.post("/webhooks/events/{event_id}/replay")
async def replay_webhook_event(
    event_id: str,
    principal: ApiKeyPrincipal = Depends(require_permission("webhooks:write")),
):
    """Re-enqueue delivery of a stored event to every matching subscription.

    Replays are idempotent per event+subscription pair, so a repeated call
    can never double-deliver (queue-level idempotency key).
    """
    repo = WebhookRepository(get_database_manager())
    event = await repo.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    if event["tenant_id"]:
        assert_tenant_access(principal, event["tenant_id"])

    subscriptions = await repo.list_subscriptions(event["tenant_id"])
    matching = [
        s for s in subscriptions
        if s["active"] and event["event_type"] in (s.get("events") or [])
    ]
    if not matching:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active subscription matches this event",
        )

    queue = get_queue_manager()
    for subscription in matching:
        await queue.enqueue(
            Job(
                type=JOB_WEBHOOK_DELIVER,
                payload={
                    "event_id": event_id,
                    "subscription_id": subscription["id"],
                    "tenant_id": event["tenant_id"],
                },
            ),
            idempotency_key=f"webhook:{event_id}:{subscription['id']}",
        )
    logger.info("webhook_event_replayed", event_id=event_id, subscriptions=len(matching))
    return {"event_id": event_id, "replayed": len(matching)}
