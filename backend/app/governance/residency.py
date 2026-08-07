"""
P5-12 — Deployment shapes & residency (Arch §6.5, §11).

Residency pinning from L1: a tenant carries a ``region``; archives and
exports are confined to the tenant's region-namespaced path. Dedicated
deployment is a *configuration* choice (own DB/vector/gateway behind the
same control plane), never a code fork.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

DEFAULT_REGION = "eu-west-1"
SUPPORTED_REGIONS = ("eu-west-1", "us-east-1", "us-west-2", "ap-southeast-1")


def resolve_region(region: Optional[str]) -> str:
    """Validate + normalize the tenant region; deny unsupported."""
    if region is None:
        return DEFAULT_REGION
    r = str(region).strip().lower()
    if r not in SUPPORTED_REGIONS:
        raise ValueError(
            f"unsupported region '{r}'; supported: {', '.join(SUPPORTED_REGIONS)}"
        )
    return r


def archive_prefix(tenant_id: str, region: Optional[str] = None) -> str:
    """Archive/export storage prefix pinned to the tenant's region."""
    return f"tenant/{tenant_id}/archive/{resolve_region(region)}/"


def dedicated_deployment_config(tenant: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Return the dedicated shape config when the tenant opted in.

    Returns None for the default shared shape (own DB/vector/gateway only
    for dedicated tenants; otherwise shared pools).
    """
    features = tenant.get("features") or {}
    if not features.get("dedicated_deployment"):
        return None
    return {
        "tenant_id": str(tenant["id"]),
        "shape": "dedicated",
        "database": {"dedicated": True, "pool": f"tenant_{tenant['id']}"},
        "vector": {"dedicated": True, "namespace": f"tenant_{tenant['id']}"},
        "gateway": {"dedicated": True, "quota": "isolated"},
        "control_plane": "shared",
    }


def deployment_shape(tenant: Mapping[str, Any]) -> str:
    """'dedicated' when the tenant's features opt in, else 'shared'."""
    features = tenant.get("features") or {}
    return "dedicated" if features.get("dedicated_deployment") else "shared"