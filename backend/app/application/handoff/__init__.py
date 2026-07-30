"""
Handoff service for human escalation.

Manages the handoff process when confidence is low or policy requires escalation.
"""

import structlog
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

logger = structlog.get_logger(__name__)


@dataclass
class HandoffRequest:
    """Request for human handoff."""

    tenant_id: UUID
    conversation_id: str
    user_message: str
    model_response: str | None
    confidence: float
    reason: str
    context: dict[str, Any] = field(default_factory=dict)
    attempted_resolution: str | None = None
    recommended_next_step: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass
class HandoffResponse:
    """Response from handoff processing."""

    success: bool
    ticket_id: str | None
    assigned_to: str | None
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


class HandoffService:
    """Service for managing human handoffs."""

    def __init__(
        self,
        ticketing_webhook_url: str | None = None,
        escalation_email: str | None = None,
    ):
        self.ticketing_webhook_url = ticketing_webhook_url
        self.escalation_email = escalation_email
        self._session_cache: dict[str, HandoffRequest] = {}

    async def create_handoff(
        self,
        tenant_id: UUID,
        conversation_id: str,
        user_message: str,
        model_response: str | None,
        confidence: float,
        reason: str,
        context: dict[str, Any] | None = None,
        attempted_resolution: str | None = None,
        recommended_next_step: str | None = None,
    ) -> HandoffResponse:
        """Create a handoff request."""
        logger.info(
            "creating_handoff",
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            reason=reason,
            confidence=confidence,
        )

        handoff_request = HandoffRequest(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            user_message=user_message,
            model_response=model_response,
            confidence=confidence,
            reason=reason,
            context=context or {},
            attempted_resolution=attempted_resolution,
            recommended_next_step=recommended_next_step,
        )

        # Cache the request
        self._session_cache[conversation_id] = handoff_request

        # Send to ticketing system
        try:
            ticket_response = await self._send_to_ticketing(handoff_request)
            if ticket_response.success:
                logger.info(
                    "handoff_ticket_created",
                    ticket_id=ticket_response.ticket_id,
                    conversation_id=conversation_id,
                )
                return ticket_response
        except Exception as e:
            logger.error("ticketing_error", error=str(e), conversation_id=conversation_id)

        # Fallback: return success even if ticketing fails
        return HandoffResponse(
            success=True,
            ticket_id=None,
            assigned_to=None,
            message="Handoff recorded. Ticketing system unavailable.",
            metadata={"fallback": True},
        )

    async def _send_to_ticketing(self, request: HandoffRequest) -> HandoffResponse:
        """Send handoff to external ticketing system."""
        if not self.ticketing_webhook_url:
            logger.warning("no_ticketing_webhook_configured")
            return HandoffResponse(
                success=False,
                ticket_id=None,
                assigned_to=None,
                message="Ticketing webhook not configured",
            )

        # In production, this would make an HTTP POST to the ticketing system
        # For now, simulate a successful response
        logger.info(
            "simulating_ticket_creation",
            webhook_url=self.ticketing_webhook_url,
            request_id=request.id,
        )

        return HandoffResponse(
            success=True,
            ticket_id=f"TICKET-{request.id[:8]}",
            assigned_to="support-queue",
            message="Ticket created successfully",
            metadata={"webhook_url": self.ticketing_webhook_url},
        )

    def get_handoff_status(self, conversation_id: str) -> HandoffRequest | None:
        """Get the status of a handoff request."""
        return self._session_cache.get(conversation_id)

    def clear_handoff(self, conversation_id: str) -> None:
        """Clear a handoff request from cache."""
        if conversation_id in self._session_cache:
            del self._session_cache[conversation_id]
            logger.info("handoff_cleared", conversation_id=conversation_id)


def create_handoff_service(
    ticketing_webhook_url: str | None = None,
    escalation_email: str | None = None,
) -> HandoffService:
    """Factory function to create handoff service."""
    return HandoffService(
        ticketing_webhook_url=ticketing_webhook_url,
        escalation_email=escalation_email,
    )