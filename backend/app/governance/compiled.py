"""
P5-1 — Compiled tenant config (Arch §12, §6).

A ``CompiledSurfaceConfig`` is the immutable, ready-to-run artifact the
runtime reads for one surface at one config version: enabled ingress/egress
rails, model-catalog policy, tool allowlist, budgets (loop + USD hierarchy),
knowledge allowlist, brand voice. Produced by ``compile_surface_config``
and cached per ``(tenant_id, surface_id, version)``; publish invalidates the
cache. Deny-by-default (P0-4): an unconfigured surface compiles to a
fully-blocking config.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple
from uuid import UUID

from backend.app.domain.tenant import TenantConfig

_CONTRACTS_ROOT = Path(__file__).resolve().parents[3] / "contracts" / "schemas"
TENANT_CONFIG_SCHEMA = _CONTRACTS_ROOT / "tenant-config.schema.json"

INGRESS_RAIL_ORDER = (
    "regex_fastpath",
    "classifier",
    "nemo_rails",
    "spotlighting",
    "pii_detection",
)
EGRESS_RAIL_ORDER = ("output_validation", "pii_detection", "jailbreak_detection")


class ConfigValidationError(ValueError):
    """The config payload failed JSON Schema validation."""


@dataclass(frozen=True)
class RailBinding:
    """A single rail bound for the compile: name + enabled flag."""

    name: str
    enabled: bool


@dataclass(frozen=True)
class CompiledSurfaceConfig:
    """Ready-to-run per-surface config artifact."""

    tenant_id: UUID
    surface_id: str | None
    version: int
    source_hash: str

    ingress_rails: Tuple[RailBinding, ...] = ()
    egress_rails: Tuple[RailBinding, ...] = ()
    tool_allowlist: Tuple[str, ...] = ()
    budgets: Mapping[str, float] = field(default_factory=dict)
    knowledge_allowlist: Tuple[str, ...] = ()
    brand_voice: Optional[Mapping[str, Any]] = None
    deny_by_default: bool = False

    def allows_tool(self, name: str) -> bool:
        return bool(self.tool_allowlist) and name in self.tool_allowlist


@lru_cache(maxsize=1)
def _schema_data() -> dict[str, Any]:
    with TENANT_CONFIG_SCHEMA.open(encoding="utf-8") as f:
        return json.load(f)


def validate_payload(payload: Mapping[str, Any]) -> None:
    """Validate the tenant config payload against the JSON Schema.

    Raises ``ConfigValidationError`` on the first structural violation so
    invalid config cannot publish (P5-1 acceptance).
    """
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(_schema_data())
    errors = sorted(
        validator.iter_errors(payload), key=lambda e: list(e.absolute_path)
    )
    if errors:
        first = errors[0]
        probe = "/".join(map(str, first.absolute_path)) or "<root>"
        raise ConfigValidationError(f"{probe}: {first.message}")


def _as_dict(config_obj: Any) -> dict[str, Any]:
    if isinstance(config_obj, Mapping):
        return dict(config_obj)
    if hasattr(config_obj, "__dict__"):
        return {
            k: (str(v) if isinstance(v, UUID) else v)
            for k, v in vars(config_obj).items()
            if not k.startswith("_") and not callable(v)
        }
    raise TypeError(f"cannot compile config of type {type(config_obj)!r}")


def _rails(
    cfg: Mapping[str, Any], order: Tuple[str, ...]
) -> Tuple[RailBinding, ...]:
    return tuple(
        RailBinding(name=rail, enabled=_rail_enabled(cfg, rail)) for rail in order
    )


def _rail_enabled(cfg: Mapping[str, Any], rail: str) -> bool:
    guardrails = cfg.get("guardrail_config") or {}
    return bool(guardrails.get(rail, True))


def _hash_source(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compile_surface_config(
    tenant_config: TenantConfig | Mapping[str, Any],
    *,
    surface: Optional[Mapping[str, Any]] = None,
    version: int = 1,
) -> CompiledSurfaceConfig:
    """Compile the effective tenant config into a per-surface artifact.

    Budgets are enriched with the USD hierarchy keys used by the gateway:
    ``platform_usd`` / ``tenant_usd`` from settings defaults,
    ``surface_usd`` / ``end_user_usd`` from the surface row when present
    (P5-7). A surface that is missing, inactive, or carries no allowlists
    compiles deny-by-default.
    """
    from backend.app.modules.tenant_config import tenant_config_from_data

    payload = _as_dict(tenant_config)
    validate_payload(payload)

    tenant_cfg = tenant_config_from_data(payload)

    ingress = _rails(tenant_cfg.guardrail_config or {}, INGRESS_RAIL_ORDER)
    egress = _rails(tenant_cfg.guardrail_config or {}, EGRESS_RAIL_ORDER)

    tool_allowlist: list[str] = []
    knowledge_allowlist: list[str] = []
    brand_voice: Optional[Mapping[str, Any]] = None
    deny_by_default = False

    if surface is not None:
        tool_allowlist = sorted(surface.get("tool_allowlist") or [])
        knowledge_allowlist = sorted(surface.get("knowledge_allowlist") or [])
        brand_voice = surface.get("brand_voice_override")
        if not surface.get("active", True):
            deny_by_default = True
    else:
        deny_by_default = True

    # Budgets: loop budgets from tenant config; USD levels composed for the
    # gateway quota (P5-7). 0.0 = unlimited at that level (gateway contract).
    tenant_budgets = dict(tenant_cfg.budgets or {})
    budgets: dict[str, float] = {
        k: float(v)
        for k, v in tenant_budgets.items()
        if k not in ("quota_platform_usd", "quota_tenant_usd")
    }
    from backend.app.settings.env import settings

    budgets["quota_platform_usd"] = float(getattr(settings, "QUOTA_PLATFORM_USD", 0.0))
    budgets["quota_tenant_usd"] = float(
        tenant_budgets.get("monthly_usd", getattr(settings, "QUOTA_TENANT_USD", 0.0))
    )
    budgets["quota_surface_usd"] = float(
        (surface or {}).get("budgets", {}).get("monthly_usd", 0.0)
    )
    budgets["quota_end_user_usd"] = float(
        (surface or {}).get("budgets", {}).get("end_user_monthly_usd", 0.0)
    )

    return CompiledSurfaceConfig(
        tenant_id=(
            tenant_config.id
            if isinstance(tenant_config, TenantConfig)
            else UUID(str(payload["id"]))
        ),
        surface_id=surface["id"] if surface else None,
        version=version,
        source_hash=_hash_source(payload),
        ingress_rails=ingress,
        egress_rails=egress,
        tool_allowlist=tuple(tool_allowlist),
        knowledge_allowlist=tuple(knowledge_allowlist),
        budgets=budgets,
        brand_voice=brand_voice,
        deny_by_default=deny_by_default,
    )