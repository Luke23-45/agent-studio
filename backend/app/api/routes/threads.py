"""
Thread API (Arch 7.1, P1-4/P1-5).

Append-only regeneration and editing: the endpoints never mutate an
existing message — they append new messages chained to the originals.
End-user/session-token authentication lands in P1-8; for now the routes
are tenant-scoped via the admin API key like the other tenant routes.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.app.api.dependencies.auth import (
    ApiKeyPrincipal,
    assert_tenant_access,
    require_permission,
)
from backend.app.infrastructure.db import get_database_manager
from backend.app.infrastructure.db.threads import (
    MessageNotFoundError,
    ThreadNotFoundError,
)
from backend.app.session.service import InvalidTargetError, ThreadMutationService

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
