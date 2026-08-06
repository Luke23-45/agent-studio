"""Outbound webhook delivery over HTTP with HMAC signatures (matrix 1.7).

A single call performs one delivery attempt. Retries with exponential
backoff and dead-lettering are handled by the queue layer (the worker
handler raises on failure); this class decides what is retryable:
network errors, timeouts and 5xx are transient; 4xx responses are not.
"""

import json
import structlog
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .signer import signature_header

logger = structlog.get_logger(__name__)


@dataclass
class DeliveryResult:
    success: bool
    http_status: int | None = None
    error: str | None = None
    attempts: int = 1


@dataclass
class WebhookEnvelope:
    """The signed, serialized payload sent to a subscriber."""

    event_id: str
    event_type: str
    tenant_id: str
    data: dict[str, Any]
    occurred_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def body(self) -> bytes:
        return json.dumps(
            {
                "event_id": self.event_id,
                "event_type": self.event_type,
                "tenant_id": self.tenant_id,
                "occurred_at": self.occurred_at,
                "data": self.data,
            },
            default=str,
        ).encode("utf-8")


class WebhookDeliverer:
    """POSTs signed envelopes to a subscription URL."""

    def __init__(self, timeout_seconds: float = 10.0):
        self.timeout_seconds = timeout_seconds

    async def deliver(
        self,
        url: str,
        secret: str,
        envelope: WebhookEnvelope,
    ) -> DeliveryResult:
        import httpx

        body = envelope.body()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Neryva-Agent-Studio/1.0",
            "X-Neryva-Event-Id": envelope.event_id,
            "X-Neryva-Event-Type": envelope.event_type,
            "X-Neryva-Timestamp": envelope.occurred_at,
            "X-Neryva-Signature": signature_header(secret, body),
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(url, content=body, headers=headers)
        except Exception as e:
            logger.warning("webhook_delivery_error", url=url, error=str(e))
            return DeliveryResult(success=False, error=str(e))

        if response.status_code >= 200 and response.status_code < 300:
            logger.info(
                "webhook_delivered",
                url=url,
                event_id=envelope.event_id,
                status=response.status_code,
            )
            return DeliveryResult(success=True, http_status=response.status_code)

        # 4xx is a permanent subscriber-side rejection: do not retry.
        if 400 <= response.status_code < 500:
            error = f"subscriber rejected payload (HTTP {response.status_code})"
        else:
            error = f"transient failure (HTTP {response.status_code})"
        logger.warning("webhook_delivery_failed", url=url, status=response.status_code)
        return DeliveryResult(success=False, http_status=response.status_code, error=error)
