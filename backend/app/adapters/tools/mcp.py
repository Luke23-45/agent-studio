"""
MCP client integration (feature-matrix 6.3, P1).

Model Context Protocol client side (spec 2025-06-18) over two transports:

- ``streamable-http``: JSON-RPC 2.0 POSTs to the server URL with
  ``Mcp-Session-Id`` and ``Authorization: Bearer`` support (SSE and
  single-JSON responses are both parsed).
- ``stdio``: JSON-RPC 2.0 newline-delimited frames over a spawned child
  process (``command`` + ``args`` + ``env`` from the tenant's auth config).

No MCP SDK dependency: the wire protocol is small and fully testable.
MCP servers are a TOOL SOURCE behind the registry -- every tool call is
still authorized by the P5-3 gate (tenant registry enablement + surface
allowlist) exactly like a local tool.
"""

from __future__ import annotations

import asyncio
import json
import structlog
import uuid
from typing import Any, Awaitable, Callable

logger = structlog.get_logger(__name__)

MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_SESSION_HEADER = "mcp-session-id"


class MCPError(Exception):
    """Transport or JSON-RPC failure talking to an MCP server."""


class MCPClient:
    """Minimal MCP client over the streamable-HTTP transport."""

    def __init__(
        self,
        *,
        server_url: str,
        auth_token: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
        transport: Any = None,
    ):
        self.server_url = server_url
        self.auth_token = auth_token
        self.headers = dict(headers or {})
        self.timeout = timeout
        self._transport = transport
        self._client: Any = None
        self._session_id: str | None = None
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _get_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                transport=self._transport,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = dict(self.headers)
        if self._session_id:
            headers[MCP_SESSION_HEADER] = self._session_id
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        client = await self._get_client()
        request_id = self._next_id()
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        try:
            response = await client.post(
                self.server_url, json=payload, headers=self._headers()
            )
        except Exception as e:  # network-level failure
            raise MCPError(f"MCP transport error ({method}): {e}") from e

        if response.status_code >= 400:
            raise MCPError(
                f"MCP server returned HTTP {response.status_code}: {response.text[:500]}"
            )

        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id

        body = _extract_json(response)
        if "error" in body:
            err = body["error"]
            raise MCPError(f"MCP JSON-RPC error {err.get('code')}: {err.get('message')}")
        return body.get("result", {})

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        """Fire-and-forget notification (no response expected)."""
        try:
            client = await self._get_client()
            payload = {"jsonrpc": "2.0", "method": method, "params": params}
            await client.post(self.server_url, json=payload, headers=self._headers())
        except Exception as e:  # pragma: no cover - notifications never raise
            logger.warning("mcp_notification_failed", method=method, error=str(e))

    async def connect(self) -> None:
        result = await self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "neryva", "version": "1.0.0"},
            },
        )
        logger.info("mcp_connected", server_url=self.server_url, server_info=result.get("serverInfo"))
        await self._notify("notifications/initialized", {})

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request("tools/list", {})
        return list(result.get("tools", []))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._request("tools/call", {"name": name, "arguments": arguments or {}})

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class MCPStdioClient:
    """Minimal MCP client over the stdio transport (newline-delimited JSON-RPC)."""

    def __init__(
        self,
        *,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 15.0,
    ):
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.timeout = timeout
        self._proc: Any = None
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def connect(self) -> None:
        import sys

        full_env = dict(__import__("os").environ)
        full_env.update(self.env)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=full_env,
            )
        except Exception as e:
            raise MCPError(f"stdio spawn failed ({self.command}): {e}") from e

        result = await self._request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "neryva", "version": "1.0.0"},
        })
        logger.info("mcp_stdio_connected", command=self.command, server_info=result.get("serverInfo"))
        self._proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n')
        await self._proc.stdin.drain()

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise MCPError("stdio client not connected")
        request_id = self._next_id()
        frame = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        ) + "\n"
        self._proc.stdin.write(frame.encode("utf-8"))
        await self._proc.stdin.drain()
        try:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=self.timeout)
        except asyncio.TimeoutError as e:
            raise MCPError(f"MCP stdio timeout ({method})") from e
        if not line:
            raise MCPError("MCP stdio server closed the pipe")
        try:
            body = json.loads(line)
        except json.JSONDecodeError as e:
            raise MCPError(f"MCP stdio invalid JSON frame: {e}") from e
        if "error" in body:
            err = body["error"]
            raise MCPError(f"MCP JSON-RPC error {err.get('code')}: {err.get('message')}")
        return body.get("result", {})

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request("tools/list", {})
        return list(result.get("tools", []))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._request("tools/call", {"name": name, "arguments": arguments or {}})

    async def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:  # pragma: no cover - best effort
                pass
            self._proc = None


def _extract_json(response: Any) -> dict[str, Any]:
    """Extract the JSON-RPC body from a single-JSON or SSE response."""
    content_type = (response.headers.get("content-type") or "").lower()
    if content_type.startswith("text/event-stream"):
        data = response.text
        payloads = []
        for line in data.splitlines():
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if payload:
                    payloads.append(payload)
        if not payloads:
            raise MCPError("empty SSE response from MCP server")
        return json.loads(payloads[-1])
    try:
        return response.json()
    except Exception as e:
        raise MCPError(f"MCP server returned non-JSON body: {response.text[:200]}") from e


class MCPToolSource:
    """A tool source backed by one MCP server (registered per tenant).

    ``connect()`` performs the handshake and fetches the tool list; each
    discovered tool becomes a ``ToolSpec`` whose executor proxies
    ``tools/call`` and formats the MCP content blocks into the registry's
    ``{content, is_error}`` result contract.
    """

    def __init__(
        self,
        *,
        server_name: str,
        transport: str = "streamable-http",
        server_url: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        auth_token: str | None = None,
        timeout: float = 15.0,
        client_transport: Any = None,
    ):
        self.server_name = server_name
        self.transport = transport
        self._client: MCPClient | MCPStdioClient | None = None
        self._specs: list[Any] = []
        self._connected = False
        self._kwargs = dict(
            server_url=server_url,
            command=command,
            args=args,
            env=env,
            auth_token=auth_token,
            timeout=timeout,
        )
        self._client_transport = client_transport

    def _build_client(self) -> MCPClient | MCPStdioClient:
        if self.transport == "stdio":
            return MCPStdioClient(
                command=self._kwargs["command"] or "python",
                args=self._kwargs["args"],
                env=self._kwargs["env"],
                timeout=self._kwargs["timeout"],
            )
        if self._kwargs["server_url"] is None:
            raise MCPError(f"MCP server {self.server_name!r} has no server_url")
        return MCPClient(
            server_url=self._kwargs["server_url"],
            auth_token=self._kwargs["auth_token"],
            timeout=self._kwargs["timeout"],
            transport=self._client_transport,
        )

    async def connect(self) -> None:
        """Handshake + tool discovery. Safe to call once."""
        if self._connected:
            return
        client = self._build_client()
        await client.connect()
        tools = await client.list_tools()
        self._client = client
        self._specs = [self._make_spec(client, tool) for tool in tools]
        self._connected = True
        logger.info(
            "mcp_source_connected",
            server=self.server_name,
            tools=len(self._specs),
        )

    def _make_spec(self, client: MCPClient | MCPStdioClient, tool: dict[str, Any]) -> Any:
        from backend.app.application.tools import ToolSpec

        name = str(tool.get("name") or "")
        return ToolSpec(
            name=name,
            description=str(tool.get("description") or ""),
            parameters=tool.get("inputSchema") or {"type": "object"},
            executor=self._make_executor(client, name),
        )

    def _make_executor(
        self, client: MCPClient | MCPStdioClient, name: str
    ) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
        async def _execute(arguments: dict[str, Any]) -> dict[str, Any]:
            result = await client.call_tool(name, arguments or {})
            text_parts = [
                str(c.get("text", ""))
                for c in result.get("content", [])
                if c.get("type") == "text" and c.get("text")
            ]
            content = "\n".join(text_parts) if text_parts else json.dumps(result, default=str)
            return {"content": content, "is_error": bool(result.get("isError"))}

        return _execute

    def tool_specs(self) -> list[Any]:
        return list(self._specs)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._connected = False
