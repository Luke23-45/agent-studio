"""
Tests for P0 deployment-blocker fixes:

- fail-closed guardrail configuration and classifier behavior
- real GuardrailsAI RAIL serialization (no more vacuous stubs)
- in-memory vector store (dev fallback) correctness
- DB-first tenant/policy hydration helpers
- LLM provider factory covering google/azure/custom
- session memory (history flows into the LLM prompt)
- end-to-end API smoke test (tenants + conversations)
"""

import asyncio
import pytest
from uuid import uuid4

from backend.app.domain.policy import PolicyAction, PolicySet
from backend.app.domain.tenant import TenantConfig
from backend.app.modules.guardrails.config import (
    GuardrailsModuleConfig,
    build_config_from_tenant,
)
from backend.app.modules.guardrails.errors import GuardrailConfigurationError
from backend.tests.gateway_fakes import FakeGateway


class TestGuardrailsConfigFailClosed:
    def test_nemo_enabled_without_config_path_raises(self):
        with pytest.raises(GuardrailConfigurationError):
            GuardrailsModuleConfig(enable_nemo_rails=True).validate()

    def test_nemo_disabled_without_config_path_is_valid(self):
        # Defaults must validate cleanly (nemo off by default)
        GuardrailsModuleConfig().validate()

    def test_nemo_enabled_with_config_path_is_valid(self):
        GuardrailsModuleConfig(
            enable_nemo_rails=True, nemo_config_path="config/nemo"
        ).validate()

    def test_build_config_from_tenant_wires_global_flags(self):
        from backend.app.settings.feature_flags import feature_flags

        tenant = TenantConfig(
            id=uuid4(),
            name="t",
            slug="t",
            guardrail_config={
                "regex_fastpath": True,
                "classifier": True,
                "nemo_rails": True,
                "jailbreak_detection": True,
                "output_validation": True,
                "pii_detection": True,
                "spotlighting": True,
            },
        )
        original = feature_flags.ENABLE_NEMO_GUARDRAILS
        try:
            object.__setattr__(feature_flags, "ENABLE_NEMO_GUARDRAILS", False)
            object.__setattr__(feature_flags, "ENABLE_PRESIDIO", False)
            cfg = build_config_from_tenant(tenant, nemo_config_path="config/nemo")
            # Global flag must gate the tenant-level enablement
            assert cfg.enable_nemo_rails is False
            assert cfg.enable_pii_redaction is False
            assert cfg.enable_jailbreak_scan is True
            assert cfg.enable_regex_fastpath is True
        finally:
            object.__setattr__(feature_flags, "ENABLE_NEMO_GUARDRAILS", original)
            object.__setattr__(feature_flags, "ENABLE_PRESIDIO", True)


class TestClassifierFailClosed:
    @pytest.mark.asyncio
    async def test_classifier_fails_closed_when_models_unavailable(self):
        from backend.app.modules.guardrails.classifier import ClassifierLayer
        from backend.app.domain.safety import ViolationSeverity

        layer = ClassifierLayer()
        await layer.initialize()
        # Models cannot load in this environment; the layer must degrade
        assert layer._jina_model is None

        tenant = TenantConfig(
            id=uuid4(), name="t", slug="t",
            allowed_topics=["billing"],
        )
        result = await layer.evaluate("Can I get a refund?", tenant)
        assert result.passed is False
        assert result.violations
        assert result.violations[0].severity == ViolationSeverity.HIGH

    @pytest.mark.asyncio
    async def test_classifier_passes_when_no_topic_restrictions(self):
        from backend.app.modules.guardrails.classifier import ClassifierLayer

        layer = ClassifierLayer()
        await layer.initialize()
        tenant = TenantConfig(id=uuid4(), name="t", slug="t")
        result = await layer.evaluate("anything goes", tenant)
        assert result.passed is True


class TestGuardrailsAIRailSerialization:
    def test_rail_string_contains_properties_and_required(self):
        from backend.app.modules.guardrails.guardrails_ai import (
            GuardrailsAIEngine,
            OutputSchema,
        )

        engine = GuardrailsAIEngine()
        schema = OutputSchema(
            name="reply",
            json_schema={
                "type": "object",
                "required": ["answer"],
                "properties": {
                    "answer": {"type": "string"},
                    "score": {"type": "number"},
                },
            },
        )
        rail = engine._build_rail_string(schema)
        assert 'name="answer" type="str" required="true"' in rail
        assert 'name="score" type="float"' in rail
        assert "<rail version=" in rail

    def test_rail_string_serializes_nested_objects_and_arrays(self):
        from backend.app.modules.guardrails.guardrails_ai import (
            GuardrailsAIEngine,
            OutputSchema,
        )

        engine = GuardrailsAIEngine()
        schema = OutputSchema(
            name="reply",
            json_schema={
                "type": "object",
                "properties": {
                    "meta": {
                        "type": "object",
                        "properties": {"id": {"type": "integer"}},
                    },
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
            },
        )
        rail = engine._build_rail_string(schema)
        assert '<object name="meta">' in rail
        assert 'name="id" type="int"' in rail
        assert '<list name="tags">' in rail
        assert '<element type="str"/>' in rail

    def test_rail_string_escapes_invalid_element_names(self):
        from backend.app.modules.guardrails.guardrails_ai import (
            GuardrailsAIEngine,
            OutputSchema,
        )

        engine = GuardrailsAIEngine()
        schema = OutputSchema(
            name="my schema!",
            json_schema={"type": "object", "properties": {}},
        )
        rail = engine._build_rail_string(schema)
        assert 'name="my_schema_"' in rail


class TestInMemoryVectorStore:
    @pytest.mark.asyncio
    async def test_add_search_filter_delete(self):
        from backend.app.adapters.vectorstore import (
            InMemoryVectorStore,
            VectorDocument,
            VectorSearchConfig,
        )

        store = InMemoryVectorStore()
        docs = [
            VectorDocument(
                id="a",
                content="billing help",
                embedding=[1.0, 0.0, 0.0],
                metadata={"tenant_id": "t1"},
            ),
            VectorDocument(
                id="b",
                content="sales help",
                embedding=[0.5, 0.5, 0.0],
                metadata={"tenant_id": "t1"},
            ),
            VectorDocument(
                id="c",
                content="other tenant",
                embedding=[1.0, 0.0, 0.0],
                metadata={"tenant_id": "t2"},
            ),
        ]
        assert await store.add_documents(docs) == ["a", "b", "c"]

        # Tenant filter isolates tenants
        config = VectorSearchConfig(
            top_k=5, score_threshold=0.0, filter_metadata={"tenant_id": "t1"}
        )
        results = await store.search([1.0, 0.0, 0.0], config)
        assert {r.document.id for r in results} == {"a", "b"}

        # Score ordering
        assert results[0].document.id == "a"
        assert results[0].score > results[1].score

        # Threshold filtering
        strict = VectorSearchConfig(
            top_k=5, score_threshold=0.95, filter_metadata={"tenant_id": "t1"}
        )
        assert {r.document.id for r in await store.search([1.0, 0.0, 0.0], strict)} == {"a"}

        await store.delete_documents(["a"])
        assert await store.get_document("a") is None
        assert len(store) == 2

    @pytest.mark.asyncio
    async def test_orthogonal_vectors_are_zero_similarity(self):
        from backend.app.adapters.vectorstore import (
            InMemoryVectorStore,
            VectorDocument,
            VectorSearchConfig,
        )

        store = InMemoryVectorStore()
        await store.add_documents(
            [VectorDocument(id="x", content="", embedding=[0.0, 1.0], metadata={})]
        )
        results = await store.search([1.0, 0.0], VectorSearchConfig(score_threshold=0.9))
        assert results == []


class TestTenantPolicyHydration:
    def test_tenant_config_from_data_merges_defaults(self):
        from backend.app.modules.tenant_config import tenant_config_from_data

        cfg = tenant_config_from_data(
            {"id": str(uuid4()), "name": "Acme", "slug": "acme"}
        )
        assert cfg.default_provider == "openai"
        # Defaults merged for guardrails added after creation
        assert cfg.guardrail_config["nemo_rails"] is False
        assert cfg.guardrail_config["jailbreak_detection"] is True

    def test_policy_set_from_db_restores_rules_and_priority(self):
        from backend.app.modules.tenant_config import policy_set_from_db

        row = {
            "id": str(uuid4()),
            "tenant_id": str(uuid4()),
            "name": "default",
            "version": 1,
            "rules": [
                {
                    "name": "block adult",
                    "policy_type": "topic_filter",
                    "action": "block",
                    "conditions": {"topic": "adult"},
                    "priority": 10,
                }
            ],
        }
        policy_set = policy_set_from_db(row)
        assert policy_set.version == 1
        assert policy_set.evaluate({"topic": "adult"}) == PolicyAction.BLOCK
        # Deny-by-default (Arch 2.5): an uncovered topic is blocked, not allowed.
        assert policy_set.evaluate({"topic": "billing"}) == PolicyAction.BLOCK


class TestLLMProviderFactory:
    def test_azure_google_custom_adapters_are_constructible(self):
        from backend.app.adapters.llm import (
            LLMConfig,
            LLMProviderType,
            create_llm_adapter,
        )

        for provider in (
            LLMProviderType.GOOGLE,
            LLMProviderType.CUSTOM,
            LLMProviderType.OPENAI,
            LLMProviderType.ANTHROPIC,
        ):
            adapter = create_llm_adapter(
                provider, "test-key", LLMConfig(model="test-model")
            )
            assert adapter.provider_type == provider

    def test_unknown_provider_raises(self):
        from backend.app.adapters.llm import LLMConfig, LLMProviderType, create_llm_adapter

        with pytest.raises(ValueError):
            create_llm_adapter("nope", "k", LLMConfig(model="m"))


class TestSessionMemory:
    @pytest.mark.asyncio
    async def test_history_included_in_llm_messages(self):
        from unittest.mock import AsyncMock, Mock

        from backend.app.application.orchestration import create_orchestration_service

        tenant = TenantConfig(
            id=uuid4(), name="t", slug="t",
            allowed_topics=["support"], escalation_threshold=0.5,
        )
        service = create_orchestration_service(
            tenant_config=tenant,
            policy_set=PolicySet(tenant_id=tenant.id, name="default"),
            gateway=FakeGateway(),
        )

        captured = {}

        async def fake_chat(messages):
            captured["messages"] = messages
            return Mock(
                content="I can help with billing.",
                model="fake",
                usage={},
                finish_reason="stop",
            )

        fake_adapter = Mock()
        fake_adapter.chat = fake_chat
        service.gateway = FakeGateway(chat_stub=fake_adapter)

        state = {
            "tenant_id": tenant.id,
            "session_id": "s1",
            "user_message": "second message",
            "redacted_message": "second message",
            "context": {},
            "conversation_history": [
                {"role": "user", "content": "first message"},
                {"role": "assistant", "content": "first answer"},
            ],
            "retrieved_docs": [],
            "model_response": None,
            "validation_result": {},
            "policy_action": PolicyAction.ALLOW,
            "confidence": 0.0,
            "handoff_required": False,
            "redact_attempts": 0,
            "error": None,
        }
        await service._generate_response(state)

        roles = [m.role for m in captured["messages"]]
        contents = [m.content for m in captured["messages"]]
        assert roles == ["system", "user", "assistant", "user"]
        assert "first message" in contents
        assert "first answer" in contents
        assert contents[-1] == "second message"
        assert state["model_response"] == "I can help with billing."


class TestApiSmoke:
    """End-to-end route tests against an isolated temp SQLite database.

    The app is booted through its real lifespan (TestClient), with settings
    pointed at a temp DB + temp tenant-config path so nothing under the repo
    is mutated. Schema is created with Base.metadata.create_all (the pattern
    used by test_persistence.py); migrations themselves are covered by the
    alembic suite.
    """

    def _isolated_app(self, tmp_path):
        import backend.app.api.routes.conversations as conversations_module
        from backend.app.settings import feature_flags as flags_module
        from backend.app.settings.env import settings

        original = {
            "auth": settings.AUTH_ENABLED,
            "presidio": flags_module.feature_flags.ENABLE_PRESIDIO,
            "db_url": settings.DATABASE_URL,
            "cfg_path": settings.TENANT_CONFIG_PATH,
            "openai_key": settings.OPENAI_API_KEY,
            "anthropic_key": settings.ANTHROPIC_API_KEY,
            "google_key": settings.GOOGLE_API_KEY,
            "azure_key": settings.AZURE_API_KEY,
            "custom_key": settings.CUSTOM_LLM_API_KEY,
        }

        def restore():
            settings.AUTH_ENABLED = original["auth"]
            settings.DATABASE_URL = original["db_url"]
            settings.TENANT_CONFIG_PATH = original["cfg_path"]
            settings.OPENAI_API_KEY = original["openai_key"]
            settings.ANTHROPIC_API_KEY = original["anthropic_key"]
            settings.GOOGLE_API_KEY = original["google_key"]
            settings.AZURE_API_KEY = original["azure_key"]
            settings.CUSTOM_LLM_API_KEY = original["custom_key"]
            object.__setattr__(
                flags_module.feature_flags, "ENABLE_PRESIDIO", original["presidio"]
            )
            conversations_module._rag_services.clear()

        settings.AUTH_ENABLED = False
        object.__setattr__(flags_module.feature_flags, "ENABLE_PRESIDIO", False)
        settings.DATABASE_URL = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/smoke.db"
        settings.TENANT_CONFIG_PATH = str(tmp_path / "tenant_configs")
        # Force the 503 provider-key path deterministically
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

    def test_health_endpoint(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                resp = client.get("/health")
                assert resp.status_code == 200
                # P1: health reports every component; in-memory fallbacks
                # (no Redis/S3 locally) surface as degraded, never "healthy"
                body = resp.json()
                assert body["status"] in ("healthy", "degraded")
                assert body["database"] == "healthy"
                assert body["components"]["database"]["status"] == "HEALTHY"
                assert {"queue", "cache", "storage", "guardrails", "langfuse"} <= set(
                    body["components"]
                )
        finally:
            restore()

    def test_tenant_create_and_conversation_flow(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                me = client.get("/api/v1/auth/me")
                assert me.status_code == 200
                assert me.json()["role"] == "super_admin"
                assert me.json()["tenant_id"] is None

                create = client.post(
                    "/api/v1/tenants",
                    json={"name": "Acme", "slug": "acme"},
                )
                assert create.status_code == 200
                tenant = create.json()
                assert tenant["slug"] == "acme"

                # No LLM key configured -> clean 503, not a crash
                conv = client.post(
                    "/api/v1/conversations",
                    json={"tenant_slug": "acme", "message": "Hello"},
                )
                assert conv.status_code == 503
                assert "No API key configured" in conv.json()["detail"]

                # Unknown tenant -> 404
                missing = client.post(
                    "/api/v1/conversations",
                    json={"tenant_slug": "nope", "message": "Hello"},
                )
                assert missing.status_code == 404
        finally:
            restore()
