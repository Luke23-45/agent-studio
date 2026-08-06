"""
Surfaces API (Arch 6.1, P1-9).

Tenant-scoped CRUD over per-surface configuration (persona, knowledge /
tool allowlists, model pin, budgets, brand voice). Resolution lives in
``resolve_surface``: unconfigured or inactive surfaces resolve to None —
the request path denies (P0-4 default-deny) instead of falling back.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.app.api.dependencies.auth import (
    assert_tenant_access,
    require_permission,
)
from backend.app.infrastructure.db import SurfaceRepository, get_database_manager

router = APIRouter()


class SurfaceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    surface_type: str = "widget"
    persona: str | None = None
    knowledge_allowlist: list[str] = Field(default_factory=list)
    tool_allowlist: list[str] = Field(default_factory=list)
    model_pin: str | None = None
    budgets: dict[str, Any] = Field(default_factory=dict)
    brand_voice_override: dict[str, Any] | None = None
    active: bool = True


class SurfaceUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    surface_type: str | None = None
    persona: str | None = None
    knowledge_allowlist: list[str] | None = None
    tool_allowlist: list[str] | None = None
    model_pin: str | None = None
    budgets: dict[str, Any] | None = None
    brand_voice_override: dict[str, Any] | None = None
    active: bool | None = None


async def resolve_surface(
    tenant_id: str, surface_id: str | None
) -> dict[str, Any] | None:
    """Resolve a surface for the request path; None = unconfigured (deny)."""
    repo = SurfaceRepository(get_database_manager())
    if surface_id:
        surface = await repo.get(tenant_id, surface_id)
    else:
        surface = await repo.get_default(tenant_id)
    if surface is None or not surface["active"]:
        return None
    return surface


@router.post(
    "/tenants/{tenant_id}/surfaces",
    summary="Create a surface (first active surface becomes the default)",
)
async def create_surface(
    tenant_id: str,
    request: SurfaceRequest,
    principal=Depends(require_permission("tenants:write")),
) -> dict[str, Any]:
    assert_tenant_access(principal, tenant_id)
    repo = SurfaceRepository(get_database_manager())
    if request.surface_type not in {"widget", "hosted_page", "public_api", "admin", "harness"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown surface_type: {request.surface_type}",
        )
    return await repo.create(
        tenant_id,
        request.name,
        surface_type=request.surface_type,
        persona=request.persona,
        knowledge_allowlist=request.knowledge_allowlist,
        tool_allowlist=request.tool_allowlist,
        model_pin=request.model_pin,
        budgets=request.budgets,
        brand_voice_override=request.brand_voice_override,
    )


@router.get("/tenants/{tenant_id}/surfaces", summary="List surfaces")
async def list_surfaces(
    tenant_id: str,
    principal=Depends(require_permission("tenants:read")),
) -> list[dict[str, Any]]:
    assert_tenant_access(principal, tenant_id)
    return await SurfaceRepository(get_database_manager()).list_by_tenant(tenant_id)


@router.get("/tenants/{tenant_id}/surfaces/default", summary="Resolve the default surface")
async def get_default_surface(
    tenant_id: str,
    principal=Depends(require_permission("tenants:read")),
) -> dict[str, Any]:
    assert_tenant_access(principal, tenant_id)
    surface = await resolve_surface(tenant_id, None)
    if surface is None:
        raise HTTPException(status_code=404, detail="no active surface configured")
    return surface


@router.get("/tenants/{tenant_id}/surfaces/{surface_id}", summary="Get a surface")
async def get_surface(
    tenant_id: str,
    surface_id: str,
    principal=Depends(require_permission("tenants:read")),
) -> dict[str, Any]:
    assert_tenant_access(principal, tenant_id)
    surface = await SurfaceRepository(get_database_manager()).get(tenant_id, surface_id)
    if surface is None:
        raise HTTPException(status_code=404, detail="surface not found")
    return surface


@router.put("/tenants/{tenant_id}/surfaces/{surface_id}", summary="Update a surface")
async def update_surface(
    tenant_id: str,
    surface_id: str,
    request: SurfaceUpdateRequest,
    principal=Depends(require_permission("tenants:write")),
) -> dict[str, Any]:
    assert_tenant_access(principal, tenant_id)
    fields = {k: v for k, v in request.model_dump(exclude_none=True).items()}
    updated = await SurfaceRepository(get_database_manager()).update(
        tenant_id, surface_id, **fields
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="surface not found")
    return updated


@router.delete(
    "/tenants/{tenant_id}/surfaces/{surface_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a surface",
)
async def delete_surface(
    tenant_id: str,
    surface_id: str,
    principal=Depends(require_permission("tenants:write")),
) -> None:
    assert_tenant_access(principal, tenant_id)
    deleted = await SurfaceRepository(get_database_manager()).delete(tenant_id, surface_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="surface not found")
