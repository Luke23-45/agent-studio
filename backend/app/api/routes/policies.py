"""
Policy management API (P7-4 admin console policy editor).

Versioned policy sets per tenant: drafts are edited freely, published
revisions are immutable (editing one creates a new draft on top), and
the runtime only ever evaluates the highest *published* version
(``PolicyRepository.get_by_tenant``). Publish transitions are recorded
on the immutable audit trail with the acting key.

Frontend ``PolicyRule`` fields are mapped to the domain/db model:

- ``kind``  (input|output|tool|topic|safety)   <-> ``policy_type``
- ``action`` (block|flag|redact|escalate)      <-> ``PolicyAction``
  (``flag`` maps to ALLOW with ``conditions["flag"] = True``)
- ``pattern``/``description``/``severity`` live in ``conditions``.
"""

from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.app.api.dependencies.auth import (
    ApiKeyPrincipal,
    assert_tenant_access,
    require_mfa_proof,
    require_permission,
)
from backend.app.infrastructure.db import (
    AuditRepository,
    PolicyRepository,
    TenantRepository,
    get_database_manager,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Policies"])

KIND_TO_POLICY_TYPE: dict[str, str] = {
    "input": "pii_redaction",
    "output": "output_validation",
    "tool": "tool_authorization",
    "topic": "topic_filter",
    "safety": "escalation",
}

POLICY_TYPE_TO_KIND: dict[str, str] = {v: k for k, v in KIND_TO_POLICY_TYPE.items()}

ACTION_TO_DB: dict[str, str] = {
    "block": "BLOCK",
    "redact": "REDACT",
    "escalate": "ESCALATE",
    "flag": "ALLOW",
}

DB_TO_ACTION: dict[str, str] = {v: k for k, v in ACTION_TO_DB.items()}


class PolicyRuleInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    kind: str = Field(..., min_length=1, max_length=32)
    action: str = Field(..., min_length=1, max_length=16)
    pattern: str | None = None
    description: str = ""
    enabled: bool = True
    severity: str = "medium"


class PolicySetInput(BaseModel):
    name: str = Field(default="default", min_length=1, max_length=128)
    status: str = Field(default="draft", pattern="^(draft|review)$")
    rules: list[PolicyRuleInput] = Field(default_factory=list)


class PolicySetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    rules: list[PolicyRuleInput] | None = None


class PublishResponse(BaseModel):
    id: str
    tenant_id: str
    version: int
    status: str


def _rule_to_db(rule: PolicyRuleInput) -> dict[str, Any]:
    """Frontend rule shape -> PolicyRuleModel columns."""
    action = ACTION_TO_DB.get(rule.action, rule.action.upper())
    conditions: dict[str, Any] = {}
    if rule.pattern is not None:
        conditions["pattern"] = rule.pattern
    if rule.description:
        conditions["description"] = rule.description
    if rule.severity:
        conditions["severity"] = rule.severity
    if rule.action == "flag":
        conditions["flag"] = True
    return {
        "name": rule.name,
        "policy_type": KIND_TO_POLICY_TYPE.get(rule.kind, rule.kind),
        "action": action,
        "conditions": conditions,
        "priority": 0,
        "enabled": rule.enabled,
    }


def _rule_to_api(rule: dict[str, Any]) -> dict[str, Any]:
    """PolicyRuleModel row -> frontend PolicyRule shape."""
    conditions = rule.get("conditions") or {}
    action = DB_TO_ACTION.get(rule.get("action", ""), str(rule.get("action", "")).lower())
    return {
        "id": rule["id"],
        "name": rule.get("name", ""),
        "kind": POLICY_TYPE_TO_KIND.get(rule.get("policy_type", ""), rule.get("policy_type", "topic")),
        "action": action,
        "pattern": conditions.get("pattern"),
        "description": conditions.get("description", ""),
        "enabled": rule.get("enabled", True),
        "severity": conditions.get("severity", "medium"),
    }


def _set_to_api(row: dict[str, Any]) -> dict[str, Any]:
    """PolicySetModel row (rules included) -> frontend PolicySet shape.

    ``published_at`` is the row's ``updated_at`` when published (the
    publish transition is the last write); ``published_by`` is not
    persisted on the set and is recorded on the audit trail instead.
    """
    status_value = row.get("status", "draft")
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "version": row.get("version", 1),
        "status": status_value,
        "rules": [_rule_to_api(r) for r in row.get("rules") or []],
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "published_at": row.get("updated_at") if status_value == "published" else None,
        "published_by": None,
    }


async def _tenant_or_404(tenant_id: UUID) -> None:
    db = get_database_manager()
    tenant = await TenantRepository(db).get_by_id(str(tenant_id))
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant not found: {tenant_id}",
        )


@router.get("/tenants/{tenant_id}/policies")
async def list_policies(
    tenant_id: UUID,
    principal: ApiKeyPrincipal = Depends(require_permission("policies:read")),
) -> list[dict[str, Any]]:
    """All policy sets for a tenant (every status, newest version first)."""
    assert_tenant_access(principal, tenant_id)
    db = get_database_manager()
    sets = await PolicyRepository(db).list_sets(str(tenant_id))
    return [_set_to_api(s) for s in sets]


@router.post(
    "/tenants/{tenant_id}/policies",
    status_code=status.HTTP_201_CREATED,
)
async def create_policy_set(
    tenant_id: UUID,
    request: PolicySetInput,
    principal: ApiKeyPrincipal = Depends(require_permission("policies:write")),
) -> dict[str, Any]:
    """Create a new policy set (draft or review; publish is explicit)."""
    assert_tenant_access(principal, tenant_id)
    await _tenant_or_404(tenant_id)

    db = get_database_manager()
    repo = PolicyRepository(db)
    row = await repo.create_set(
        str(tenant_id),
        request.name,
        [_rule_to_db(r) for r in request.rules],
        status=request.status,
    )
    await AuditRepository(db).add(
        action="policy_set.created",
        resource_type="policy_set",
        resource_id=row["id"],
        tenant_id=str(tenant_id),
        actor_type="api_key",
        actor_id=principal.key_id,
        details={
            "name": request.name,
            "status": request.status,
            "version": row["version"],
            "rule_count": len(request.rules),
        },
    )
    logger.info(
        "policy_set_created",
        tenant_id=str(tenant_id),
        policy_set_id=row["id"],
        version=row["version"],
        status=request.status,
        actor=principal.key_id,
    )
    return _set_to_api(await repo.get_set(row["id"], str(tenant_id)))


@router.get("/tenants/{tenant_id}/policies/{policy_id}")
async def get_policy_set(
    tenant_id: UUID,
    policy_id: str,
    principal: ApiKeyPrincipal = Depends(require_permission("policies:read")),
) -> dict[str, Any]:
    assert_tenant_access(principal, tenant_id)
    db = get_database_manager()
    row = await PolicyRepository(db).get_set(policy_id, str(tenant_id))
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Policy set not found: {policy_id}",
        )
    return _set_to_api(row)


@router.put("/tenants/{tenant_id}/policies/{policy_id}")
async def update_policy_set(
    tenant_id: UUID,
    policy_id: str,
    request: PolicySetUpdate,
    principal: ApiKeyPrincipal = Depends(require_permission("policies:write")),
) -> dict[str, Any]:
    """Edit a draft in place; editing a published set creates a new draft
    revision on top (published revisions stay immutable)."""
    assert_tenant_access(principal, tenant_id)
    db = get_database_manager()
    repo = PolicyRepository(db)
    existing = await repo.get_set(policy_id, str(tenant_id))
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Policy set not found: {policy_id}",
        )
    rules_db = [_rule_to_db(r) for r in request.rules] if request.rules is not None else None
    row = await repo.update_set_rules(
        policy_id,
        str(tenant_id),
        name=request.name,
        rules=rules_db,
    )
    await AuditRepository(db).add(
        action="policy_set.updated",
        resource_type="policy_set",
        resource_id=row["id"],
        tenant_id=str(tenant_id),
        actor_type="api_key",
        actor_id=principal.key_id,
        details={
            "source_set_id": policy_id,
            "source_status": existing["status"],
            "version": row["version"],
            "status": row["status"],
            "name": row["name"],
            "rule_count": len(row.get("rules") or []),
        },
    )
    return _set_to_api(await repo.get_set(row["id"], str(tenant_id)))


@router.post("/tenants/{tenant_id}/policies/{policy_id}/publish", response_model=PublishResponse)
async def publish_policy_set(
    tenant_id: UUID,
    policy_id: str,
    principal: ApiKeyPrincipal = Depends(require_permission("policies:write")),
    _mfa: ApiKeyPrincipal = Depends(require_mfa_proof),
) -> PublishResponse:
    """Promote a draft to published; the runtime switches on the next request."""
    assert_tenant_access(principal, tenant_id)
    db = get_database_manager()
    repo = PolicyRepository(db)
    row = await repo.publish_set(policy_id, str(tenant_id))
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Policy set not found: {policy_id}",
        )
    await AuditRepository(db).add(
        action="policy_set.published",
        resource_type="policy_set",
        resource_id=row["id"],
        tenant_id=str(tenant_id),
        actor_type="api_key",
        actor_id=principal.key_id,
        details={"version": row["version"], "name": row["name"]},
    )
    logger.info(
        "policy_set_published",
        tenant_id=str(tenant_id),
        policy_set_id=row["id"],
        version=row["version"],
        actor=principal.key_id,
    )
    return PublishResponse(
        id=row["id"],
        tenant_id=row["tenant_id"],
        version=row["version"],
        status=row["status"],
    )
