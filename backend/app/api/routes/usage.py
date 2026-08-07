"""
P7-4 — Usage & billing API over the spend ledger (Arch 10, P3-5).

Read-only aggregations of ``spend_events`` (written by the CostLedger,
the single writer) plus the durable USD quota windows:

- ``GET /usage/summary``       — totals, broken down per tenant
- ``GET /usage/tenants/{id}``  — tenant totals + per model/surface/day
- ``GET /usage/quota``         — quota windows (limit vs spent)
- ``GET /usage/events``        — newest spend events (drill-down feed)

Tenant-bound principals (tenant_admin) are scoped to their own tenant by
``assert_tenant_access``; super_admin sees the platform view. Financial
data is gated behind the ``billing:read`` permission (auditors do not
have it).
"""

from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.app.api.dependencies.auth import (
    ApiKeyPrincipal,
    assert_tenant_access,
    require_permission,
)
from backend.app.infrastructure.db import (
    QuotaStateRepository,
    SpendEventRepository,
    get_database_manager,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/usage", tags=["Usage & Billing"])


def _totals_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "calls": int(row.get("calls") or 0),
        "usd": round(float(row.get("usd") or 0.0), 6),
        "inputTokens": int(row.get("input_tokens") or 0),
        "outputTokens": int(row.get("output_tokens") or 0),
        "cachedTokens": int(row.get("cached_tokens") or 0),
    }


@router.get("/summary")
async def usage_summary(
    since: datetime | None = Query(default=None, description="ISO-8601 start"),
    until: datetime | None = Query(default=None, description="ISO-8601 end"),
    principal: ApiKeyPrincipal = Depends(require_permission("billing:read")),
) -> dict[str, Any]:
    """Platform usage totals, broken down per tenant (or own tenant only
    for tenant-bound principals)."""
    db = get_database_manager()
    repo = SpendEventRepository(db)
    tenant_id = str(principal.tenant_id) if principal.tenant_id else None

    totals = await repo.aggregate(tenant_id=tenant_id, since=since, until=until)
    per_tenant = (
        await repo.aggregate(
            since=since, until=until, group_by="tenant", limit=100
        )
        if tenant_id is None
        else []
    )
    return {
        "totals": _totals_dict(totals[0]),
        "perTenant": [
            {"tenantId": row["key"], **_totals_dict(row)} for row in per_tenant
        ],
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
    }


@router.get("/tenants/{tenant_id}")
async def usage_tenant_detail(
    tenant_id: UUID,
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    principal: ApiKeyPrincipal = Depends(require_permission("billing:read")),
) -> dict[str, Any]:
    """Per-tenant usage: totals plus per-model, per-surface and per-day
    breakdowns (last 90 days by default)."""
    assert_tenant_access(principal, tenant_id)
    db = get_database_manager()
    repo = SpendEventRepository(db)
    tenant = str(tenant_id)

    totals = await repo.aggregate(tenant_id=tenant, since=since, until=until)
    per_model = await repo.aggregate(
        tenant_id=tenant, since=since, until=until, group_by="model", limit=50
    )
    per_surface = await repo.aggregate(
        tenant_id=tenant, since=since, until=until, group_by="surface", limit=50
    )
    per_day = await repo.aggregate(
        tenant_id=tenant, since=since, until=until, group_by="day", limit=90
    )
    return {
        "tenantId": tenant,
        "totals": _totals_dict(totals[0]),
        "perModel": [
            {"key": row["key"] or "unknown", **_totals_dict(row)} for row in per_model
        ],
        "perSurface": [
            {"key": row["key"] or "unknown", **_totals_dict(row)} for row in per_surface
        ],
        "perDay": [{"key": row["key"], **_totals_dict(row)} for row in per_day],
    }


@router.get("/quota")
async def usage_quota(
    tenant_id: UUID | None = Query(default=None),
    scope_type: str | None = Query(default=None),
    principal: ApiKeyPrincipal = Depends(require_permission("billing:read")),
) -> dict[str, Any]:
    """Durable USD quota windows (limit vs spent per window)."""
    if tenant_id is not None:
        assert_tenant_access(principal, tenant_id)
    elif principal.tenant_id is not None:
        tenant_id = principal.tenant_id
    windows = await QuotaStateRepository(get_database_manager()).list_windows(
        tenant_id=str(tenant_id) if tenant_id else None,
        scope_type=scope_type,
        limit=100,
    )
    return {
        "windows": [
            {
                "scopeType": w["scope_type"],
                "tenantId": w["tenant_id"],
                "surfaceId": w["surface_id"],
                "endUserId": w["end_user_id"],
                "windowStartedAt": w["window_started_at"].isoformat()
                if w["window_started_at"]
                else None,
                "reservedUsd": round(float(w["reserved_usd"]), 6),
                "spentUsd": round(float(w["spent_usd"]), 6),
                "limitUsd": round(float(w["limit_usd"]), 6)
                if w["limit_usd"] is not None
                else None,
            }
            for w in windows
        ]
    }


@router.get("/events")
async def usage_events(
    tenant_id: UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    principal: ApiKeyPrincipal = Depends(require_permission("billing:read")),
) -> dict[str, Any]:
    """Newest spend events (drill-down feed)."""
    if tenant_id is not None:
        assert_tenant_access(principal, tenant_id)
    elif principal.tenant_id is not None:
        tenant_id = principal.tenant_id
    events = await SpendEventRepository(get_database_manager()).list_filtered(
        tenant_id=str(tenant_id) if tenant_id else None,
        limit=limit,
    )
    return {
        "events": [
            {
                "id": e["id"],
                "tenantId": e["tenant_id"],
                "surfaceId": e["surface_id"],
                "provider": e["provider"],
                "model": e["model"],
                "inputTokens": int(e["input_tokens"] or 0),
                "outputTokens": int(e["output_tokens"] or 0),
                "cachedTokens": int(e["cached_tokens"] or 0),
                "usd": round(float(e["usd"] or 0.0), 6),
                "createdAt": e["created_at"].isoformat() if e["created_at"] else None,
            }
            for e in events
        ]
    }
