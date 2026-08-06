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
        action, _ = self.evaluate_with_results(context)
        return action

    def evaluate_with_results(self, context: dict[str, Any]) -> tuple[PolicyAction, list[dict[str, Any]]]:
        """Evaluate rules against context.

        Returns ``(action, matching_rules)`` where ``matching_rules`` is a list
        of dicts with rule id/name/action/conditions/priority, suitable for an
        audit evidence packet.
        """
        matching = [r for r in self.rules if r.matches(context)]
        if not matching:
            return PolicyAction.ALLOW, []

        # Sort by priority (higher first)
        matching.sort(key=lambda r: r.priority, reverse=True)
        rule_details = [
            {
                "rule_id": str(r.id),
                "name": r.name,
                "action": r.action.value,
                "policy_type": r.policy_type.value,
                "conditions": r.conditions,
                "priority": r.priority,
            }
            for r in matching
        ]
        return matching[0].action, rule_details
