"""
P5-9 — Evidence & audit.

One validated evidence packet per policy decision (guardrail, tool gate,
quota), scoped by tenant + end-user, conforming to
``contracts/schemas/evidence-packet.schema.json`` so decisions are
reconstructable from evidence alone (EU-AI-Act Art. 12). Also exposes the
retention-policy helpers used by the config pipeline (ties P6-8).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from backend.app.domain.policy import PolicyAction

_VALID_DECISIONS = {"ALLOW", "BLOCK", "ESCALATE", "REDACT"}
_VALID_DIRECTIONS = {"policy"}


@dataclass(frozen=True)
class EvidencePacket:
    """Schema-valid evidence packet (direction=policy per the contract).

    The same shape persisted via ``GuardrailEvidenceModel``; ``to_dict()``
    is JSON-serializable and validated by the tests against the schema.
    """

    tenant_id: str
    conversation_id: str | None
    session_id: str
    direction: str = "policy"
    decision: str = "ALLOW"
    allowed: bool = True
    input_hash: str = ""
    violations: Sequence[Mapping[str, Any]] = field(default_factory=list)
    layers_evaluated: Sequence[str] = field(default_factory=list)
    processing_time_ms: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "direction": self.direction,
            "decision": self.decision,
            "allowed": self.allowed,
            "input_hash": self.input_hash,
            "violations": [dict(v) for v in self.violations],
            "layers_evaluated": list(self.layers_evaluated),
            "processing_time_ms": float(self.processing_time_ms),
            "metadata": dict(self.metadata),
        }


def hash_input(text: str) -> str:
    """sha256 hex of the decision input, per the packet schema pattern."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _violation(
    *,
    rule_id: str | None,
    name: str,
    action: str,
    policy_type: str | None,
    conditions: dict[str, Any],
    priority: int,
) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "name": name,
        "action": action,
        "policy_type": policy_type,
        "conditions": conditions,
        "priority": priority,
    }


def _violation_from(violation: Mapping[str, Any]) -> dict[str, Any]:
    """Accept domain-style violation dicts; guarantee schema keys."""
    return _violation(
        rule_id=violation.get("rule_id"),
        name=violation.get("name") or "policy",
        action=violation.get("action") or "block",
        policy_type=violation.get("policy_type"),
        conditions=dict(violation.get("conditions") or {}),
        priority=violation.get("priority", 0),
    )


def build_packet(
    *,
    tenant_id: str,
    session_id: str,
    decision: PolicyAction | str,
    input_hash: str,
    conversation_id: str | None = None,
    violations: Sequence[Mapping[str, Any]] = (),
    layers_evaluated: Sequence[str] = (),
    processing_time_ms: float = 0.0,
    metadata: Mapping[str, Any] | None = None,
    allowed: bool | None = None,
) -> EvidencePacket:
    """Build an evidence packet for a policy decision.

    ``decision`` accepts a domain ``PolicyAction`` or a raw string; both are
    normalized to the schema enum, and non-enum values become ESCALATE.
    """
    normalized = (
        decision.name
        if isinstance(decision, PolicyAction)
        else str(decision).upper()
    )
    if normalized not in _VALID_DECISIONS:
        normalized = "ESCALATE"
    if allowed is None:
        allowed = normalized == "ALLOW"

    return EvidencePacket(
        tenant_id=str(tenant_id),
        conversation_id=conversation_id,
        session_id=session_id,
        decision=normalized,
        allowed=allowed,
        input_hash=input_hash or hash_input(session_id),
        violations=[_violation_from(v) for v in violations],
        layers_evaluated=list(layers_evaluated),
        processing_time_ms=float(processing_time_ms),
        metadata=dict(metadata or {}),
    )


def build_tool_gate_packet(
    *,
    tenant_id: str,
    session_id: str,
    tool_name: str,
    reason: str | None,
    conversation_id: str | None = None,
    surface_id: str | None = None,
) -> EvidencePacket:
    """Evidence for a denied tool call (P5-3 denial audit)."""
    return build_packet(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        session_id=session_id,
        decision="BLOCK",
        input_hash=hash_input(tool_name),
        layers_evaluated=["tool_authorization"],
        violations=[
            {
                "rule_id": None,
                "name": "tool-gate-denied",
                "action": "block",
                "policy_type": "tool_authorization",
                "conditions": {
                    "tool": tool_name,
                    "reason": reason or "",
                    "surface_id": surface_id or "",
                },
                "priority": 1,
            }
        ],
        metadata={"decision_class": "tool_gate", "tool": tool_name},
    )


def build_quota_packet(
    *,
    tenant_id: str,
    session_id: str,
    conversation_id: str | None,
    level: str,
    limit_usd: float,
    projected_usd: float,
) -> EvidencePacket:
    """Evidence packet for a budget-hierarchy rejection (P5-7/P5-9)."""
    return build_packet(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        session_id=session_id,
        decision="BLOCK",
        input_hash=hash_input(f"{level}:{limit_usd}:{projected_usd}"),
        layers_evaluated=["budget_hierarchy"],
        violations=[
            {
                "rule_id": None,
                "name": "quota-exceeded",
                "action": "block",
                "policy_type": "budget",
                "conditions": {
                    "level": level,
                    "limit_usd": limit_usd,
                    "projected_usd": projected_usd,
                },
                "priority": 1,
            }
        ],
        metadata={"decision_class": "quota", "level": level},
    )


def evidence_is_valid(packet: EvidencePacket) -> None:
    """Validate packet shape against the contract schema (P5-9).

    Raises ``TagValidationError`` (jsonschema) when the packet does not
    match ``contracts/schemas/evidence-packet.schema.json``.
    """
    from jsonschema import Draft202012Validator
    from pathlib import Path

    schema_path = (
        Path(__file__).resolve().parents[3] / "contracts" / "schemas"
        / "evidence-packet.schema.json"
    )
    import json

    Draft202012Validator(json.loads(schema_path.read_text())).validate(
        packet.to_dict()
    )