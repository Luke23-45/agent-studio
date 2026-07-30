"""
Policy domain models and business logic.

Defines tenant policies, guardrails, and authorization rules.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import UUID, uuid4


class PolicyAction(Enum):
    """Actions that can be taken by policy evaluation."""

    ALLOW = "allow"
    BLOCK = "block"
    ESCALATE = "escalate"
    REDACT = "redact"


class PolicyType(Enum):
    """Types of policies."""

    TOPIC_FILTER = "topic_filter"
    PII_REDACTION = "pii_redaction"
    TOOL_AUTHORIZATION = "tool_authorization"
    OUTPUT_VALIDATION = "output_validation"
    ESCALATION = "escalation"


@dataclass
class PolicyRule:
    """A single policy rule."""

    id: UUID = field(default_factory=uuid4)
    name: str = ""
    policy_type: PolicyType = PolicyType.TOPIC_FILTER
    action: PolicyAction = PolicyAction.ALLOW
    conditions: dict[str, Any] = field(default_factory=dict)
    priority: int = 0

    def matches(self, context: dict[str, Any]) -> bool:
        """Check if this rule matches the given context."""
        # Simple implementation - can be extended
        for key, expected_value in self.conditions.items():
            if context.get(key) != expected_value:
                return False
        return True


@dataclass
class PolicySet:
    """Collection of policy rules for a tenant."""

    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID | None = None
    name: str = ""
    rules: list[PolicyRule] = field(default_factory=list)
    version: int = 1

    def evaluate(self, context: dict[str, Any]) -> PolicyAction:
        """Evaluate all rules against context and return highest priority action."""
        matching_rules = [r for r in self.rules if r.matches(context)]
        if not matching_rules:
            return PolicyAction.ALLOW

        # Sort by priority (higher first)
        matching_rules.sort(key=lambda r: r.priority, reverse=True)
        return matching_rules[0].action
