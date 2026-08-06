"""
Tests for tenant lifecycle: GDPR export/erasure/offboarding and the
immutable exportable audit trail (audit §4.19-4.22, ISO-42001 A.9).
"""

import asyncio
import json
from uuid import uuid4

import pytest

from backend.app.infrastructure.db import (
    ApiKeyRepository,
    AuditRepository,
    ConversationRepository,
    EscalationRepository,
    EvidenceRepository,
    TenantRepository,
    WebhookRepository,
)
from backend.app.infrastructure.db.manager import DatabaseConfig, DatabaseManager
from backend.app.infrastructure.db.models import Base


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(
        DatabaseConfig(database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/lc.db")
    )

    async def _init():
        await manager.initialize()
        async with manager._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    yield manager
    asyncio.run(manager.close())


def _seed_tenant_data(db):
    """Create a tenant + conversations/messages/evidence/escalations/webhooks."""

    async def _seed():
        tenant_repo = TenantRepository(db)
        tenant = await tenant_repo.create(
            {
                "id": str(uuid4()),
                "slug": "gdpr-co",
                "name": "GDPR Co",
                "allowed_topics": ["general"],
                "blocked_topics": [],
                "escalation_threshold": 0.7,
                "knowledge_allowlist": [],
                "default_provider": "openai",
                "default_model": "gpt-4",
                "features": {},
                "guardrail_config": {},
                "guardrail_thresholds": {},
            }
        )
        conv_repo = ConversationRepository(db)
        conversation = await conv_repo.get_or_create(tenant["id"], "session-1")
        await conv_repo.add_message(
            conversation["id"], "user", "hello world", redacted_content="hello"
        )
        await conv_repo.add_message(conversation["id"], "assistant", "hi there")

        await EvidenceRepository(db).add(
            {
                "tenant_id": tenant["id"],
                "conversation_id": conversation["id"],
                "session_id": "session-1",
                "direction": "input",
                "decision": "ALLOW",
                "allowed": True,
                "input_hash": "a" * 64,
                "violations": [],
                "layers_evaluated": ["regex"],
                "processing_time_ms": 1.0,
                "metadata": {},
            }
        )
        await EscalationRepository(db).add(
            {
                "tenant_id": tenant["id"],
                "session_id": "session-1",
                "category": "low_confidence",
                "severity": "medium",
                "status": "open",
                "reason": "low confidence",
            }
        )
        webhook_repo = WebhookRepository(db)
        await webhook_repo.create_subscription(
            tenant["id"], "https://hook.example/x", "secret-1234567890",
            ["conversation.completed"],
        )
        await webhook_repo.record_event(
            "evt-1", tenant["id"], "conversation.completed", {"session_id": "s1"}
        )
        await AuditRepository(db).add(
            action="tenant.created",
            resource_type="tenant",
            resource_id=tenant["id"],
            tenant_id=tenant["id"],
            actor_type="system",
        )
        return tenant

    return asyncio.run(_seed())


class TestLifecycleService:
    def test_export_contains_all_data_classes(self, db):
        from backend.app.application.tenant_lifecycle import TenantLifecycleService

        tenant = _seed_tenant_data(db)
        bundle = asyncio.run(TenantLifecycleService(db=db).export_tenant_data(tenant["id"]))

        assert bundle["tenant"]["id"] == tenant["id"]
        assert len(bundle["conversations"]) == 1
        assert len(bundle["conversations"][0]["messages"]) == 2
        assert len(bundle["guardrail_evidence"]) == 1
        assert len(bundle["escalations"]) == 1
        assert len(bundle["webhook_events"]) == 1
        assert len(bundle["audit_events"]) == 1

    def test_export_missing_tenant_raises(self, db):
        from backend.app.application.tenant_lifecycle import TenantLifecycleService

        with pytest.raises(ValueError):
            asyncio.run(
                TenantLifecycleService(db=db).export_tenant_data(str(uuid4()))
            )

    def test_erasure_keeps_tenant_and_audit(self, db):
        from backend.app.application.tenant_lifecycle import TenantLifecycleService

        tenant = _seed_tenant_data(db)
        service = TenantLifecycleService(db=db)
        deleted = asyncio.run(service.erase_tenant_data(tenant["id"]))

        assert deleted["conversations_and_messages"] >= 3
        assert deleted["guardrail_evidence"] == 1
        assert deleted["escalations"] == 1
        assert deleted["webhooks"] >= 2

        assert asyncio.run(TenantRepository(db).get_by_id(tenant["id"])) is not None
        audit = asyncio.run(AuditRepository(db).list_events(tenant_id=tenant["id"]))
        actions = {e["action"] for e in audit}
        assert "tenant.data_erased" in actions

    def test_offboard_deletes_tenant_and_revokes_keys(self, db):
        from backend.app.application.tenant_lifecycle import TenantLifecycleService

        tenant = _seed_tenant_data(db)
        key = asyncio.run(
            ApiKeyRepository(db).create(
                name="t1", key_hash="x" * 64, prefix="nrv_live_", role="tenant_admin",
                tenant_id=tenant["id"],
            )
        )
        service = TenantLifecycleService(db=db)
        assert asyncio.run(service.offboard_tenant(tenant["id"])) is True

        assert asyncio.run(TenantRepository(db).get_by_id(tenant["id"])) is None
        keys = asyncio.run(ApiKeyRepository(db).list_all())
        revoked = [k for k in keys if k["id"] == key["id"]]
        assert len(revoked) == 1 and revoked[0]["revoked"] is True

    def test_audit_trail_is_append_only(self, db):
        """No repository method mutates audit events (immutable)."""
        from backend.app.application.tenant_lifecycle import TenantLifecycleService

        tenant = _seed_tenant_data(db)
        before = asyncio.run(AuditRepository(db).list_events(tenant_id=tenant["id"]))
        assert len(before) == 1

        audit_repo = AuditRepository(db)
        assert not hasattr(audit_repo, "update")
        assert not hasattr(audit_repo, "delete")


class TestApiLifecycleSmoke:
    def _isolated_app(self, tmp_path):
        import backend.app.api.routes.conversations as conversations_module
        from backend.app.settings import feature_flags as flags_module
        from backend.app.settings.env import settings

        original = {
            "auth": settings.AUTH_ENABLED,
            "presidio": flags_module.feature_flags.ENABLE_PRESIDIO,
            "db_url": settings.DATABASE_URL,
            "cfg_path": settings.TENANT_CONFIG_PATH,
            "keys": [
                settings.OPENAI_API_KEY,
                settings.ANTHROPIC_API_KEY,
                settings.GOOGLE_API_KEY,
                settings.AZURE_API_KEY,
                settings.CUSTOM_LLM_API_KEY,
            ],
        }

        def restore():
            settings.AUTH_ENABLED = original["auth"]
            settings.DATABASE_URL = original["db_url"]
            settings.TENANT_CONFIG_PATH = original["cfg_path"]
            settings.OPENAI_API_KEY, settings.ANTHROPIC_API_KEY, settings.GOOGLE_API_KEY = original["keys"][:3]
            settings.AZURE_API_KEY, settings.CUSTOM_LLM_API_KEY = original["keys"][3:]
            object.__setattr__(flags_module.feature_flags, "ENABLE_PRESIDIO", original["presidio"])
            conversations_module._rag_services.clear()

        settings.AUTH_ENABLED = False
        object.__setattr__(flags_module.feature_flags, "ENABLE_PRESIDIO", False)
        settings.DATABASE_URL = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/lc-smoke.db"
        settings.TENANT_CONFIG_PATH = str(tmp_path / "tenant_configs")
        for attr in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "AZURE_API_KEY", "CUSTOM_LLM_API_KEY"):
            setattr(settings, attr, None)

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

    def _provision_tenant(self, client, slug):
        resp = client.post(
            "/api/v1/tenants",
            json={
                "slug": slug,
                "name": "Lifecycle Co",
                "allowed_topics": ["general"],
                "blocked_topics": [],
                "default_provider": "openai",
                "default_model": "gpt-4",
                "escalation_threshold": 0.5,
            },
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_gdpr_export_and_audit_export(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                tenant = self._provision_tenant(client, "lifecycleco")
                resp = client.get(f"/api/v1/tenants/{tenant['id']}/gdpr/export")
                assert resp.status_code == 200, resp.text
                assert "attachment" in resp.headers["content-disposition"]
                bundle = resp.json()
                assert bundle["tenant"]["id"] == tenant["id"]

                audit = client.get("/api/v1/audit/export")
                assert audit.status_code == 200
                assert audit.json()["immutable"] is True
                actions = {e["action"] for e in audit.json()["events"]}
                assert "tenant.created" in actions
        finally:
            restore()

    def test_gdpr_erasure_and_offboard(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                tenant = self._provision_tenant(client, "lifecycleco2")
                erased = client.delete(f"/api/v1/tenants/{tenant['id']}/data")
                assert erased.status_code == 200, erased.text
                assert "deleted" in erased.json()

                offboarded = client.delete(f"/api/v1/tenants/{tenant['id']}")
                assert offboarded.status_code == 200
                assert offboarded.json()["status"] == "offboarded"

                gone = client.get(f"/api/v1/tenants/{tenant['id']}")
                assert gone.status_code == 404
        finally:
            restore()
