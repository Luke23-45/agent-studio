"""
Evaluation runs API (P7-4 evals dashboard).

Eval runs ARE real replays: ``POST /evals`` re-runs one stored
conversation through the production orchestration pipeline
(``EvalReplayService``) and the outcome is persisted as an immutable
``eval.replay`` audit event (append-only, tamper-evident trail). The
read endpoints hydrate ``EvalRun``/``EvalCase`` from those events.

``EvalCase`` rows are derived from what the replay report records:
per-turn failures are captured in ``details.errors``; successful turns
are aggregated into the run totals, so a run with no failures exposes
no individual case rows.
"""

from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.app.api.dependencies.auth import (
    ApiKeyPrincipal,
    assert_tenant_access,
    require_permission,
)
from backend.app.application.eval_replay.service import EvalReplayService, ReplayError
from backend.app.infrastructure.db import (
    AuditRepository,
    ConversationRepository,
    TenantRepository,
    get_database_manager,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Evals"])

EVAL_RUN_ACTION = "eval.replay"

EVAL_TYPES = {"regression", "red_team", "adversarial", "quality", "compaction"}


class EvalRunCreateRequest(BaseModel):
    session_id: str = Field(..., min_length=1, description="Conversation session to replay")
    tenant_id: str | None = None
    name: str | None = Field(default=None, max_length=128)
    type: str = Field(default="quality", description="One of: regression, red_team, adversarial, quality, compaction")
    config: dict[str, Any] = Field(default_factory=dict)


def _report_to_run(
    event: dict[str, Any],
    details: dict[str, Any],
) -> dict[str, Any]:
    """Audit event (action=eval.replay) -> frontend EvalRun shape."""
    created = event.get("created_at")
    total = details.get("total_turns") or 0
    succeeded = details.get("succeeded") or 0
    failed = details.get("failed") or 0
    avg_confidence = details.get("avg_confidence")
    eval_type = details.get("type", "quality")
    if eval_type not in EVAL_TYPES:
        eval_type = "quality"
    return {
        "id": event["id"],
        "tenant_id": event["tenant_id"],
        "name": details.get("name") or f"Replay {details.get('session_id', '')}",
        "type": eval_type,
        "status": "completed",
        "total_cases": total,
        "passed_cases": succeeded,
        "failed_cases": failed,
        "score": round(avg_confidence * 100, 1) if avg_confidence is not None else 0.0,
        "config": {
            "session_id": details.get("session_id"),
            "conversation_id": details.get("conversation_id"),
            "replayed": details.get("replayed"),
            "skipped_blocked": details.get("skipped_blocked"),
            "handoffs": details.get("handoffs"),
            **details.get("config", {}),
        },
        "started_at": created,
        "completed_at": created,
        "created_at": created,
    }


def _run_from_event(event: dict[str, Any]) -> dict[str, Any]:
    details = event.get("details") or {}
    return _report_to_run(event, details)


def _cases_from_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    details = event.get("details") or {}
    cases: list[dict[str, Any]] = []
    for index, error in enumerate(details.get("errors") or []):
        cases.append(
            {
                "id": f"{event['id']}-case-{index + 1}",
                "eval_run_id": event["id"],
                "input": error.get("turn", ""),
                "expected": "",
                "actual": "",
                "passed": False,
                "score": None,
                "latency_ms": None,
                "error": error.get("error"),
            }
        )
    return cases


@router.post("/evals", status_code=status.HTTP_201_CREATED)
async def create_eval_run(
    request: EvalRunCreateRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("evals:run")),
) -> dict[str, Any]:
    """Replay one stored conversation through the production pipeline and
    record the outcome as an immutable eval.replay audit event."""
    db = get_database_manager()
    tenant_id = request.tenant_id or (
        str(principal.tenant_id) if principal.tenant_id else None
    )
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tenant_id is required when the API key is not tenant-bound",
        )
    assert_tenant_access(principal, tenant_id)

    tenant = await TenantRepository(db).get_by_id(tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant not found: {tenant_id}",
        )
    conversation = await ConversationRepository(db).get_by_session(
        tenant_id, request.session_id
    )
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation not found: session={request.session_id}",
        )

    try:
        report = await EvalReplayService(db).replay_conversation(
            tenant_id,
            request.session_id,
            details_extra={
                "name": request.name,
                "type": request.type,
                "config": request.config,
                "actor": principal.key_id,
            },
        )
    except ReplayError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )

    event = await AuditRepository(db).get_by_id(report["audit_event_id"])
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Replay completed but its audit event was not recorded",
        )
    return _run_from_event(event)


@router.get("/evals")
async def list_eval_runs(
    tenant_id: UUID | None = Query(default=None, description="Scope to one tenant"),
    type: str | None = Query(default=None, description="Filter by run type"),
    limit: int = Query(default=100, ge=1, le=1000),
    principal: ApiKeyPrincipal = Depends(require_permission("evals:read")),
) -> list[dict[str, Any]]:
    """Latest eval runs, newest first."""
    if tenant_id is not None:
        assert_tenant_access(principal, tenant_id)
    db = get_database_manager()
    events = await AuditRepository(db).list_events(
        tenant_id=str(tenant_id) if tenant_id else None,
        action=EVAL_RUN_ACTION,
        limit=limit,
    )
    runs = [_run_from_event(e) for e in events]
    if type:
        runs = [r for r in runs if r["type"] == type]
    return runs


@router.get("/evals/{eval_id}")
async def get_eval_run(
    eval_id: str,
    principal: ApiKeyPrincipal = Depends(require_permission("evals:read")),
) -> dict[str, Any]:
    db = get_database_manager()
    event = await AuditRepository(db).get_by_id(eval_id)
    if event is None or event.get("action") != EVAL_RUN_ACTION:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Eval run not found: {eval_id}",
        )
    if event.get("tenant_id"):
        assert_tenant_access(principal, UUID(event["tenant_id"]))
    return _run_from_event(event)


@router.get("/evals/{eval_id}/cases")
async def get_eval_cases(
    eval_id: str,
    principal: ApiKeyPrincipal = Depends(require_permission("evals:read")),
) -> list[dict[str, Any]]:
    """Per-turn failures recorded by the replay report."""
    db = get_database_manager()
    event = await AuditRepository(db).get_by_id(eval_id)
    if event is None or event.get("action") != EVAL_RUN_ACTION:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Eval run not found: {eval_id}",
        )
    if event.get("tenant_id"):
        assert_tenant_access(principal, UUID(event["tenant_id"]))
    return _cases_from_event(event)
