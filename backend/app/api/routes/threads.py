"""
Thread API (Arch 7.1, P1-4/P1-5).

Append-only regeneration and editing: the endpoints never mutate an
existing message — they append new messages chained to the originals.
Regenerate/fork/edit are tenant-scoped via the admin API key;
``feedback`` accepts end-user session tokens (widget surface).
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.app.api.dependencies.auth import (
    ApiKeyPrincipal,
    assert_tenant_access,
    require_permission,
)
from backend.app.api.dependencies.session import get_session_principal
from backend.app.infrastructure.db import (
    AuditRepository,
    ThreadRepository,
    get_database_manager,
)
from backend.app.infrastructure.db.threads import (
    MessageNotFoundError,
    ThreadNotFoundError,
)
from backend.app.session.service import InvalidTargetError, ThreadMutationService
from backend.app.session.tokens import SessionPrincipal

router = APIRouter()


class RegenerateRequest(BaseModel):
    message_id: str = Field(..., description="Assistant message to regenerate")
    tenant_id: str | None = None


class ForkRequest(BaseModel):
    tenant_id: str | None = None


class EditRequest(BaseModel):
    content: str = Field(..., min_length=1, description="New user message text")
    redacted_content: str | None = None
    tenant_id: str | None = None


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
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tenant_id is required for tenant-scoped operations",
        )
    return request_tenant


def _mutation_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, ThreadNotFoundError):
        return HTTPException(status_code=404, detail=f"Thread not found: {exc}")
    if isinstance(exc, MessageNotFoundError):
        return HTTPException(status_code=404, detail=f"Message not found: {exc}")
    if isinstance(exc, InvalidTargetError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="internal error")


@router.post(
    "/threads/{thread_id}/regenerate",
    summary="Regenerate an assistant message (append-only)",
)
async def regenerate_message(
    thread_id: str,
    request: RegenerateRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("conversations:write")),
) -> dict[str, Any]:
    tenant_id = _resolve_tenant_id(request.tenant_id, principal)
    assert_tenant_access(principal, tenant_id)
    service = ThreadMutationService(get_database_manager())
    try:
        result = await service.regenerate(
            tenant_id,
            thread_id,
            request.message_id,
            actor_type=principal.role,
            actor_id=str(principal.key_id),
        )
    except (ThreadNotFoundError, MessageNotFoundError, InvalidTargetError) as exc:
        raise _mutation_errors(exc) from None
    return result


@router.post(
    "/threads/{thread_id}/fork",
    summary="Fork a thread at a message boundary (append-only)",
)
async def fork_thread(
    thread_id: str,
    at_seq: int = 0,
    request: ForkRequest = ForkRequest(),
    principal: ApiKeyPrincipal = Depends(require_permission("conversations:write")),
) -> dict[str, Any]:
    tenant_id = _resolve_tenant_id(request.tenant_id, principal)
    assert_tenant_access(principal, tenant_id)
    service = ThreadMutationService(get_database_manager())
    try:
        result = await service.fork(
            tenant_id,
            thread_id,
            at_seq,
            actor_type=principal.role,
            actor_id=str(principal.key_id),
        )
    except (ThreadNotFoundError, InvalidTargetError) as exc:
        raise _mutation_errors(exc) from None
    return result


@router.post(
    "/threads/{thread_id}/messages/{message_id}/edit",
    summary="Edit a user message (append-only: new user message + successor)",
)
async def edit_message(
    thread_id: str,
    message_id: str,
    request: EditRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("conversations:write")),
) -> dict[str, Any]:
    tenant_id = _resolve_tenant_id(request.tenant_id, principal)
    assert_tenant_access(principal, tenant_id)
    service = ThreadMutationService(get_database_manager())
    try:
        result = await service.edit(
            tenant_id,
            thread_id,
            message_id,
            request.content,
            request.redacted_content,
            actor_type=principal.role,
            actor_id=str(principal.key_id),
        )
    except (ThreadNotFoundError, MessageNotFoundError, InvalidTargetError) as exc:
        raise _mutation_errors(exc) from None
    return result


class FeedbackRequest(BaseModel):
    """End-user feedback on a thread (widget, P7-1)."""

    message_id: str | None = Field(default=None, max_length=64)
    rating: str = Field(..., pattern="^(up|down)$")
    comment: str | None = Field(default=None, max_length=2000)


@router.post(
    "/threads/{thread_id}/feedback",
    summary="Record end-user feedback on a thread (session-token auth)",
)
async def submit_feedback(
    thread_id: str,
    request: FeedbackRequest,
    principal: SessionPrincipal = Depends(get_session_principal),
) -> dict[str, Any]:
    """Record a thumbs up/down on a thread from the widget.

    Auth is the end-user session token; the thread must belong to the
    session's tenant. Feedback is stored on the immutable audit trail
    (append-only, P5-9) for future analytics.
    """
    tenant_id = str(principal.tenant_id)
    db = get_database_manager()
    thread = await ThreadRepository(db).get_thread(tenant_id, thread_id)
    if thread is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Thread not found: {thread_id}",
        )
    await AuditRepository(db).add(
        action="message.feedback",
        resource_type="thread",
        resource_id=thread_id,
        tenant_id=tenant_id,
        actor_type="session",
        actor_id=principal.end_user_id or thread_id,
        details={
            "message_id": request.message_id,
            "rating": request.rating,
            "comment": request.comment,
            "surface_id": principal.surface_id,
        },
    )
    return {"ok": True, "thread_id": thread_id, "rating": request.rating}


class ArchiveRequest(BaseModel):
    tenant_id: str | None = None
    region: str | None = None
    request_id: str | None = Field(default=None, max_length=64)


class ArchiveResponse(BaseModel):
    accepted: bool
    job_type: str
    thread_id: str
    idempotency_key: str | None
    note: str


def _archive_route_common(thread_id: str, request: ArchiveRequest, principal: ApiKeyPrincipal):
    tenant_id = _resolve_tenant_id(request.tenant_id, principal)
    assert_tenant_access(principal, tenant_id)
    return tenant_id


@router.post(
    "/threads/{thread_id}/archive",
    response_model=ArchiveResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Move a thread to the cold-tier archive (P1-7)",
)
async def archive_thread(
    thread_id: str,
    request: ArchiveRequest = ArchiveRequest(),
    principal: ApiKeyPrincipal = Depends(require_permission("conversations:write")),
) -> ArchiveResponse:
    """Enqueue a ``thread.archive`` worker job (202).

    The worker dumps the full durable payload (thread, messages, parts,
    events) to region-pinned object storage and flips the archived marker
    — hot queries and the retention sweep then ignore the thread. Restore
    pulls the payload back for checksum verification and clears the
    marker. Idempotent by thread id.
    """
    from backend.app.infrastructure.queue import Job, get_queue_manager

    tenant_id = _archive_route_common(thread_id, request, principal)
    db = get_database_manager()
    thread = await ThreadRepository(db).get_thread(tenant_id, thread_id)
    if thread is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Thread not found: {thread_id}",
        )

    manager = get_queue_manager()
    idem_key = request.request_id or f"thread-archive:{tenant_id}:{thread_id}"
    job = Job(
        type="thread.archive",
        payload={
            "tenant_id": tenant_id,
            "thread_id": thread_id,
            "region": request.region,
            "request_id": idem_key,
        },
    )
    accepted = await manager.enqueue(job, idempotency_key=idem_key)
    return ArchiveResponse(
        accepted=accepted,
        job_type="thread.archive",
        thread_id=thread_id,
        idempotency_key=idem_key,
        note="accepted; archive job runs on the worker",
    )


@router.post(
    "/threads/{thread_id}/restore",
    response_model=ArchiveResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Restore a thread from the cold-tier archive (P1-7)",
)
async def restore_thread(
    thread_id: str,
    request: ArchiveRequest = ArchiveRequest(),
    principal: ApiKeyPrincipal = Depends(require_permission("conversations:write")),
) -> ArchiveResponse:
    """Enqueue a ``thread.restore`` worker job (202).

    The worker downloads the cold-tier payload, verifies its checksum and
    thread id, then clears the archived marker so the thread is visible to
    hot queries again. A corrupt payload raises and the marker stays off.
    """
    from backend.app.infrastructure.queue import Job, get_queue_manager

    tenant_id = _archive_route_common(thread_id, request, principal)
    db = get_database_manager()
    thread = await ThreadRepository(db).get_thread(tenant_id, thread_id)
    if thread is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Thread not found: {thread_id}",
        )

    manager = get_queue_manager()
    idem_key = request.request_id or f"thread-restore:{tenant_id}:{thread_id}"
    job = Job(
        type="thread.restore",
        payload={
            "tenant_id": tenant_id,
            "thread_id": thread_id,
            "region": request.region,
            "request_id": idem_key,
        },
    )
    accepted = await manager.enqueue(job, idempotency_key=idem_key)
    return ArchiveResponse(
        accepted=accepted,
        job_type="thread.restore",
        thread_id=thread_id,
        idempotency_key=idem_key,
        note="accepted; restore job runs on the worker",
    )
