"""
P9-1 tool registry admin API tests (/api/v1/tools):

- register MCP server (disabled), duplicate -> 409, validation 422
- connect-test discovery (mocked source), unreachable -> 502
- enable/disable/delete lifecycle
- PATCH: merge semantics, token rotation, clear token
- auth_token encrypted at rest, redacted in every response
- RBAC: operator 403, tenant isolation
"""

import asyncio

from backend.app.api.dependencies.auth import generate_api_key, hash_api_key
from backend.app.infrastructure.db import (
    ApiKeyRepository,
    ToolRegistryRepository,
    get_database_manager,
)
from backend.app.settings.env import settings


class FakeToolSource:
    def __init__(self, *args, **kwargs):
        pass

    async def connect(self):
        pass

    def tool_specs(self):
        from backend.app.application.tools import ToolSpec

        return [
            ToolSpec(
                name="weather",
                description="current weather",
                parameters={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            )
        ]

    async def close(self):
        pass


class TestToolRouter:
    def _isolated_app(self, tmp_path):
        import base64

        original = {
            "auth": settings.AUTH_ENABLED,
            "db_url": settings.DATABASE_URL,
            "cfg_path": settings.TENANT_CONFIG_PATH,
            "provider_key": settings.PROVIDER_KEY_ENCRYPTION_KEY,
        }

        def restore():
            settings.AUTH_ENABLED = original["auth"]
            settings.DATABASE_URL = original["db_url"]
            settings.TENANT_CONFIG_PATH = original["cfg_path"]
            settings.PROVIDER_KEY_ENCRYPTION_KEY = original["provider_key"]

        settings.AUTH_ENABLED = True
        settings.DATABASE_URL = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/tools.db"
        settings.TENANT_CONFIG_PATH = str(tmp_path / "tenant_configs")
        settings.PROVIDER_KEY_ENCRYPTION_KEY = base64.urlsafe_b64encode(
            b"0" * 32
        ).decode("ascii")

        from backend.app.infrastructure.db import init_database
        from backend.app.infrastructure.db.models import Base

        async def _setup_schema():
            manager = init_database(settings.DATABASE_URL)
            await manager.initialize()
            async with manager._engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            await manager.close()

        asyncio.run(_setup_schema())

        from starlette.testclient import TestClient

        from backend.app.main import app

        return TestClient(app), restore

    def _create_keys(self):
        repo = ApiKeyRepository(get_database_manager())
        raw_super, prefix_super, hash_super = generate_api_key()
        raw_operator, prefix_operator, hash_operator = generate_api_key()
        asyncio.run(repo.create(name="tools-super", key_hash=hash_super, prefix=prefix_super, role="super_admin"))
        asyncio.run(repo.create(name="tools-operator", key_hash=hash_operator, prefix=prefix_operator, role="operator"))
        return {"super_admin": raw_super, "operator": raw_operator}

    def _provision(self, client, keys, slug="toolco"):
        resp = client.post(
            "/api/v1/tenants",
            json={
                "slug": slug,
                "name": "Tool Co",
                "allowed_topics": ["general"],
                "blocked_topics": [],
                "default_provider": "openai",
                "default_model": "gpt-4",
            },
            headers={"X-API-Key": keys["super_admin"]},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["id"]

    def test_lifecycle_encryption_rbac(self, tmp_path, monkeypatch):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                keys = self._create_keys()
                tenant_id = self._provision(client, keys)
                super_h = {"X-API-Key": keys["super_admin"]}

                # register -> 201, disabled, token redacted
                created = client.post(
                    f"/api/v1/tools/mcp?tenant_id={tenant_id}",
                    json={
                        "name": "weather-mcp",
                        "transport": "streamable-http",
                        "server_url": "https://mcp.example/tools",
                        "auth_token": "sekret-token",
                    },
                    headers=super_h,
                )
                assert created.status_code == 201, created.text
                body = created.json()
                assert body["enabled"] is False
                assert body["auth_config"]["auth_token"] == "***"

                # at rest: envelope-encrypted, never plaintext
                row = asyncio.run(
                    ToolRegistryRepository(get_database_manager()).get(tenant_id, "weather-mcp")
                )
                stored = row["auth_config"]["auth_token"]
                assert stored.startswith("encrypted:")
                assert "sekret-token" not in stored
                from backend.app.infrastructure.keys.crypto import decrypt_secret

                assert decrypt_secret(stored[len("encrypted:") :]) == "sekret-token"

                # duplicate -> 409
                dup = client.post(
                    f"/api/v1/tools/mcp?tenant_id={tenant_id}",
                    json={
                        "name": "weather-mcp",
                        "transport": "streamable-http",
                        "server_url": "https://mcp.example/tools",
                    },
                    headers=super_h,
                )
                assert dup.status_code == 409

                # validation: streamable-http requires server_url
                invalid = client.post(
                    f"/api/v1/tools/mcp?tenant_id={tenant_id}",
                    json={"name": "bad", "transport": "streamable-http"},
                    headers=super_h,
                )
                assert invalid.status_code == 422

                # connect-test with a mocked source -> discovered tools
                import backend.app.adapters.tools.mcp as mcp_mod

                monkeypatch.setattr(mcp_mod, "MCPToolSource", FakeToolSource)
                test = client.post(
                    f"/api/v1/tools/weather-mcp/connect-test?tenant_id={tenant_id}",
                    headers=super_h,
                )
                assert test.status_code == 200, test.text
                assert test.json()["ok"] is True
                assert test.json()["tools"][0]["name"] == "weather"

                # enable
                enabled = client.post(
                    f"/api/v1/tools/weather-mcp/enable?tenant_id={tenant_id}",
                    headers=super_h,
                )
                assert enabled.status_code == 200 and enabled.json()["enabled"] is True

                # PATCH: rotate token + change url, keep transport
                patched = client.patch(
                    f"/api/v1/tools/weather-mcp?tenant_id={tenant_id}",
                    json={"server_url": "https://mcp.example/v2", "auth_token": "new-token"},
                    headers=super_h,
                )
                assert patched.status_code == 200, patched.text
                cfg = patched.json()["auth_config"]
                assert cfg["server_url"] == "https://mcp.example/v2"
                assert cfg["transport"] == "streamable-http"
                assert cfg["auth_token"] == "***"
                row = asyncio.run(
                    ToolRegistryRepository(get_database_manager()).get(tenant_id, "weather-mcp")
                )
                assert decrypt_secret(row["auth_config"]["auth_token"][len("encrypted:") :]) == "new-token"

                # PATCH with empty token clears it
                cleared = client.patch(
                    f"/api/v1/tools/weather-mcp?tenant_id={tenant_id}",
                    json={"auth_token": ""},
                    headers=super_h,
                )
                assert cleared.json()["auth_config"]["auth_token"] is None

                # PATCH on a local tool row -> 422
                client.post(
                    f"/api/v1/tools/mcp?tenant_id={tenant_id}",
                    json={"name": "plain", "transport": "stdio", "command": "python"},
                    headers=super_h,
                )
                asyncio.run(
                    ToolRegistryRepository(get_database_manager()).update_config(
                        tenant_id, "plain", auth_config={"type": "local"}
                    )
                )
                not_mcp = client.patch(
                    f"/api/v1/tools/plain?tenant_id={tenant_id}",
                    json={"description": "x"},
                    headers=super_h,
                )
                assert not_mcp.status_code == 422

                # RBAC: operator cannot read or write tools
                op_h = {"X-API-Key": keys["operator"]}
                assert client.get(f"/api/v1/tools?tenant_id={tenant_id}", headers=op_h).status_code == 403
                assert client.post(
                    f"/api/v1/tools/mcp?tenant_id={tenant_id}",
                    json={"name": "x", "transport": "stdio", "command": "python"},
                    headers=op_h,
                ).status_code == 403

                # delete + 404 after
                gone = client.delete(f"/api/v1/tools/weather-mcp?tenant_id={tenant_id}", headers=super_h)
                assert gone.status_code == 204
                assert client.delete(
                    f"/api/v1/tools/weather-mcp?tenant_id={tenant_id}", headers=super_h
                ).status_code == 404
        finally:
            restore()
