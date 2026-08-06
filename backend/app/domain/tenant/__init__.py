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

    # Loop/runaway budgets (P1: human-in-the-loop + loop budgets)
    budgets: dict[str, float] = field(default_factory=lambda: {
        "max_redact_iterations": 2.0,
        "max_graph_steps": 50.0,
        "max_duration_s": 60.0,
    })

    # Feature flags
    features: dict[str, bool] = field(default_factory=dict)

    # Tool-result clearing (Arch 8.3, P2-7): keep_recent_turns = newest
    # turns whose tool payloads stay intact (the in-flight conversation
    # remains fully legible); older payloads are reclaimed in the
    # background when features["clear_tool_results"] is enabled.
    tool_clearing: dict[str, float] = field(default_factory=lambda: {
        "keep_recent_turns": 2.0,
    })

    # Guardrail configuration
    guardrail_config: dict[str, bool] = field(default_factory=lambda: {
        "regex_fastpath": True,
        "classifier": True,
        "nemo_rails": False,
        "jailbreak_detection": True,
        "output_validation": True,
        "pii_detection": True,
        "spotlighting": True,
    })
    guardrail_thresholds: dict[str, float] = field(default_factory=lambda: {
        "classifier": 0.5,
        "jailbreak": 0.7,
        "pii": 0.5,
    })

    def is_topic_allowed(self, topic: str) -> bool:
        if topic in self.blocked_topics:
            return False
        if self.allowed_topics and topic not in self.allowed_topics:
            return False
        return True

    def should_escalate(self, confidence: float) -> bool:
        return confidence < self.escalation_threshold

    def is_guardrail_enabled(self, name: str) -> bool:
        return self.guardrail_config.get(name, True)

    def get_allowed_topics(self) -> list[str]:
        return self.allowed_topics

    def get_guardrail_threshold(self, name: str) -> float:
        return self.guardrail_thresholds.get(name, 0.5)
