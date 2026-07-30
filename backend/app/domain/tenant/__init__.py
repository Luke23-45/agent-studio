"""
Tenant domain models and business logic.

Handles multi-tenant configuration, isolation, and policy scoping.
"""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4


@dataclass
class TenantConfig:
    """Configuration for a single tenant."""

    id: UUID = field(default_factory=uuid4)
    name: str = ""
    slug: str = ""

    # Policy configuration
    allowed_topics: list[str] = field(default_factory=list)
    blocked_topics: list[str] = field(default_factory=list)
    escalation_threshold: float = 0.7

    # Knowledge base scope
    knowledge_allowlist: list[str] = field(default_factory=list)

    # Model configuration
    default_provider: str = "openai"
    default_model: str = "gpt-4"

    # Feature flags
    features: dict[str, bool] = field(default_factory=dict)

    def is_topic_allowed(self, topic: str) -> bool:
        """Check if a topic is allowed for this tenant."""
        if topic in self.blocked_topics:
            return False
        if self.allowed_topics and topic not in self.allowed_topics:
            return False
        return True

    def should_escalate(self, confidence: float) -> bool:
        """Determine if the request should be escalated based on confidence."""
        return confidence < self.escalation_threshold
