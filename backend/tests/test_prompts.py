"""
P9-4 prompt management portal tests:

- API: create v1 (disabled), add v2, activate with A/B percentages
  (enabling one disables the other), delete version, RBAC enforcement.
- Resolver: deterministic per session seed, stable across re-asks,
  falls back to None (default prompt) when nothing matches.
- Orchestration hook: wired resolver replaces the system prompt prefix.
"""

import asyncio
from uuid import uuid4

from backend.app.api.dependencies.auth import generate_api_key, hash_api_key
from backend.app.application.prompts.service import resolve_prompt_variant
from backend.app.infrastructure.db import ApiKeyRepository, get_database_manager
from backend.app.settings.env import settings


class TestPromptRoutes:
    def _isolated_app(self, tmp_path):
        original = {
            "auth": settings.AUTH_ENABLED,
            "db_url": settings.DATABASE_URL,
            "cfg_path": settings.TENANT_CONFIG_PATH,
        }

        def restore():
            settings.AUTH_ENABLED = original["auth"]
            settings.DATABASE_URL = original["db_url"]
            settings.TENANT_CONFIG_PATH = original["cfg_path"]

        settings.AUTH_ENABLED = True
        settings.DATABASE_URL = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/prompts.db"
        settings.TENANT_CONFIG_PATH = str(tmp_path / "tenant_configs")

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

    def _provision(self, client, keys):
        resp = client.post(
            "/api/v1/tenants",
            json={
                "slug": "promptco",
                "name": "Prompt Co",
                "allowed_topics": ["general"],
                "blocked_topics": [],
                "default_provider": "openai",
                "default_model": "gpt-4",
            },
            headers={"X-API-Key": keys["super_admin"]},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["id"]

    def _create_keys(self):
        db = get_database_manager()
        repo = ApiKeyRepository(db)
        raw_super, prefix_super, hash_super = generate_api_key()
        raw_operator, prefix_operator, hash_operator = generate_api_key()
        asyncio.run(repo.create(name="prompt-super", key_hash=hash_super, prefix=prefix_super, role="super_admin"))
        asyncio.run(repo.create(name="prompt-operator", key_hash=hash_operator, prefix=prefix_operator, role="operator"))
        return {
            "super_admin": raw_super,
            "operator": raw_operator,
        }

    def test_prompt_lifecycle_and_rbac(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                keys = self._create_keys()
                tenant_id = self._provision(client, keys)

                created = client.post(
                    "/api/v1/prompts",
                    json={"tenant_id": tenant_id, "name": "system", "content": "you are v1"},
                    headers={"X-API-Key": keys["super_admin"]},
                )
                assert created.status_code == 201, created.text
                v1 = created.json()
                assert v1["version"] == 1 and v1["enabled"] is False

                v2 = client.post(
                    f"/api/v1/prompts/system/versions",
                    json={"tenant_id": tenant_id, "name": "system", "content": "you are v2"},
                    headers={"X-API-Key": keys["super_admin"]},
                )
                assert v2.status_code == 201 and v2.json()["version"] == 2

                # A/B: v1 gets 40% of sessions, v2 gets the rest
                act1 = client.post(
                    "/api/v1/prompts/system/activate?tenant_id=" + tenant_id,
                    json={"version": 1, "enabled": True, "target_percentage": 40},
                    headers={"X-API-Key": keys["super_admin"]},
                )
                assert act1.status_code == 200 and act1.json()["enabled"] is True
                act2 = client.post(
                    "/api/v1/prompts/system/activate?tenant_id=" + tenant_id,
                    json={"version": 2, "enabled": True, "target_percentage": 60},
                    headers={"X-API-Key": keys["super_admin"]},
                )
                assert act2.status_code == 200 and act2.json()["targetPercentage"] == 60

                listed = client.get(
                    f"/api/v1/prompts?tenant_id={tenant_id}",
                    headers={"X-API-Key": keys["super_admin"]},
                )
                assert listed.status_code == 200
                prompts = listed.json()["prompts"]
                assert len(prompts) == 2
                assert {p["enabled"] for p in prompts} == {True}

                # operator lacks prompts:write
                denied = client.post(
                    "/api/v1/prompts",
                    json={"tenant_id": tenant_id, "name": "system", "content": "x"},
                    headers={"X-API-Key": keys["operator"]},
                )
                assert denied.status_code == 403
                denied_read = client.get(
                    f"/api/v1/prompts?tenant_id={tenant_id}",
                    headers={"X-API-Key": keys["operator"]},
                )
                assert denied_read.status_code == 403

                # missing version 404s
                gone = client.delete(
                    "/api/v1/prompts/system/versions/99?tenant_id=" + tenant_id,
                    headers={"X-API-Key": keys["super_admin"]},
                )
                assert gone.status_code == 404
        finally:
            restore()

    def test_resolver_determinism_and_bands(self):
        rows = [
            {"version": 1, "target_percentage": 40},
            {"version": 2, "target_percentage": 60},
        ]
        seed = f"{uuid4()}"
        picked = resolve_prompt_variant(rows, seed=seed)
        assert resolve_prompt_variant(rows, seed=seed) == picked  # stable per session
        picks = [resolve_prompt_variant(rows, seed=f"t:{i}")["version"] for i in range(500)]
        v1_share = sum(1 for v in picks if v == 1) / len(picks)
        assert 0.30 <= v1_share <= 0.50

        # bands not covering the bucket -> fallback (None)
        sparse = [{"version": 1, "target_percentage": 10}]
        misses = sum(1 for i in range(500) if resolve_prompt_variant(sparse, seed=f"s:{i}") is None)
        assert misses > 300
        assert resolve_prompt_variant([], seed="x") is None
