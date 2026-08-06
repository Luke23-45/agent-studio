"""
Tests for the per-tenant model catalog (matrix 5.2): allowlist, fallback
order, cost ceilings, and the catalog management API.
"""

import asyncio
from uuid import uuid4

import pytest

from backend.app.infrastructure.db import ModelCatalogRepository, TenantRepository
from backend.app.infrastructure.db.manager import DatabaseConfig, DatabaseManager
from backend.app.infrastructure.db.models import Base
from backend.app.modules.model_catalog import (
    CostCeilingExceeded,
    ModelCatalogService,
    ModelNotAllowed,
    estimate_tokens,
)


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(
        DatabaseConfig(database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/catalog.db")
    )

    async def _init():
        await manager.initialize()
        async with manager._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    yield manager
    asyncio.run(manager.close())


@pytest.fixture(autouse=True)
def _cache():
    from backend.app.infrastructure.cache import init_cache

    async def _init():
        await init_cache().initialize()

    asyncio.run(_init())
    yield


def _new_tenant_id(db) -> str:
    async def _create():
        tenant = await TenantRepository(db).create(
            {
                "id": str(uuid4()),
                "slug": f"catalog-{uuid4().hex[:8]}",
                "name": "Catalog Co",
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
        return tenant["id"]

    return asyncio.run(_create())


class TestCatalogService:
    def test_add_and_list_sorted(self, db):
        tenant_id = _new_tenant_id(db)
        service = ModelCatalogService(db=db)
        asyncio.run(
            service.add_entry(
                tenant_id, "openai", "gpt-4o-mini",
                cost_ceiling_per_1k=0.1, fallback_order=2,
            )
        )
        asyncio.run(
            service.add_entry(
                tenant_id, "openai", "gpt-4",
                cost_ceiling_per_1k=0.3, fallback_order=1,
            )
        )
        rows = asyncio.run(service.list_catalog(tenant_id))
        assert len(rows) == 2
        assert [r["model"] for r in rows] == ["gpt-4", "gpt-4o-mini"]
        assert rows[0]["enabled"] is True

    def test_resolve_allowlisted_model(self, db):
        tenant_id = _new_tenant_id(db)
        service = ModelCatalogService(db=db)
        asyncio.run(service.add_entry(tenant_id, "openai", "gpt-4", fallback_order=1))
        provider, model = asyncio.run(
            service.resolve_model(tenant_id, "openai", "gpt-4")
        )
        assert (provider, model) == ("openai", "gpt-4")

    def test_resolve_falls_back(self, db):
        tenant_id = _new_tenant_id(db)
        service = ModelCatalogService(db=db)
        asyncio.run(service.add_entry(tenant_id, "openai", "gpt-4o-mini", fallback_order=0))
        provider, model = asyncio.run(
            service.resolve_model(tenant_id, "openai", "gpt-4")
        )
        assert (provider, model) == ("openai", "gpt-4o-mini")

    def test_resolve_denied_when_catalog_nonempty_but_disabled(self, db):
        tenant_id = _new_tenant_id(db)
        service = ModelCatalogService(db=db)
        entry = asyncio.run(
            service.add_entry(tenant_id, "openai", "gpt-4o-mini", fallback_order=0)
        )
        asyncio.run(service.set_enabled(entry["id"], tenant_id, False))
        with pytest.raises(ModelNotAllowed):
            asyncio.run(service.resolve_model(tenant_id, "openai", "gpt-4"))

    def test_empty_catalog_unconstrained(self, db):
        tenant_id = _new_tenant_id(db)
        service = ModelCatalogService(db=db)
        provider, model = asyncio.run(
            service.resolve_model(tenant_id, "openai", "gpt-4")
        )
        assert (provider, model) == ("openai", "gpt-4")

    def test_remove_entry(self, db):
        tenant_id = _new_tenant_id(db)
        service = ModelCatalogService(db=db)
        entry = asyncio.run(service.add_entry(tenant_id, "openai", "gpt-4"))
        assert asyncio.run(service.remove_entry(entry["id"], tenant_id)) is True
        assert asyncio.run(service.list_catalog(tenant_id)) == []
        assert asyncio.run(service.remove_entry(entry["id"], tenant_id)) is False

    def test_duplicate_entry_rejected(self, db):
        tenant_id = _new_tenant_id(db)
        service = ModelCatalogService(db=db)
        asyncio.run(service.add_entry(tenant_id, "openai", "gpt-4"))
        with pytest.raises(Exception):
            asyncio.run(service.add_entry(tenant_id, "openai", "gpt-4"))


class TestCostCeiling:
    def test_within_budget_passes(self, db):
        tenant_id = _new_tenant_id(db)
        service = ModelCatalogService(db=db)
        asyncio.run(
            service.add_entry(tenant_id, "openai", "gpt-4", cost_ceiling_per_1k=1.0)
        )
        # 500 tokens -> $0.50 of the $1.00 per-1k ceiling
        asyncio.run(
            service.check_cost_ceiling(tenant_id, "openai", "gpt-4", estimated_tokens=500)
        )

    def test_exceeds_ceiling_raises(self, db):
        tenant_id = _new_tenant_id(db)
        service = ModelCatalogService(db=db)
        asyncio.run(
            service.add_entry(tenant_id, "openai", "gpt-4", cost_ceiling_per_1k=0.05)
        )
        with pytest.raises(CostCeilingExceeded):
            asyncio.run(
                service.check_cost_ceiling(tenant_id, "openai", "gpt-4", estimated_tokens=4000)
            )

    def test_rolling_window_allows_budget_across_calls(self, db):
        tenant_id = _new_tenant_id(db)
        service = ModelCatalogService(db=db)
        asyncio.run(
            service.add_entry(tenant_id, "openai", "gpt-4", cost_ceiling_per_1k=0.05)
        )
        # 0.002 per call -> 25 calls = 0.05 (at ceiling, not over)
        for _ in range(25):
            asyncio.run(
                service.check_cost_ceiling(tenant_id, "openai", "gpt-4", estimated_tokens=40)
            )
        with pytest.raises(CostCeilingExceeded):
            asyncio.run(
                service.check_cost_ceiling(tenant_id, "openai", "gpt-4", estimated_tokens=40)
            )

    def test_zero_ceiling_unlimited(self, db):
        tenant_id = _new_tenant_id(db)
        service = ModelCatalogService(db=db)
        asyncio.run(service.add_entry(tenant_id, "openai", "gpt-4"))
        asyncio.run(
            service.check_cost_ceiling(tenant_id, "openai", "gpt-4", estimated_tokens=10_000_000)
        )


class TestEstimateTokens:
    def test_chars_over_four(self):
        assert estimate_tokens("a" * 400) == 100
        assert estimate_tokens("a" * 399) == 99

    def test_minimum_one(self):
        assert estimate_tokens("") == 1
        assert estimate_tokens("", "") == 1

    def test_multi_text(self):
        assert estimate_tokens("a" * 100, "b" * 300) == 100


class TestApiCatalogSmoke:
    """Catalog CRUD through the API + fallback enforcement in conversation POST."""

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
        settings.DATABASE_URL = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/catalog_api.db"
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
                "name": "Catalog Smoke Co",
                "allowed_topics": ["general"],
                "blocked_topics": [],
                "default_provider": "openai",
                "default_model": "gpt-4",
                "escalation_threshold": 0.5,
            },
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["id"]

    def test_catalog_crud_and_fallback(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                tenant_id = self._provision_tenant(client, "catco")

                assert client.get(f"/api/v1/tenants/{tenant_id}/models").json() == []

                created = client.post(
                    f"/api/v1/tenants/{tenant_id}/models",
                    json={
                        "tenant_id": tenant_id,
                        "provider": "openai",
                        "model": "gpt-4o-mini",
                        "cost_ceiling_per_1k": 0.1,
                        "fallback_order": 0,
                    },
                )
                assert created.status_code == 201, created.text
                entry = created.json()
                assert entry["model"] == "gpt-4o-mini"

                rows = client.get(f"/api/v1/tenants/{tenant_id}/models").json()
                assert len(rows) == 1

                # Tenant default (gpt-4) is not in the catalog -> falls back.
                resp = client.post(
                    "/api/v1/conversations",
                    json={"tenant_slug": "catco", "message": "hello"},
                )
                assert resp.status_code == 200, resp.text

                deleted = client.delete(f"/api/v1/tenants/{tenant_id}/models/{entry['id']}")
                assert deleted.status_code == 204
                assert client.get(f"/api/v1/tenants/{tenant_id}/models").json() == []
        finally:
            restore()

    def test_cost_ceiling_429_mapping(self, tmp_path, monkeypatch):
        from fastapi import HTTPException

        from backend.app.api.routes.conversations import _resolve_catalog_model
        from backend.app.domain.tenant import TenantConfig
        from backend.app.modules.model_catalog import CostCeilingExceeded

        class _FakeService:
            @staticmethod
            def estimate_tokens(*_texts):
                return 100

            async def resolve_model(self, *_):
                return "openai", "gpt-4"

            async def check_cost_ceiling(self, *_a, **_k):
                raise CostCeilingExceeded("openai", "gpt-4", 0.1, 0.2)

        monkeypatch.setattr(
            "backend.app.modules.model_catalog.ModelCatalogService", _FakeService
        )
        config = TenantConfig(id=uuid4(), default_provider="openai", default_model="gpt-4")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_resolve_catalog_model(config, "hello"))
        assert exc.value.status_code == 429

    def test_model_not_allowed_maps_to_403(self, monkeypatch):
        from fastapi import HTTPException

        from backend.app.api.routes.conversations import _resolve_catalog_model
        from backend.app.domain.tenant import TenantConfig
        from backend.app.modules.model_catalog import ModelNotAllowed

        class _FakeService:
            @staticmethod
            def estimate_tokens(*_texts):
                return 100

            async def resolve_model(self, *_):
                raise ModelNotAllowed("openai", "gpt-4", "t1")

            async def check_cost_ceiling(self, *_a, **_k):
                return None

        monkeypatch.setattr(
            "backend.app.modules.model_catalog.ModelCatalogService", _FakeService
        )
        config = TenantConfig(id=uuid4(), default_provider="openai", default_model="gpt-4")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_resolve_catalog_model(config, "hello"))
        assert exc.value.status_code == 403

    def test_override_returned_only_when_different(self, monkeypatch):
        from backend.app.api.routes.conversations import _resolve_catalog_model
        from backend.app.domain.tenant import TenantConfig

        class _FakeService:
            @staticmethod
            def estimate_tokens(*_texts):
                return 100

            async def resolve_model(self, *_):
                return "openai", "gpt-4o-mini"

            async def check_cost_ceiling(self, *_a, **_k):
                return None

        monkeypatch.setattr(
            "backend.app.modules.model_catalog.ModelCatalogService", _FakeService
        )
        config = TenantConfig(id=uuid4(), default_provider="openai", default_model="gpt-4")
        _, override = asyncio.run(_resolve_catalog_model(config, "hello"))
        assert override == "gpt-4o-mini"
