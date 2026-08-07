"""
P5-8 endpoint isolation tests (Arch §6.3.10).

Credential class cross-use must fail:

- operator/tenant endpoints (get_principal + X-API-Key) reject session
  tokens and tenant-bound keys from a different tenant
- customer session-token endpoints (Bearer) reject operator keys
- a session token minted for tenant A can never reach tenant B work

Covered at HTTP level with an isolated app (AUTH_ENABLED=True, temp
SQLite) mirroring the harness in test_operations.py, plus unit-level
checks of the principal dispatch + tenant assert.
"""

import asyncio

import pytest
from fastapi import HTTPException

from backend.app.api.dependencies.auth import generate_api_key, hash_api_key
from backend.app.infrastructure.db import (
    ApiKeyRepository,
    get_database_manager,
)
from backend.app.infrastructure.db.models import Base
from backend.app.settings.env import settings


class TestEndpointIsolation:
    def _isolated_app(self, tmp_path):
        original = {
            "auth": settings.AUTH_ENABLED,
            "db_url": settings.DATABASE_URL,
            "cfg_path": settings.TENANT_CONFIG_PATH,
            "queue_url": settings.REDIS_URL,
        }

        def restore():
            settings.AUTH_ENABLED = original["auth"]
            settings.DATABASE_URL = original["db_url"]
            settings.TENANT_CONFIG_PATH = original["cfg_path"]
            settings.REDIS_URL = original["queue_url"]

        settings.AUTH_ENABLED = True
        settings.DATABASE_URL = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/iso.db"
        settings.TENANT_CONFIG_PATH = str(tmp_path / "tenant_configs")
        settings.REDIS_URL = "redis://127.0.0.1:1"  # unreachable -> in-memory fallback

        import asyncio

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

    def _provision(self, client, key, slug="isocco", name="Isolation Co"):
        tenant_resp = client.post(
            "/api/v1/tenants",
            json={
                "slug": slug,
                "name": name,
                "allowed_topics": ["general"],
                "blocked_topics": [],
                "default_provider": "openai",
                "default_model": "gpt-4",
            },
            headers={"X-API-Key": key},
        )
        assert tenant_resp.status_code == 200, tenant_resp.text
        return tenant_resp.json()["id"]

    def _create_key(self, role: str, tenant_id=None) -> str:
        db = get_database_manager()
        repo = ApiKeyRepository(db)
        raw, prefix, key_hash = generate_api_key()
        import asyncio

        asyncio.run(
            repo.create(
                name=f"iso-{role}",
                key_hash=key_hash,
                prefix=prefix,
                role=role,
                tenant_id=tenant_id,
            )
        )
        return raw

    def test_session_only_endpoint_rejects_operator_key(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                raw_key = self._create_key("super_admin")
                # Super-admin operator key presented as a Bearer session token
                resp = client.get(
                    "/api/v1/session-tokens/me",
                    headers={"Authorization": f"Bearer {raw_key}"},
                )
                # Not a minted Fernet session token -> 401, never a success
                assert resp.status_code == 401, resp.text
        finally:
            restore()

    def test_session_only_endpoint_accepts_real_session(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                mint = client.post(
                    "/api/v1/session-tokens",
                    json={"tenant_id": "tenant-a", "device_id": "dev-1"},
                )
                assert mint.status_code == 200, mint.text
                token = mint.json()["token"]
                resp = client.get(
                    "/api/v1/session-tokens/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert resp.status_code == 200, resp.text
                assert resp.json()["tenant_id"] == "tenant-a"
        finally:
            restore()

    def test_operator_endpoint_rejects_session_token(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                session = client.post(
                    "/api/v1/session-tokens",
                    json={"tenant_id": "tenant-a", "device_id": "dev-1"},
                )
                token = session.json()["token"]
                # A session token is not a valid API key on operator endpoints
                resp = client.get(
                    "/api/v1/tenants", headers={"X-API-Key": token}
                )
                assert resp.status_code == 401, resp.text
        finally:
            restore()

    def test_tenant_bound_key_cannot_reach_other_tenant(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                admin = self._create_key("super_admin")
                a_id = self._provision(client, admin)
                b_id = self._provision(client, admin, slug="isobco", name="Iso B")
                bound = self._create_key("tenant_admin", tenant_id=a_id)
                # Own tenant is reachable
                ok = client.get(
                    f"/api/v1/tenants/{a_id}", headers={"X-API-Key": bound}
                )
                assert ok.status_code == 200, ok.text
                # The bound key must NOT reach tenant B
                denied = client.get(
                    f"/api/v1/tenants/{b_id}", headers={"X-API-Key": bound}
                )
                assert denied.status_code == 403, denied.text
        finally:
            restore()


class TestConversationTenantIsolation:
    """P5-8: session tokens are single-tenant; cross-tenant use is forbidden."""

    def test_session_principal_mismatch_rejected(self):
        from backend.app.api.dependencies.session import ConversationPrincipal

        from backend.app.domain.tenant import TenantConfig

        config = TenantConfig(
            id="tenant-b",
            slug="bco",
            name="",
            default_provider="openai",
            default_model="gpt-4",
        )
        principal = ConversationPrincipal(
            tenant_id="tenant-a",
            end_user_id="u1",
            role="end_user",
            is_session=True,
        )
        from backend.app.api.routes.conversations import _assert_conversation_tenant

        with pytest.raises(HTTPException) as exc:
            _assert_conversation_tenant(principal, config)
        assert exc.value.status_code == 403