"""
P9-4 — Prompt management portal API (versioned prompts + A/B).

Versioned tenant prompts: create v1, add versions, activate one or more
variants with deterministic A/B percentages. The conversation path
resolves the active variant per session seed (stable within a session)
and uses it as the system prompt when it hits.
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
    PromptRepository,
    get_database_manager,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/prompts", tags=["Prompts"])


class PromptCreateRequest(BaseModel):
    tenant_id: str
    name: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1)
    description: str | None = Field(default=None, max_length=512)


class PromptActivateRequest(BaseModel):
    version: int = Field(ge=1)
    enabled: bool = True
    target_percentage: int = Field(default=100, ge=0, le=100)


def _row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row["name"],
        "version": row["version"],
        "content": row["content"],
        "description": row.get("description"),
        "enabled": bool(row.get("enabled")),
        "targetPercentage": int(row.get("target_percentage") or 0),
        "createdBy": row.get("created_by"),
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
    }


@router.get("")
async def list_prompts(
    tenant_id: str,
    principal: ApiKeyPrincipal = Depends(require_permission("prompts:read")),
) -> dict:
    """List all prompt names + versions for a tenant."""
    assert_tenant_access(principal, tenant_id)
    rows = await PromptRepository(get_database_manager()).list_by_tenant(tenant_id)
    return {"prompts": [_row(r) for r in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_prompt(
    request: PromptCreateRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("prompts:write")),
) -> dict:
    """Create the first version of a prompt (starts disabled)."""
    assert_tenant_access(principal, request.tenant_id)
    row = await PromptRepository(get_database_manager()).create(
        request.tenant_id,
        request.name,
        request.content,
        description=request.description,
        created_by=principal.name,
    )
    logger.info("prompt_created", tenant_id=request.tenant_id, name=request.name, version=row["version"])
    return _row(row)


@router.get("/{name}/versions")
async def list_versions(
    tenant_id: str,
    name: str,
    principal: ApiKeyPrincipal = Depends(require_permission("prompts:read")),
) -> dict:
    assert_tenant_access(principal, tenant_id)
    rows = await PromptRepository(get_database_manager()).list_versions(tenant_id, name)
    return {"name": name, "versions": [_row(r) for r in rows]}


@router.post("/{name}/versions", status_code=status.HTTP_201_CREATED)
async def add_version(
    name: str,
    request: PromptCreateRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("prompts:write")),
) -> dict:
    """Add a new version of an existing prompt (starts disabled)."""
    tenant_id = request.tenant_id
    assert_tenant_access(principal, tenant_id)
    if request.name != name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="path name and body name must match",
        )
    row = await PromptRepository(get_database_manager()).create(
        tenant_id,
        name,
        request.content,
        description=request.description,
        created_by=principal.name,
    )
    logger.info("prompt_version_added", tenant_id=tenant_id, name=name, version=row["version"])
    return _row(row)


@router.post("/{name}/activate")
async def activate_version(
    tenant_id: str,
    name: str,
    request: PromptActivateRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("prompts:write")),
) -> dict:
    """Activate a version (optionally as an A/B variant share)."""
    assert_tenant_access(principal, tenant_id)
    row = await PromptRepository(get_database_manager()).set_active(
        tenant_id,
        name,
        request.version,
        enabled=request.enabled,
        target_percentage=request.target_percentage,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prompt version not found")
    logger.info(
        "prompt_version_activated",
        tenant_id=tenant_id,
        name=name,
        version=request.version,
        enabled=request.enabled,
        target_percentage=request.target_percentage,
    )
    return _row(row)


@router.delete("/{name}/versions/{version}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_version(
    tenant_id: str,
    name: str,
    version: int,
    principal: ApiKeyPrincipal = Depends(require_permission("prompts:write")),
) -> None:
    assert_tenant_access(principal, tenant_id)
    deleted = await PromptRepository(get_database_manager()).delete_version(tenant_id, name, version)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prompt version not found")
    logger.info("prompt_version_deleted", tenant_id=tenant_id, name=name, version=version)
