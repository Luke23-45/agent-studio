"""
Traces explorer API (P7-4 admin console).

There is no separate span store in this codebase: the durable per-request
record is the append-only spend-event ledger (``spend_events``, P0-10),
which every completed turn writes. Trace entries are derived from those
rows — one entry per completed generation — so ``status`` is always
``ok`` (failed calls never produce a spend event) and ``duration_ms`` is
not recorded in the ledger and returned as 0. ``metadata`` carries the
ledger's surface/end-user/conversation scoping.
"""

from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.app.api.dependencies.auth import (
    ApiKeyPrincipal,
    assert_tenant_access,
    require_permission,
)
from backend.app.infrastructure.db import SpendEventRepository, get_database_manager

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Traces"])


def _event_to_trace(event: dict[str, Any]) -> dict[str, Any]:
    event_id = event["id"]
    metadata: dict[str, Any] = {
        "surface_id": event.get("surface_id"),
        "end_user_id": event.get("end_user_id"),
        "conversation_id": event.get("conversation_id"),
        "reasoning_tokens": event.get("reasoning_tokens"),
        "cached_tokens": event.get("cached_tokens"),
    }
    return {
        "id": event_id,
        "tenant_id": event["tenant_id"],
        "session_id": event.get("session_id") or "",
        "thread_id": event.get("conversation_id"),
        "trace_id": event.get("request_id") or event_id,
        "span_id": event_id,
        "parent_span_id": None,
        "operation": "chat.completion",
        "status": "ok",
        "duration_ms": 0,
        "input_tokens": event.get("input_tokens") or 0,
        "output_tokens": event.get("output_tokens") or 0,
        "model": event.get("model") or "",
        "provider": event.get("provider") or "",
        "cost": event.get("usd") or 0.0,
        "metadata": {k: v for k, v in metadata.items() if v is not None},
        "created_at": event.get("created_at"),
    }


@router.get("/traces")
async def list_traces(
    tenant_id: UUID | None = Query(default=None, description="Scope to one tenant"),
    limit: int = Query(default=100, ge=1, le=1000),
    principal: ApiKeyPrincipal = Depends(require_permission("conversations:read")),
) -> list[dict[str, Any]]:
    """Newest-first trace entries, derived from the spend-event ledger."""
    if tenant_id is not None:
        assert_tenant_access(principal, tenant_id)
    db = get_database_manager()
    rows = await SpendEventRepository(db).list_filtered(
        tenant_id=str(tenant_id) if tenant_id else None,
        limit=limit,
    )
    return [_event_to_trace(r) for r in rows]


@router.get("/traces/{trace_id}")
async def get_trace(
    trace_id: str,
    principal: ApiKeyPrincipal = Depends(require_permission("conversations:read")),
) -> dict[str, Any]:
    db = get_database_manager()
    row = await SpendEventRepository(db).get_by_id(trace_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trace not found: {trace_id}",
        )
    assert_tenant_access(principal, UUID(row["tenant_id"]))
    return _event_to_trace(row)
