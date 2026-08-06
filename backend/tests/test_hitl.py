"""
Tests for human-in-the-loop gates and loop budgets (P1):

- Loop budget: configurable ``max_redact_iterations`` stops the redact
  regeneration loop and flags ``budget_exceeded`` on the final state
- HITL gates: pause/resume conversation (423 Locked while paused),
  operator assign/resolve on escalations, conversation_id + external_ref
  persisted on handoff
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.adapters.llm import LLMProviderType
from backend.app.application.orchestration import create_orchestration_service
from backend.app.domain.policy import PolicyAction, PolicyRule, PolicySet, PolicyType
from backend.app.domain.tenant import TenantConfig
from backend.app.infrastructure.db import ConversationRepository, EscalationRepository
from backend.app.infrastructure.db.manager import DatabaseConfig, DatabaseManager
from backend.app.infrastructure.db.models import Base
from backend.app.modules.escalation import create_escalation_service
from backend.tests.gateway_fakes import FakeGateway


class _FakeAdapter:
    provider_type = LLMProviderType.OPENAI

    def __init__(self):
        self.config = SimpleNamespace(model="gpt-4")

    async def chat(self, messages):
        return SimpleNamespace(content="A complete and useful answer for the tenant.")


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(
        DatabaseConfig(database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/hitl.db")
    )

    async def _init():
        await manager.initialize()
        async with manager._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    yield manager
    asyncio.run(manager.close())


def _redact_policy_set(tenant_id, escalation_threshold: float = 0.5) -> PolicySet:
    return PolicySet(
        id=uuid4(),
        tenant_id=tenant_id,
        name="default",
        version=1,
        rules=[
            PolicyRule(
                name="redact-responses",
                policy_type=PolicyType.OUTPUT_VALIDATION,
                action=PolicyAction.REDACT,
                conditions={"confidence": 0.85},
                priority=1,
            )
        ],
    )


class TestLoopBudget:
    """Configurable redact-loop budget stops regeneration loops."""

    def _orchestration(self, tenant_config, policy_set):
        service = create_orchestration_service(
            tenant_config=tenant_config,
            policy_set=policy_set,
            gateway=FakeGateway(),
        )
        service.gateway = FakeGateway(chat_stub=_FakeAdapter())
        return service

    def test_default_budget_allows_two_iterations(self):
        tenant = TenantConfig(
            id=uuid4(), name="Acme", slug="acme",
            default_provider="openai", default_model="gpt-4",
            escalation_threshold=0.5,
        )
        service = self._orchestration(tenant, _redact_policy_set(tenant.id))
        result = asyncio.run(service.process_message("hi", "hi", session_id="s1"))
        assert result["redact_attempts"] == 2
        assert result["budget_exceeded"] is True
        assert result["error"] is None

    def test_tenant_budget_one_iteration_short_circuits(self):
        tenant = TenantConfig(
            id=uuid4(), name="Acme", slug="acme",
            default_provider="openai", default_model="gpt-4",
            escalation_threshold=0.5,
            budgets={"max_redact_iterations": 1, "max_graph_steps": 50, "max_duration_s": 60},
        )
        service = self._orchestration(tenant, _redact_policy_set(tenant.id))
        result = asyncio.run(service.process_message("hi", "hi", session_id="s1"))
        assert result["redact_attempts"] == 1
        assert result["budget_exceeded"] is True

    def test_no_loop_without_redact_policy(self):
        tenant = TenantConfig(
            id=uuid4(), name="Acme", slug="acme",
            default_provider="openai", default_model="gpt-4",
            escalation_threshold=0.5,
        )
        policy_set = PolicySet(id=uuid4(), tenant_id=tenant.id, name="default", version=1)
        service = self._orchestration(tenant, policy_set)
        result = asyncio.run(service.process_message("hi", "hi", session_id="s1"))
        assert result["redact_attempts"] == 0
        assert result["budget_exceeded"] is False


class TestHandoffPersistence:
    async def test_create_handoff_persists_conversation_id(self, db):
        tenant = TenantConfig(id=uuid4(), name="Acme", slug="acme")
        service = create_escalation_service(
            tenant_config=tenant, escalation_repository=EscalationRepository(db)
        )
        await service.create_handoff(
            session_id="s1",
            user_message="help me",
            confidence=0.2,
            reason="low_confidence",
            policy_action=PolicyAction.ALLOW,
            conversation_history=[],
            conversation_id="conv-1",
        )
        rows = await EscalationRepository(db).list_by_conversation("conv-1")
        assert len(rows) == 1
        assert rows[0]["conversation_id"] == "conv-1"
        assert rows[0]["status"] == "pending"
        assert rows[0]["tenant_id"] == str(tenant.id)

    async def test_external_ref_persisted_when_ticket_created(self, db, monkeypatch):
        import aiohttp

        tenant = TenantConfig(id=uuid4(), name="Acme", slug="acme")
        service = create_escalation_service(
            tenant_config=tenant,
            ticketing_webhook_url="https://tickets.example/hook",
            escalation_repository=EscalationRepository(db),
        )

        class _FakeCtx:
            status = 201

            async def json(self):
                return {"ticket_id": "TKT-123"}

            async def text(self):
                return ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        def fake_post(self, url, json=None, headers=None):
            return _FakeCtx()

        monkeypatch.setattr(aiohttp.ClientSession, "post", fake_post)
        response = await service.create_handoff(
            session_id="s1",
            user_message="help me",
            confidence=0.2,
            reason="low_confidence",
            policy_action=PolicyAction.ALLOW,
            conversation_history=[],
        )
        assert response.success
        assert response.ticket_id == "TKT-123"
        rows = await EscalationRepository(db).list_by_tenant(str(tenant.id))
        assert rows[0]["external_ref"] == "TKT-123"

    async def test_no_conversation_id_keeps_null(self, db):
        tenant = TenantConfig(id=uuid4(), name="Acme", slug="acme")
        service = create_escalation_service(
            tenant_config=tenant, escalation_repository=EscalationRepository(db)
        )
        await service.create_handoff(
            session_id="s1",
            user_message="help me",
            confidence=0.2,
            reason="low_confidence",
            policy_action=PolicyAction.ALLOW,
            conversation_history=[],
        )
        rows = await EscalationRepository(db).list_by_tenant(str(tenant.id))
        assert rows[0]["conversation_id"] is None


class TestEscalationTransitions:
    def test_assign_and_resolve_flow(self, db):
        tenant_id = str(uuid4())
        repo = EscalationRepository(db)
        row = asyncio.run(
            repo.add(
                {
                    "id": str(uuid4()),
                    "tenant_id": tenant_id,
                    "conversation_id": "conv-1",
                    "session_id": "s1",
                    "category": "general",
                    "severity": "high",
                    "status": "pending",
                    "reason": "low_confidence",
                    "summary": "help",
                    "details": {"confidence": 0.2},
                    "channel": "generic",
                }
            )
        )
        assigned = asyncio.run(repo.update_status(row["id"], "in_review"))
        assert assigned["status"] == "in_review"
        resolved = asyncio.run(
            repo.update_status(row["id"], "resolved", resolved_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
        )
        assert resolved["status"] == "resolved"
        assert resolved["resolved_at"] is not None

    def test_update_status_unknown_returns_none(self, db):
        repo = EscalationRepository(db)
        assert asyncio.run(repo.update_status("nope", "resolved")) is None

    def test_conversation_status_roundtrip(self, db):
        conv_repo = ConversationRepository(db)
        created = asyncio.run(conv_repo.get_or_create("tenant-1", "s1"))
        assert created["status"] == "active"
        assert asyncio.run(conv_repo.set_status(created["id"], "paused")) is True
        assert asyncio.run(conv_repo.set_status("missing", "paused")) is False


class TestApiHitlSmoke:
    """End-to-end: pause/resume gate blocks messages with 423, operator
    assign/resolve transitions on escalations."""

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
        settings.DATABASE_URL = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/hitl_api.db"
        settings.TENANT_CONFIG_PATH = str(tmp_path / "tenant_configs")
        settings.OPENAI_API_KEY = None
        settings.ANTHROPIC_API_KEY = None
        settings.GOOGLE_API_KEY = None
        settings.AZURE_API_KEY = None
        settings.CUSTOM_LLM_API_KEY = None

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
                "name": "HITL Smoke Co",
                "allowed_topics": ["general"],
                "blocked_topics": [],
                "default_provider": "openai",
                "default_model": "gpt-4",
                "escalation_threshold": 0.5,
            },
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["id"]

    def _conversation_id(self, tenant_id):
        from backend.app.infrastructure.db import get_database_manager

        async def _find():
            rows = await ConversationRepository(get_database_manager()).list_by_tenant(tenant_id)
            return rows[0]["id"]

        return asyncio.run(_find())

    def test_pause_resume_gate(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                tenant_id = self._provision_tenant(client, "pauseco")
                first = client.post(
                    "/api/v1/conversations",
                    json={"tenant_slug": "pauseco", "message": "hello"},
                )
                assert first.status_code == 200, first.text
                session_id = first.json()["session_id"]

                conv_id = self._conversation_id(tenant_id)
                paused = client.post(f"/api/v1/conversations/{conv_id}/pause")
                assert paused.status_code == 200, paused.text
                assert paused.json()["status"] == "paused"

                blocked = client.post(
                    "/api/v1/conversations",
                    json={"tenant_slug": "pauseco", "message": "hello again", "session_id": session_id},
                )
                assert blocked.status_code == 423, blocked.text

                resumed = client.post(f"/api/v1/conversations/{conv_id}/resume")
                assert resumed.status_code == 200
                assert resumed.json()["status"] == "active"

                ok = client.post(
                    "/api/v1/conversations",
                    json={"tenant_slug": "pauseco", "message": "hello again", "session_id": session_id},
                )
                assert ok.status_code == 200, ok.text
        finally:
            restore()

    def test_pause_unknown_conversation_404(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                resp = client.post(f"/api/v1/conversations/{uuid4()}/pause")
                assert resp.status_code == 404
        finally:
            restore()

    def test_escalation_operator_actions(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                tenant_id = self._provision_tenant(client, "opsco")
                from backend.app.infrastructure.db import get_database_manager

                async def _seed():
                    row = await EscalationRepository(get_database_manager()).add(
                        {
                            "id": str(uuid4()),
                            "tenant_id": tenant_id,
                            "conversation_id": "conv-1",
                            "session_id": "s1",
                            "category": "general",
                            "severity": "high",
                            "status": "pending",
                            "reason": "low_confidence",
                            "summary": "needs help",
                            "details": {"confidence": 0.2},
                            "channel": "generic",
                        }
                    )
                    return row["id"]

                escalation_id = asyncio.run(_seed())

                assigned = client.post(
                    f"/api/v1/escalations/{escalation_id}/assign",
                    json={"note": "assigning to tier-2"},
                )
                assert assigned.status_code == 200, assigned.text
                assert assigned.json()["status"] == "in_review"
                assert assigned.json()["details"]["assign_note"] == "assigning to tier-2"

                resolved = client.post(
                    f"/api/v1/escalations/{escalation_id}/resolve",
                    json={"note": "handled by operator"},
                )
                assert resolved.status_code == 200, resolved.text
                assert resolved.json()["status"] == "resolved"
                assert resolved.json()["details"]["resolution_note"] == "handled by operator"

                again = client.post(
                    f"/api/v1/escalations/{escalation_id}/resolve",
                    json={"note": "double resolve"},
                )
                assert again.status_code == 409

                missing = client.post(f"/api/v1/escalations/{uuid4()}/assign", json={})
                assert missing.status_code == 404
        finally:
            restore()
