"""
Conversation domain models and business logic.

Handles conversation state, message history, and session management.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4


class MessageRole(Enum):
    """Role of a message sender."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ConfidenceLevel(Enum):
    """Confidence levels for assistant responses."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class Message:
    """A single message in a conversation."""

    id: UUID = field(default_factory=uuid4)
    role: MessageRole = MessageRole.USER
    content: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class Conversation:
    """A conversation session."""

    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID | None = None
    user_id: str | None = None
    messages: list[Message] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    confidence_level: ConfidenceLevel = ConfidenceLevel.HIGH
    is_escalated: bool = False

    def add_message(self, role: MessageRole, content: str, metadata: dict | None = None) -> Message:
        """Add a message to the conversation."""
        message = Message(role=role, content=content, metadata=metadata or {})
        self.messages.append(message)
        self.updated_at = datetime.utcnow()
        return message

    def get_recent_messages(self, limit: int = 10) -> list[Message]:
        """Get recent messages from the conversation."""
        return self.messages[-limit:]

    def set_confidence(self, level: ConfidenceLevel) -> None:
        """Set the confidence level for the conversation."""
        self.confidence_level = level

    def escalate(self) -> None:
        """Mark conversation for escalation."""
        self.is_escalated = True
