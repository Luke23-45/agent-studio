"""
P9-1 tests: MCP client integration as a tool source behind the P5-3 gate.

- streamable-HTTP transport: initialize handshake, session id, tools/list,
  tools/call, JSON-RPC + HTTP errors, SSE response parsing
- stdio transport: JSON-RPC frames over a spawned child process
- MCPToolSource: spec discovery + result formatting ({content, is_error})
- ToolRegistry.register_source: collision handling
- factory: tenant tool_registry rows -> registry (enabled only, fail-safe
  on unreachable servers)
- gate integration: MCP tool calls are authorized by the P5-3 gate exactly
  like local tools (deny by default, surface allowlist, enablement)
"""

import json
from uuid import uuid4

import httpx
import pytest

from backend.app.adapters.tools.mcp import (
    MCPClient,
    MCPError,
    MCPStdioClient,
    MCPToolSource,
)
from backend.app.application.tools import ToolRegistry, ToolSpec
from backend.app.governance.toolgate import ToolAuthorizationGate
from backend.app.domain.tenant import TenantConfig


def _handler(method_results=None, **kwargs):
    """Build a MockTransport handler serving JSON-RPC methods."""
    method_results = dict(method_results or {})
    method_results.update(kwargs)

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        response: dict = {"jsonrpc": "2.0", "id": body.get("id")}
        if method in method_results:
            result = method_results[method]
            if callable(result):
                result = result(request, body)
            response["result"] = result
        elif method == "initialize":
            response["result"] = {"serverInfo": {"name": "test-server", "version": "1"}}
        elif method == "notifications/initialized":
            return httpx.Response(202)
        else:
            response["error"] = {"code": -32601, "message": f"method not found: {method}"}
        return httpx.Response(200, json=response)

    return handler


TOOLS_LIST = {
    "tools": [
        {
            "name": "weather",
            "description": "current weather for a city",
            "inputSchema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
        {
            "name": "echo",
            "description": "echo text",
            "inputSchema": {"type": "object"},
        },
    ]
}


def _client(handler):
    return MCPClient(
        server_url="https://mcp.example/tools",
        auth_token="tok-123",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )


class TestMCPClientHTTP:
    async def test_handshake_and_tool_list(self):
        calls = []
        client = MCPClient(
            server_url="https://mcp.example/tools",
            auth_token="tok-123",
            timeout=5.0,
            transport=httpx.MockTransport(_recorder(calls)),
        )
        await client.connect()
        tools = await client.list_tools()
        assert [t["name"] for t in tools] == ["weather", "echo"]

        methods = [c["body"]["method"] for c in calls]
        assert methods[0] == "initialize"
        assert "notifications/initialized" in methods
        # bearer auth sent on every request
        for c in calls:
            assert c["request"].headers["authorization"] == "Bearer tok-123"

    async def test_call_tool_round_trip(self):
        calls = []

        def on_call(request, body):
            calls.append(body.get("params"))
            return {"content": [{"type": "text", "text": "sunny 22c"}], "isError": False}

        client = _client(_handler({"tools/call": on_call}))
        await client.connect()
        result = await client.call_tool("weather", {"city": "berlin"})
        assert calls == [{"name": "weather", "arguments": {"city": "berlin"}}]
        assert result["content"][0]["text"] == "sunny 22c"
        assert result["isError"] is False

    async def test_session_id_header_is_captured(self):
        def handler(request):
            return httpx.Response(
                200, headers={"mcp-session-id": "sess-42"}, json={"jsonrpc": "2.0", "id": 1, "result": {}}
            )

        client = _client(handler)
        await client.connect()
        assert client._session_id == "sess-42"

    async def test_jsonrpc_error_raises(self):
        async def handler(request):
            body = json.loads(request.content)
            if body.get("method") == "initialize":
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": body.get("id"), "result": {}},
                )
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}},
            )

        client = _client(handler)
        await client.connect()
        with pytest.raises(MCPError, match="boom"):
            await client.list_tools()

    async def test_http_error_raises(self):
        client = _client(lambda request: httpx.Response(503, text="unavailable"))
        with pytest.raises(MCPError, match="503"):
            await client.connect()

    async def test_sse_response_is_parsed(self):
        sse = (
            'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":['
            '{"name":"sse-tool","description":"d","inputSchema":{"type":"object"}}]}}\n\n'
        )

        def handler(request):
            return httpx.Response(
                200,
                content=sse,
                headers={"content-type": "text/event-stream"},
            )

        client = _client(handler)
        await client.connect()
        tools = await client.list_tools()
        assert tools[0]["name"] == "sse-tool"


def _recorder(calls):
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append({"request": request, "body": body})
        if body.get("method") == "initialize":
            result = {"serverInfo": {"name": "test", "version": "1"}}
        elif body.get("method") == "tools/list":
            result = TOOLS_LIST
        elif body.get("method") == "tools/call":
            result = {"content": [{"type": "text", "text": "ok"}], "isError": False}
        else:
            result = {}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": result})

    return handler


class TestMCPStdioClient:
    @pytest.mark.asyncio
    async def test_stdio_round_trip(self):
        script = (
            "import sys,json\n"
            "for line in sys.stdin:\n"
            "    msg=json.loads(line)\n"
            "    m=msg.get('method')\n"
            "    out={'jsonrpc':'2.0'}\n"
            "    if 'id' in msg: out['id']=msg['id']\n"
            "    if m=='initialize': out['result']={'serverInfo':{'name':'child','version':'1'}}\n"
            "    elif m=='tools/list': out['result']={'tools':[{'name':'child-tool','description':'d','inputSchema':{'type':'object'}}]}\n"
            "    elif m=='tools/call': out['result']={'content':[{'type':'text','text':'child says hi'}],'isError':False}\n"
            "    else: continue\n"
            "    sys.stdout.write(json.dumps(out)+'\\n'); sys.stdout.flush()\n"
        )
        import sys

        client = MCPStdioClient(
            command=sys.executable,
            args=["-c", script],
            timeout=10.0,
        )
        try:
            await client.connect()
            tools = await client.list_tools()
            assert tools[0]["name"] == "child-tool"
            result = await client.call_tool("child-tool", {})
            assert result["content"][0]["text"] == "child says hi"
        finally:
            await client.close()


class TestMCPToolSource:
    @pytest.mark.asyncio
    async def test_source_discovers_specs_and_executes(self):
        calls = []

        def on_call(request, body):
            calls.append(body)
            return {
                "content": [{"type": "text", "text": "sunny"}],
                "isError": body.get("params", {}).get("arguments", {}).get("city") == "bad",
            }

        source = MCPToolSource(
            server_name="weather-mcp",
            server_url="https://mcp.example/tools",
            auth_token="tok",
            timeout=5.0,
            client_transport=httpx.MockTransport(
                _handler({"tools/list": TOOLS_LIST, "tools/call": on_call})
            ),
        )
        await source.connect()
        specs = source.tool_specs()
        assert [s.name for s in specs] == ["weather", "echo"]

        registry = ToolRegistry()
        added = await registry.register_source(source)
        assert added == 2

        ok = await registry.execute("weather", {"city": "berlin"})
        assert ok["content"] == "sunny"
        assert ok["is_error"] is False

        err = await registry.execute("weather", {"city": "bad"})
        assert err["is_error"] is True
        assert "weather" in calls[0]["params"]["name"]
        await source.close()

    @pytest.mark.asyncio
    async def test_unreachable_server_raises_on_connect(self):
        source = MCPToolSource(
            server_name="down",
            server_url="https://down.example/tools",
            timeout=1.0,
            client_transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        )
        with pytest.raises(MCPError):
            await source.connect()


class TestRegisterSource:
    @pytest.mark.asyncio
    async def test_collision_skips_second_registrant(self):
        class FakeSource:
            def __init__(self):
                self.server_name = "fake"

            async def connect(self):
                pass

            def tool_specs(self):
                return [ToolSpec(name="dup", description="from source")]

            async def close(self):
                pass

        registry = ToolRegistry()
        registry.register(ToolSpec(name="dup", description="local"))
        added = await registry.register_source(FakeSource())
        assert added == 0
        assert registry.get("dup").description == "local"

    @pytest.mark.asyncio
    async def test_close_releases_sources(self):
        class FakeSource:
            def __init__(self):
                self.server_name = "fake"
                self.closed = False

            async def connect(self):
                pass

            def tool_specs(self):
                return [ToolSpec(name="t1", description="d")]

            async def close(self):
                self.closed = True

        source = FakeSource()
        registry = ToolRegistry()
        await registry.register_source(source)
        await registry.close()
        assert source.closed is True
        # idempotent: second close does not raise
        await registry.close()

    @pytest.mark.asyncio
    async def test_close_never_raises_on_broken_source(self):
        class BrokenSource:
            server_name = "broken"

            def tool_specs(self):
                return []

            def close(self):
                raise RuntimeError("boom")

        registry = ToolRegistry()
        await registry.register_source(BrokenSource())
        await registry.close()  # must not raise


class TestArgumentValidation:
    @pytest.mark.asyncio
    async def test_invalid_arguments_rejected_before_executor(self):
        calls = []
        spec = ToolSpec(
            name="flight",
            description="book a flight",
            parameters={
                "type": "object",
                "properties": {
                    "origin": {"type": "string"},
                    "passengers": {"type": "integer", "minimum": 1},
                },
                "required": ["origin"],
            },
            executor=lambda args: calls.append(args) or {"content": "booked"},
        )
        registry = ToolRegistry()
        registry.register(spec)

        missing = await registry.execute("flight", {"passengers": 2})
        assert missing["is_error"] is True
        assert "origin" in missing["content"]

        bad_type = await registry.execute("flight", {"origin": "BER", "passengers": "two"})
        assert bad_type["is_error"] is True
        assert "passengers" in bad_type["content"]

        # valid args reach the executor untouched
        ok = await registry.execute("flight", {"origin": "BER", "passengers": 2})
        assert ok["is_error"] is False
        assert calls == [{"origin": "BER", "passengers": 2}]

    @pytest.mark.asyncio
    async def test_non_object_arguments_rejected(self):
        registry = ToolRegistry()
        registry.register(ToolSpec(name="x", description="d"))
        result = await registry.execute("x", ["not", "an", "object"])
        assert result["is_error"] is True
        assert "object" in result["content"]

    @pytest.mark.asyncio
    async def test_open_schema_accepts_anything(self):
        registry = ToolRegistry()
        registry.register(ToolSpec(name="open", description="d"))
        result = await registry.execute("open", {"anything": [1, 2, 3]})
        assert result["is_error"] is False


class TestBuildToolRegistry:
    @pytest.fixture
    async def db(self, tmp_path):
        from backend.app.infrastructure.db import init_database
        from backend.app.infrastructure.db.models import Base

        manager = init_database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db")
        await manager.initialize()
        async with manager._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield manager
        await manager.close()

    @pytest.mark.asyncio
    async def test_builds_registry_from_enabled_mcp_rows(self, db):
        from backend.app.application.tools.factory import build_tool_registry
        from backend.app.infrastructure.db import ToolRegistryRepository

        tenant_id = str(uuid4())
        repo = ToolRegistryRepository(db)
        await repo.register(
            tenant_id,
            "weather-mcp",
            description="server",
            enabled=True,
            auth_config={
                "type": "mcp",
                "transport": "streamable-http",
                "server_url": "https://mcp.example/tools",
                "auth_token": "tok",
            },
        )
        # disabled row must NOT be connected
        await repo.register(
            tenant_id,
            "disabled-mcp",
            enabled=False,
            auth_config={
                "type": "mcp",
                "transport": "streamable-http",
                "server_url": "https://mcp.example/tools",
            },
        )
        # non-MCP row must be skipped
        await repo.register(tenant_id, "local-tool", enabled=True, auth_config={})

        registry = await build_tool_registry(
            db=db,
            tenant_id=tenant_id,
            client_transport=httpx.MockTransport(_recorder([])),
        )
        names = {s.name for s in registry.list()}
        assert names == {"weather", "echo"}
        result = await registry.execute("weather", {"city": "x"})
        assert result["content"] == "ok"

    @pytest.mark.asyncio
    async def test_unreachable_server_degrades_to_empty(self, db):
        from backend.app.application.tools.factory import build_tool_registry
        from backend.app.infrastructure.db import ToolRegistryRepository

        tenant_id = str(uuid4())
        await ToolRegistryRepository(db).register(
            tenant_id,
            "down-mcp",
            enabled=True,
            auth_config={
                "type": "mcp",
                "transport": "streamable-http",
                "server_url": "https://down.example/tools",
            },
        )
        registry = await build_tool_registry(
            db=db,
            tenant_id=tenant_id,
            client_transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        )
        assert registry.list() == []

    @pytest.mark.asyncio
    async def test_mcp_disabled_flag_yields_empty_registry(self, db):
        from backend.app.application.tools.factory import build_tool_registry
        from backend.app.infrastructure.db import ToolRegistryRepository

        tenant_id = str(uuid4())
        await ToolRegistryRepository(db).register(
            tenant_id,
            "weather-mcp",
            enabled=True,
            auth_config={
                "type": "mcp",
                "transport": "streamable-http",
                "server_url": "https://mcp.example/tools",
            },
        )
        registry = await build_tool_registry(
            db=db,
            tenant_id=tenant_id,
            enable_mcp=False,
        )
        assert registry.list() == []


class TestGateIntegration:
    def test_mcp_tool_denied_unless_enabled_and_allowlisted(self):
        tenant = TenantConfig(id=uuid4(), name="t", slug="t")
        # tool exists in the gate's registered map (enabled)
        gate = ToolAuthorizationGate(
            tenant_config=tenant,
            registered_tools={"weather": True},
            surface_allowlist=["weather"],
        )
        assert gate.authorize({"name": "weather"}).allowed is True
        # not on allowlist
        gate2 = ToolAuthorizationGate(
            tenant_config=tenant,
            registered_tools={"weather": True},
            surface_allowlist=[],
        )
        decision = gate2.authorize({"name": "weather"})
        assert decision.allowed is False
        assert "allowlist" in decision.reason
        # disabled in tenant registry
        gate3 = ToolAuthorizationGate(
            tenant_config=tenant,
            registered_tools={"weather": False},
            surface_allowlist=["weather"],
        )
        assert gate3.authorize({"name": "weather"}).allowed is False
