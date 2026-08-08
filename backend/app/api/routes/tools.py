"""
P9-1 — Tool registry admin API (MCP servers as tool sources, 6.3).

Registers MCP servers per tenant in the ``tool_registry`` table behind
the P5-3 gate: a server row is created DISABLED, the operator connects
to it to confirm discovery (``/connect-test``), then enables it. The
gate (tenant enablement + surface allowlist) is the only authority on
whether a tool call actually runs.

``auth_config`` for an MCP row::

    {
      "type": "mcp",
      "transport": "streamable-http" | "stdio",
      "server_url": "https://.../mcp",     # streamable-http
      "command": "python",                  # stdio
      "args": ["-m", "my.server"],
      "env": {"FOO": "bar"},
      "auth_token": "..."                   # optional bearer
    }
"""

import structlog
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.app.api.dependencies.auth import (
    ApiKeyPrincipal,
    assert_tenant_access,
    require_permission,
)
from backend.app.infrastructure.db import (
    ToolRegistryRepository,
    get_database_manager,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/tools", tags=["Tools"])


class MCPRegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128, description="MCP server name (registry key)")
    description: str | None = Field(default=None, max_length=512)
    transport: Literal["streamable-http", "stdio"] = "streamable-http"
    server_url: str | None = Field(default=None, max_length=2048)
    command: str | None = Field(default=None, max_length=512)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    auth_token: str | None = Field(default=None, max_length=2048)


def _redact(row: dict) -> dict:
    """Never leak stored auth tokens back to the admin API."""
    auth_config = row.get("auth_config") or {}
    if isinstance(auth_config, dict) and "auth_token" in auth_config:
        auth_config = dict(auth_config)
        auth_config["auth_token"] = "***" if auth_config.get("auth_token") else None
    return {**row, "auth_config": auth_config}


def _encrypt_token(token: str | None) -> str | None:
    """Envelope-encrypt the MCP bearer token before storage (never plaintext)."""
    if not token:
        return None
    from backend.app.infrastructure.keys.crypto import encrypt_secret

    return "encrypted:" + encrypt_secret(token)


def _decrypt_token(value: str | None) -> str | None:
    """Decrypt a stored MCP bearer token (legacy plaintext rows degrade)."""
    if not value:
        return None
    if value.startswith("encrypted:"):
        from backend.app.infrastructure.keys.crypto import decrypt_secret

        return decrypt_secret(value[len("encrypted:") :])
    return value  # legacy row written before encryption landed


async def _require_row(tenant_id: str, name: str) -> dict:
    row = await ToolRegistryRepository(get_database_manager()).get(tenant_id, name)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found")
    return row


@router.get("")
async def list_tools(
    tenant_id: str,
    principal: ApiKeyPrincipal = Depends(require_permission("tools:read")),
) -> dict:
    """List the tenant's registered tools (MCP servers) with redacted config."""
    assert_tenant_access(principal, tenant_id)
    rows = await ToolRegistryRepository(get_database_manager()).list_by_tenant(tenant_id)
    return {"tools": [_redact(r) for r in rows]}


@router.post("/mcp", status_code=status.HTTP_201_CREATED)
async def register_mcp_server(
    tenant_id: str,
    request: MCPRegisterRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("tools:write")),
) -> dict:
    """Register an MCP server as a disabled tool source (enable to activate)."""
    assert_tenant_access(principal, tenant_id)
    if request.transport == "streamable-http" and not request.server_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="server_url is required for streamable-http transport",
        )
    if request.transport == "stdio" and not request.command:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="command is required for stdio transport",
        )

    repo = ToolRegistryRepository(get_database_manager())
    existing = await repo.get(tenant_id, request.name)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tool already registered: {request.name}",
        )
    row = await repo.register(
        tenant_id,
        request.name,
        description=request.description,
        enabled=False,
        auth_config={
            "type": "mcp",
            "transport": request.transport,
            "server_url": request.server_url,
            "command": request.command,
            "args": request.args,
            "env": request.env,
            "auth_token": _encrypt_token(request.auth_token),
        },
    )
    logger.info("mcp_server_registered", tenant_id=tenant_id, name=request.name)
    return _redact(row)


class MCPUpdateRequest(BaseModel):
    description: str | None = Field(default=None, max_length=512)
    server_url: str | None = Field(default=None, max_length=2048)
    command: str | None = Field(default=None, max_length=512)
    args: list[str] | None = None
    env: dict[str, str] | None = None
    auth_token: str | None = Field(
        default=None,
        description="New bearer token (encrypted at rest); '' clears it; "
        "None leaves it unchanged",
    )


@router.patch("/{name}")
async def update_tool(
    tenant_id: str,
    name: str,
    request: MCPUpdateRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("tools:write")),
) -> dict:
    """Update an MCP server's config (fields merge; token re-encrypted)."""
    row = await _require_row(tenant_id, name)
    auth_config = dict(row.get("auth_config") or {})
    if auth_config.get("type") != "mcp":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Tool {name} is not an MCP server",
        )
    updates: dict = {}
    if request.description is not None:
        updates["description"] = request.description
    for field in ("server_url", "command", "args", "env"):
        value = getattr(request, field)
        if value is not None:
            updates[field] = value
    if request.auth_token is not None:
        updates["auth_token"] = _encrypt_token(request.auth_token)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No fields to update",
        )
    updated = await ToolRegistryRepository(get_database_manager()).update_config(
        tenant_id, name, description=updates.pop("description", None), auth_config=updates
    )
    logger.info("mcp_server_updated", tenant_id=tenant_id, name=name)
    return _redact(updated)


@router.post("/{name}/connect-test")
async def connect_test_mcp(
    tenant_id: str,
    name: str,
    principal: ApiKeyPrincipal = Depends(require_permission("tools:write")),
) -> dict:
    """Connect to the MCP server and list the tools it would contribute."""
    row = await _require_row(tenant_id, name)
    auth_config = row.get("auth_config") or {}
    if auth_config.get("type") != "mcp":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Tool {name} is not an MCP server",
        )
    from backend.app.adapters.tools.mcp import MCPToolSource

    source = MCPToolSource(
        server_name=name,
        transport=auth_config.get("transport", "streamable-http"),
        server_url=auth_config.get("server_url"),
        command=auth_config.get("command"),
        args=auth_config.get("args") or [],
        env=auth_config.get("env") or {},
        auth_token=_decrypt_token(auth_config.get("auth_token")),
    )
    try:
        await source.connect()
        specs = source.tool_specs()
        return {
            "ok": True,
            "tools": [
                {"name": s.name, "description": s.description, "parameters": s.parameters}
                for s in specs
            ],
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"MCP server unreachable: {e}",
        ) from e
    finally:
        await source.close()


@router.post("/{name}/enable")
async def enable_tool(
    tenant_id: str,
    name: str,
    principal: ApiKeyPrincipal = Depends(require_permission("tools:write")),
) -> dict:
    """Enable a registered tool (still subject to the surface allowlist)."""
    await _require_row(tenant_id, name)
    row = await ToolRegistryRepository(get_database_manager()).set_enabled(tenant_id, name, True)
    logger.info("tool_enabled", tenant_id=tenant_id, name=name)
    return _redact(row)


@router.post("/{name}/disable")
async def disable_tool(
    tenant_id: str,
    name: str,
    principal: ApiKeyPrincipal = Depends(require_permission("tools:write")),
) -> dict:
    """Disable a registered tool (gate denies immediately)."""
    await _require_row(tenant_id, name)
    row = await ToolRegistryRepository(get_database_manager()).set_enabled(tenant_id, name, False)
    logger.info("tool_disabled", tenant_id=tenant_id, name=name)
    return _redact(row)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tool(
    tenant_id: str,
    name: str,
    principal: ApiKeyPrincipal = Depends(require_permission("tools:write")),
) -> None:
    """Remove the tool registration entirely."""
    deleted = await ToolRegistryRepository(get_database_manager()).delete(tenant_id, name)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found")
    logger.info("tool_deleted", tenant_id=tenant_id, name=name)
