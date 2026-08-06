from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4


@dataclass
class HandoffRequest:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    session_id: str = ""
    user_message: str = ""
    model_response: str | None = None
    confidence: float = 0.0
    reason: str = ""
    policy_action: Any = None
    conversation_history: list[dict[str, str]] = field(default_factory=list)
    attempted_resolutions: list[str] = field(default_factory=list)
    recommended_next_step: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    status: str = "pending"
    assigned_to: str | None = None

    def to_ticket_payload(self) -> dict[str, Any]:
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
        parts = [
            f"**Escalation Reason:** {self.reason}",
            f"**Confidence Score:** {self.confidence:.2f}",
            f"**User Message:** {self.user_message}",
            "",
            "**Conversation History:**",
        ]
        for msg in self.conversation_history[-5:]:
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
        if self.confidence < 0.3:
            return "high"
        elif self.confidence < 0.5:
            return "medium"
        return "low"


@dataclass
class HandoffResponse:
    success: bool
    ticket_id: str | None = None
    assignment_url: str | None = None
    message: str = ""
    estimated_wait_time_minutes: int | None = None
