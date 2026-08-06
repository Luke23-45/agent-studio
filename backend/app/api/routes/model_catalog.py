"""
Per-tenant model catalog management (matrix 5.2).

Catalog entries allowlist (provider, model) pairs for a tenant with a
fallback order and per-1k-tokens cost ceiling. Enforcement happens in the
conversation routes; these endpoints manage the catalog itself.
"""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.app.api.dependencies.auth import (
    ApiKeyPrincipal,
    assert_tenant_access,
    require_permission,
)
from backend.app.infrastructure.db import get_database_manager
from backend.app.modules.model_catalog import ModelCatalogService

logger = structlog.get_logger(__name__)

router = APIRouter()


class ModelCatalogEntryRequest(BaseModel):
    tenant_id: str
    provider: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=128)
    cost_ceiling_per_1k: float = Field(default=0.0, ge=0.0)
    fallback_order: int = Field(default=0, ge=0)


class ModelCatalogEntryResponse(BaseModel):
    id: str
    tenant_id: str
    provider: str
    model: str
    enabled: bool
    cost_ceiling_per_1k: float
    fallback_order: int


def _to_response(row: dict) -> ModelCatalogEntryResponse:
    return ModelCatalogEntryResponse(
        id=row["id"],
        tenant_id=row["tenant_id"],
        provider=row["provider"],
        model=row["model"],
        enabled=row["enabled"],
        cost_ceiling_per_1k=row["cost_ceiling_per_1k"],
        fallback_order=row["fallback_order"],
    )


@router.get(
    "/tenants/{tenant_id}/models",
    response_model=list[ModelCatalogEntryResponse],
)
async def list_model_catalog(
    tenant_id: str,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:read")),
):
    assert_tenant_access(principal, tenant_id)
    rows = await ModelCatalogService().list_catalog(tenant_id)
    return [_to_response(r) for r in rows]


@router.post(
    "/tenants/{tenant_id}/models",
    response_model=ModelCatalogEntryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_model_catalog_entry(
    tenant_id: str,
    request: ModelCatalogEntryRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:write")),
):
    assert_tenant_access(principal, tenant_id)
    if request.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="tenant_id in body must match the path",
        )
    row = await ModelCatalogService().add_entry(
        tenant_id=tenant_id,
        provider=request.provider,
        model=request.model,
        cost_ceiling_per_1k=request.cost_ceiling_per_1k,
        fallback_order=request.fallback_order,
    )
    return _to_response(row)


@router.delete("/tenants/{tenant_id}/models/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_catalog_entry(
    tenant_id: str,
    entry_id: str,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:write")),
):
    assert_tenant_access(principal, tenant_id)
    deleted = await ModelCatalogService().remove_entry(entry_id, tenant_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catalog entry not found")
    return None
