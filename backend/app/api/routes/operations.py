"""
Operation trigger routes (P1 worker wiring).

These routes do not run heavy work inline: they validate the request,
enqueue a worker job (retries + DLQ + idempotency), and return 202.
This is the API surface that feeds the queue: knowledge ingestion
(chunk + embed pipeline), eval replay, and retention cleanup.
"""

import structlog
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.app.api.dependencies.auth import (
    ApiKeyPrincipal,
    assert_tenant_access,
    require_permission,
)
from backend.app.infrastructure.db import (
    TenantRepository,
    get_database_manager,
)
from backend.app.infrastructure.queue import Job, get_queue_manager
from backend.app.worker.handlers import (
    JOB_CLEANUP_RUN,
    JOB_EVAL_REPLAY,
    JOB_INGESTION_PROCESS,
)

logger = structlog.get_logger(__name__)

router = APIRouter()


class DocumentIngestRequest(BaseModel):
    """Knowledge document to ingest via the worker pipeline.

    ``content`` is chunked by the ``ingestion.process`` job; the worker
    then enqueues ``ingestion.embed`` to index the chunks into the
    tenant's vector space. A tenant_id may be supplied by a super-admin
    for scoped ingestion; tenant-bound keys are locked to their tenant.
    """

    content: str = Field(min_length=1, max_length=10 * 1024 * 1024)
    source: str = Field(min_length=1, max_length=1024, description="filename or URL")
    tenant_id: str | None = None
    document_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentIngestResponse(BaseModel):
    accepted: bool
    job_type: str
    document_id: str | None
    idempotency_key: str | None
    note: str


class EvalReplayRequest(BaseModel):
    tenant_id: str
    session_id: str


class EvalReplayResponse(BaseModel):
    accepted: bool
    job_type: str
    idempotency_key: str | None


class RetentionRunRequest(BaseModel):
    tenant_id: str
    older_than_seconds: int = Field(ge=0, default=None)
    document_id: str | None = None
    source: str | None = None
    job_type: str | None = None
    limit: int = Field(default=100, ge=1, le=10_000)
    requeue_dead_letter: bool = False


class RetentionRunResponse(BaseModel):
    accepted: bool
    job_type: str
    idempotency_key: str | None


def _resolve_tenant_id(request_tenant: str | None, principal: ApiKeyPrincipal) -> str:
    if principal.tenant_id is not None:
        if request_tenant and request_tenant != str(principal.tenant_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key is scoped to a different tenant",
            )
        return str(principal.tenant_id)
    if not request_tenant:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="tenant_id is required for tenant-scoped ingestion",
        )
    return request_tenant


@router.post(
    "/tenants/{tenant_id}/documents",
    response_model=DocumentIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_document(
    tenant_id: str,
    request: DocumentIngestRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("knowledge:write")),
):
    """Queue a knowledge document for chunking + embedding (202, async)."""
    assert_tenant_access(principal, tenant_id)
    target_tenant = _resolve_tenant_id(tenant_id, principal)
    assert_tenant_access(principal, target_tenant)

    manager = get_queue_manager()
    idem_key = f"ingest:{target_tenant}:{request.source}"
    job = Job(
        type=JOB_INGESTION_PROCESS,
        payload={
            "content": request.content,
            "source": request.source,
            "tenant_id": target_tenant,
            "document_id": request.document_id,
            "metadata": request.metadata,
        },
    )
    accepted = await manager.enqueue(job, idempotency_key=idem_key)
    logger.info(
        "document_ingest_enqueued",
        tenant_id=target_tenant,
        source=request.source,
        accepted=accepted,
    )
    return DocumentIngestResponse(
        accepted=accepted,
        job_type=JOB_INGESTION_PROCESS,
        document_id=request.document_id or f"pending:{request.source}",
        idempotency_key=idem_key,
        note="accepted; chunk+embed pipeline runs on the worker",
    )


@router.post(
    "/eval/replay",
    response_model=EvalReplayResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enqueue_eval_replay(
    request: EvalReplayRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("evals:run")),
):
    """Queue replay of a stored conversation through the pipeline (202)."""
    assert_tenant_access(principal, request.tenant_id)

    db = get_database_manager()
    tenant = await TenantRepository(db).get_by_id(request.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    manager = get_queue_manager()
    idem_key = f"eval-replay:{request.tenant_id}:{request.session_id}"
    job = Job(
        type=JOB_EVAL_REPLAY,
        payload={"tenant_id": request.tenant_id, "session_id": request.session_id},
    )
    accepted = await manager.enqueue(job, idempotency_key=idem_key)
    return EvalReplayResponse(
        accepted=accepted,
        job_type=JOB_EVAL_REPLAY,
        idempotency_key=idem_key,
    )


@router.post(
    "/retention/run",
    response_model=RetentionRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_retention(
    request: RetentionRunRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:write")),
):
    """Queue a retention cleanup pass for a tenant (202)."""
    assert_tenant_access(principal, request.tenant_id)

    manager = get_queue_manager()
    idem_key = f"retention:{request.tenant_id}:{request.older_than_seconds}:{int(request.requeue_dead_letter)}"
    job = Job(
        type=JOB_CLEANUP_RUN,
        payload={
            "tenant_id": request.tenant_id,
            "document_id": request.document_id,
            "source": request.source,
            "older_than_seconds": request.older_than_seconds,
            "job_type": request.job_type,
            "limit": request.limit,
            "requeue_dead_letter": request.requeue_dead_letter,
        },
    )
    accepted = await manager.enqueue(job, idempotency_key=idem_key)
    return RetentionRunResponse(
        accepted=accepted,
        job_type=JOB_CLEANUP_RUN,
        idempotency_key=idem_key,
    )
