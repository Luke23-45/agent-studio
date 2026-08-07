"""Tenant onboarding automation (P5-10, Arch §14.1).

Runs the onboarding checklist for a tenant: default deny-by-default
surface, isolation namespaces/prefixes (computed provisioning), budget
defaults, and a tenant-bound operator key. Each step is recorded on the
immutable audit trail; re-running is idempotent and reports the state of
every step.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from backend.app.governance.isolation import resolve_tenant_context
from backend.app.governance.residency import archive_prefix
from backend.app.infrastructure.db import (
    ApiKeyRepository,
    AuditRepository,
    DatabaseManager,
    SurfaceRepository,
    TenantRepository,
    get_database_manager,
)

logger = structlog.get_logger(__name__)

DEFAULT_SURFACE_NAME = "default"


def _mint_operator_key(tenant_id: str) -> tuple[str, str, str, dict[str, Any]]:
    """Mint (raw_key, prefix, key_hash, row_data) for a tenant-bound key."""
    from backend.app.api.dependencies.auth import generate_api_key

    raw, prefix, hash_ = generate_api_key()
    return (
        raw,
        prefix,
        hash_,
        {
            "name": "onboarding-operator",
            "key_hash": hash_,
            "prefix": prefix,
            "role": "operator",
            "tenant_id": tenant_id,
        },
    )


class TenantOnboardingService:
    """Idempotent provisioning checklist for one tenant."""

    def __init__(self, db: DatabaseManager | None = None):
        self.db = db or get_database_manager()

    async def run(self, tenant_id: str) -> dict[str, Any]:
        tenant = await TenantRepository(self.db).get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"tenant not found: {tenant_id}")

        audit = AuditRepository(self.db)
        surface_repo = SurfaceRepository(self.db)
        key_repo = ApiKeyRepository(self.db)

        ctx = resolve_tenant_context(tenant_id=tenant_id)
        vector_namespace = ctx.vector_namespace()
        object_prefix = ctx.storage_prefix()
        archive_prefix_for_region = archive_prefix(tenant_id, tenant.get("region"))

        steps: dict[str, Any] = {}

        # 1) Default surface (deny-by-default: empty tool/knowledge allowlists).
        existing_surface = await surface_repo.get_default(tenant_id)
        if existing_surface is not None:
            steps["default_surface"] = {
                "status": "already_configured",
                "surface_id": existing_surface["id"],
            }
        else:
            surface = await surface_repo.create(
                tenant_id,
                DEFAULT_SURFACE_NAME,
                surface_type="widget",
                persona=None,
                knowledge_allowlist=[],
                tool_allowlist=[],
                model_pin=None,
                budgets={},
                brand_voice_override=None,
            )
            steps["default_surface"] = {"status": "created", "surface_id": surface["id"]}
            await audit.add(
                action="tenant.onboarding.surface_created",
                resource_type="surface",
                resource_id=surface["id"],
                tenant_id=tenant_id,
                actor_type="system",
                details={"surface_id": surface["id"], "deny_by_default": True},
            )

        # 2) Isolation namespaces + object prefixes (computed provisioning).
        steps["vector_namespace"] = {"status": "provisioned", "namespace": vector_namespace}
        steps["object_prefix"] = {"status": "provisioned", "prefix": object_prefix}
        steps["archive_prefix"] = {
            "status": "provisioned",
            "prefix": archive_prefix_for_region,
        }
        await audit.add(
            action="tenant.onboarding.isolation_registered",
            resource_type="tenant",
            resource_id=tenant_id,
            tenant_id=tenant_id,
            actor_type="system",
            details={
                "vector_namespace": vector_namespace,
                "object_prefix": object_prefix,
                "archive_prefix": archive_prefix_for_region,
                "surface_id": steps["default_surface"].get("surface_id"),
            },
        )

        # 3) Default budgets: empty = gateway applies deny-by-default.
        steps["budgets"] = {
            "status": "configured",
            "effective": "deny_by_default",
        }

        # 4) Tenant-bound operator key (created only if none is active).
        operator_key = None
        for row in await key_repo.list_all():
            if (
                row["tenant_id"] == tenant_id
                and row["role"] == "operator"
                and not row["revoked"]
            ):
                operator_key = row
                break
        if operator_key is not None:
            steps["operator_key"] = {
                "status": "already_provisioned",
                "key_id": operator_key["id"],
            }
            operator_raw_key = None
        else:
            raw_key, prefix, _, row_data = _mint_operator_key(tenant_id)
            created = await key_repo.create(**row_data)
            steps["operator_key"] = {
                "status": "created",
                "key_id": created["id"],
                "prefix": prefix,
            }
            operator_raw_key = raw_key
            await audit.add(
                action="tenant.onboarding.operator_key_created",
                resource_type="api_key",
                resource_id=created["id"],
                tenant_id=tenant_id,
                actor_type="system",
                details={"role": "operator", "tenant_id": tenant_id},
            )

        receipt = {
            "schema_version": "1.0",
            "tenant_id": tenant_id,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "steps": steps,
        }
        if operator_raw_key is not None:
            receipt["operator_key"] = {"key": operator_raw_key, "shown_once": True}
        await audit.add(
            action="tenant.onboarding.completed",
            resource_type="tenant",
            resource_id=tenant_id,
            tenant_id=tenant_id,
            actor_type="system",
            details={name: step.get("status") for name, step in steps.items()},
        )
        logger.info("tenant_onboarded", tenant_id=tenant_id)
        return receipt

    async def checklist(self, tenant_id: str) -> dict[str, Any]:
        tenant = await TenantRepository(self.db).get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"tenant not found: {tenant_id}")

        surface = await SurfaceRepository(self.db).get_default(tenant_id)
        keys = await ApiKeyRepository(self.db).list_all()

        ctx = resolve_tenant_context(tenant_id=tenant_id)
        return {
            "tenant_id": tenant_id,
            "region": tenant.get("region"),
            "steps": {
                "default_surface": {
                    "status": "configured" if surface is not None else "missing",
                    "surface_id": surface["id"] if surface else None,
                },
                "vector_namespace": {"status": "provisioned", "namespace": ctx.vector_namespace()},
                "object_prefix": {"status": "provisioned", "prefix": ctx.storage_prefix()},
                "archive_prefix": {
                    "status": "provisioned",
                    "prefix": archive_prefix(tenant_id, tenant.get("region")),
                },
                "budgets": {"status": "deny_by_default"},
                "operator_key": {
                    "status": (
                        "provisioned"
                        if any(
                            k["tenant_id"] == tenant_id
                            and k["role"] == "operator"
                            and not k["revoked"]
                            for k in keys
                        )
                        else "missing"
                    )
                },
            },
        }
