"""
Tool registry factory for the request path (P9-1).

Builds a per-request ``ToolRegistry`` from the tenant's ``tool_registry``
rows: enabled MCP servers (``auth_config.type == "mcp"``) are connected
as tool sources; every other row is skipped (local tools are registered
in code via ``ToolSpec``, there is no arbitrary-code executor stored in
the DB).

Fail-safe semantics: an unreachable MCP server logs and contributes
nothing -- its tools are simply absent, so the P5-3 gate denies any call
to them (deny by default). The gate's ``enabled_map`` still reflects the
DB rows, so enablement decisions stay authoritative.
"""

from __future__ import annotations

import structlog
from typing import Any

from backend.app.adapters.tools.mcp import MCPToolSource
from backend.app.application.tools import ToolRegistry, create_tool_registry
from backend.app.settings.env import settings

logger = structlog.get_logger(__name__)


def _timeout() -> float:
    return getattr(settings, "MCP_TOOL_TIMEOUT_SECONDS", 15.0)


def _decrypt_token(value: str | None) -> str | None:
    """Bearer token at rest is envelope-encrypted (P9-1); legacy plaintext
    rows degrade gracefully."""
    if not value or not value.startswith("encrypted:"):
        return value
    from backend.app.infrastructure.keys.crypto import decrypt_secret

    return decrypt_secret(value[len("encrypted:") :])


async def build_tool_registry(
    *,
    db: Any,
    tenant_id: str,
    enable_mcp: bool = True,
    client_transport: Any = None,
) -> ToolRegistry:
    """Build the request tool registry from tenant tool_registry rows.

    ``enable_mcp`` gates source connection (driven by the
    ``ENABLE_MCP_TOOLS`` feature flag at the route layer); ``None`` for
    ``client_transport`` uses the real network (tests inject a mock).
    """
    registry = create_tool_registry()
    if not enable_mcp:
        return registry

    from backend.app.infrastructure.db import ToolRegistryRepository

    rows = await ToolRegistryRepository(db).list_by_tenant(tenant_id)
    for row in rows:
        if not bool(row.get("enabled")):
            continue
        auth_config = row.get("auth_config") or {}
        if auth_config.get("type") != "mcp":
            continue
        source = MCPToolSource(
            server_name=row["name"],
            transport=auth_config.get("transport", "streamable-http"),
            server_url=auth_config.get("server_url"),
            command=auth_config.get("command"),
            args=auth_config.get("args") or [],
            env=auth_config.get("env") or {},
            auth_token=_decrypt_token(auth_config.get("auth_token")),
            timeout=_timeout(),
            client_transport=client_transport,
        )
        try:
            await source.connect()
            added = await registry.register_source(source)
            logger.info(
                "mcp_source_registered",
                server=row["name"],
                tools=added,
                tenant_id=tenant_id,
            )
        except Exception as e:  # noqa: BLE001 - one server never breaks a request
            logger.warning(
                "mcp_source_unavailable",
                server=row["name"],
                tenant_id=tenant_id,
                error=str(e),
            )
    return registry
