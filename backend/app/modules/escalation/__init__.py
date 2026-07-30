"""
Escalation and human handoff module.

Handles confidence-based escalation, handoff preparation, and integration
with external ticketing/support systems.
"""

import structlog
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from backend.app.domain.policy import PolicyAction
from backend.app.domain.tenant import TenantConfig

logger = structlog.get_logger(__name__)


@dataclass
class HandoffRequest:
    """Request for human handoff."""

    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    session_id: str = ""
    user_message: str = ""
    model_response: str | None = None
    confidence: float = 0.0
    reason: str = ""
    policy_action: PolicyAction = PolicyAction.ESCALATE
    conversation_history: list[dict[str, str]] = field(default_factory=list)
    attempted_resolutions: list[str] = field(default_factory=list)
    recommended_next_step: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    status: str = "pending"  # pending, assigned, resolved, closed
    assigned_to: str | None = None

    def to_ticket_payload(self) -> dict[str, Any]:
        """Convert to ticket system payload."""
        return {
            "subject": f"Neryva Escalation - Confidence: {self.confidence:.2f}",
            "description": self._build_description(),
            "priority": self._calculate_priority(),
            "tags": ["neryva", "escalation", str(self.tenant_id)],
            "metadata": {
                "session_id": self.session_id,
                "handoff_id": str(self.id),
                "confidence": self.confidence,
                "reason": self.reason,
            },
        }

    def _build_description(self) -> str:
        """Build detailed description for human agent."""
        parts = [
            f"**Escalation Reason:** {self.reason}",
            f"**Confidence Score:** {self.confidence:.2f}",
            f"**User Message:** {self.user_message}",
            "",
            "**Conversation History:**",
        ]

        for msg in self.conversation_history[-5:]:  # Last 5 messages
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            parts.append(f"- {role.capitalize()}: {content}")

        if self.model_response:
            parts.extend(["", "**Model's Proposed Response:**", self.model_response])

        if self.attempted_resolutions:
            parts.extend(["", "**Attempted Resolutions:**"])
            for resolution in self.attempted_resolutions:
                parts.append(f"- {resolution}")

        if self.recommended_next_step:
            parts.extend(["", "**Recommended Next Step:**", self.recommended_next_step])

        return "\n".join(parts)

    def _calculate_priority(self) -> str:
        """Calculate priority based on confidence and policy action."""
        if self.confidence < 0.3:
            return "high"
        elif self.confidence < 0.5:
            return "medium"
        else:
            return "low"


@dataclass
class HandoffResponse:
    """Response from handoff processing."""

    success: bool
    ticket_id: str | None = None
    assignment_url: str | None = None
    message: str = ""
    estimated_wait_time_minutes: int | None = None


class EscalationService:
    """Service for handling escalations and human handoffs."""

    def __init__(
        self,
        tenant_config: TenantConfig,
        ticketing_webhook_url: str | None = None,
        helpdesk_integration: str | None = None,
    ):
        self.tenant_config = tenant_config
        self.ticketing_webhook_url = ticketing_webhook_url
        self.helpdesk_integration = helpdesk_integration or "generic"
        self._ticketing_client: Any | None = None

    def should_escalate(
        self,
        confidence: float,
        policy_action: PolicyAction,
        explicit_request: bool = False,
    ) -> bool:
        """Determine if escalation is needed."""
        # Explicit user request for human
        if explicit_request:
            return True

        # Policy requires escalation
        if policy_action == PolicyAction.ESCALATE:
            return True

        # Confidence below threshold
        if confidence < self.tenant_config.escalation_threshold:
            return True

        return False

    def detect_explicit_handoff_request(self, message: str) -> bool:
        """Detect if user explicitly requests human assistance."""
        handoff_phrases = [
            "speak to a human",
            "talk to a person",
            "human agent",
            "real person",
            "customer service",
            "support agent",
            "escalate this",
            "supervisor",
            "manager",
        ]

        message_lower = message.lower()
        return any(phrase in message_lower for phrase in handoff_phrases)

    async def create_handoff(
        self,
        session_id: str,
        user_message: str,
        confidence: float,
        reason: str,
        policy_action: PolicyAction,
        conversation_history: list[dict[str, str]],
        model_response: str | None = None,
        attempted_resolutions: list[str] | None = None,
    ) -> HandoffResponse:
        """Create a handoff request."""
        handoff = HandoffRequest(
            tenant_id=self.tenant_config.id,
            session_id=session_id,
            user_message=user_message,
            model_response=model_response,
            confidence=confidence,
            reason=reason,
            policy_action=policy_action,
            conversation_history=conversation_history,
            attempted_resolutions=attempted_resolutions or [],
            recommended_next_step=self._generate_recommendation(
                confidence, reason, model_response
            ),
        )

        logger.info(
            "handoff_created",
            handoff_id=handoff.id,
            tenant_id=self.tenant_config.id,
            confidence=confidence,
            reason=reason,
        )

        # Send to ticketing system
        try:
            response = await self._send_to_ticketing_system(handoff)
            return response
        except Exception as e:
            logger.error("handoff_ticketing_error", error=str(e))
            return HandoffResponse(
                success=False,
                message=f"Handoff recorded but ticketing system unavailable: {str(e)}",
            )

    def _generate_recommendation(
        self, confidence: float, reason: str, model_response: str | None
    ) -> str:
        """Generate recommended next step for human agent."""
        if confidence < 0.3:
            return "Low confidence response - please review conversation context and provide accurate assistance."
        elif "policy" in reason.lower():
            return "Policy violation detected - review against tenant guidelines before responding."
        elif "blocked" in reason.lower():
            return "Content blocked by guardrails - verify if block was appropriate and respond accordingly."
        else:
            return "Standard escalation - continue conversation from where bot left off."

    async def _send_to_ticketing_system(
        self, handoff: HandoffRequest
    ) -> HandoffResponse:
        """Send handoff to external ticketing system."""
        if not self.ticketing_webhook_url:
            # No external integration - return local success
            logger.warning("no_ticketing_integration", handoff_id=handoff.id)
            return HandoffResponse(
                success=True,
                message="Handoff recorded locally. No external ticketing system configured.",
                estimated_wait_time_minutes=None,
            )

        # Generic webhook integration
        import aiohttp

        payload = handoff.to_ticket_payload()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.ticketing_webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    if response.status in (200, 201):
                        result = await response.json()
                        ticket_id = result.get("ticket_id", str(handoff.id))
                        return HandoffResponse(
                            success=True,
                            ticket_id=ticket_id,
                            message=f"Ticket created: {ticket_id}",
                            estimated_wait_time_minutes=15,  # Default estimate
                        )
                    else:
                        error_text = await response.text()
                        raise Exception(f"Ticketing API error: {error_text}")
        except Exception as e:
            logger.error("ticketing_webhook_error", error=str(e))
            raise

    def format_handoff_message(self, handoff: HandoffRequest) -> str:
        """Format handoff message for user."""
        return (
            "I'm connecting you with a human specialist who can better assist you. "
            "Please hold on for a moment. "
            f"(Reference ID: {handoff.id})"
        )


@dataclass
class EscalationMetrics:
    """Metrics for escalation tracking."""

    total_escalations: int = 0
    escalations_by_reason: dict[str, int] = field(default_factory=dict)
    average_confidence_at_escalation: float = 0.0
    average_resolution_time_minutes: float = 0.0
    tickets_created: int = 0
    tickets_resolved: int = 0


class EscalationMetricsCollector:
    """Collects and tracks escalation metrics."""

    def __init__(self):
        self.metrics = EscalationMetrics()
        self._escalation_confidences: list[float] = []

    def record_escalation(self, reason: str, confidence: float) -> None:
        """Record an escalation event."""
        self.metrics.total_escalations += 1

        # Track by reason
        reason_key = reason.split("-")[0].strip() if "-" in reason else reason
        self.metrics.escalations_by_reason[reason_key] = (
            self.metrics.escalations_by_reason.get(reason_key, 0) + 1
        )

        # Track confidence
        self._escalation_confidences.append(confidence)

        # Update average
        if self._escalation_confidences:
            self.metrics.average_confidence_at_escalation = (
                sum(self._escalation_confidences) / len(self._escalation_confidences)
            )

    def record_ticket_created(self) -> None:
        """Record ticket creation."""
        self.metrics.tickets_created += 1

    def record_ticket_resolved(self, resolution_time_minutes: float) -> None:
        """Record ticket resolution."""
        self.metrics.tickets_resolved += 1

        # Update average resolution time (simple moving average)
        total = self.metrics.tickets_resolved
        old_avg = self.metrics.average_resolution_time_minutes
        self.metrics.average_resolution_time_minutes = (
            (old_avg * (total - 1) + resolution_time_minutes) / total
        )

    def get_summary(self) -> dict[str, Any]:
        """Get metrics summary."""
        return {
            "total_escalations": self.metrics.total_escalations,
            "escalations_by_reason": self.metrics.escalations_by_reason,
            "average_confidence_at_escalation": round(
                self.metrics.average_confidence_at_escalation, 3
            ),
            "average_resolution_time_minutes": round(
                self.metrics.average_resolution_time_minutes, 2
            ),
            "tickets_created": self.metrics.tickets_created,
            "tickets_resolved": self.metrics.tickets_resolved,
            "resolution_rate": (
                self.metrics.tickets_resolved / self.metrics.tickets_created
                if self.metrics.tickets_created > 0
                else 0.0
            ),
        }


# Singleton instance
_escalation_service: EscalationService | None = None
_metrics_collector: EscalationMetricsCollector | None = None


def get_escalation_service(
    tenant_config: TenantConfig,
    ticketing_webhook_url: str | None = None,
    helpdesk_integration: str | None = None,
) -> EscalationService:
    """Get or create escalation service instance."""
    global _escalation_service
    if _escalation_service is None:
        _escalation_service = EscalationService(
            tenant_config=tenant_config,
            ticketing_webhook_url=ticketing_webhook_url,
            helpdesk_integration=helpdesk_integration,
        )
    return _escalation_service


def get_metrics_collector() -> EscalationMetricsCollector:
    """Get or create metrics collector instance."""
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = EscalationMetricsCollector()
    return _metrics_collector


def create_escalation_service(
    tenant_config: TenantConfig,
    ticketing_webhook_url: str | None = None,
    helpdesk_integration: str | None = None,
) -> EscalationService:
    """Factory function to create escalation service."""
    return EscalationService(
        tenant_config=tenant_config,
        ticketing_webhook_url=ticketing_webhook_url,
        helpdesk_integration=helpdesk_integration,
    )