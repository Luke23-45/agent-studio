"""
API smoke tests for the worker trigger routes (P1 worker wiring):

- POST /tenants/{tenant_id}/documents  -> enqueue ingestion.process (202)
- POST /eval/replay                    -> enqueue eval.replay (202)
- POST /retention/run                  -> enqueue cleanup.run (202)

RBAC: knowledge:write (super_admin, tenant_admin), evals:run (super_admin,
tenant_admin), tenants:write (super_admin). Operator gets 403 on all.
"""

import asyncio
from uuid import uuid4

from backend.app.api.dependencies.auth import generate_api_key, hash_api_key
from backend.app.infrastructure.db import ApiKeyRepository, get_database_manager
from backend.app.settings.env import settings


class TestOperationRoutes:
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
        settings.DATABASE_URL = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/ops.db"
        settings.TENANT_CONFIG_PATH = str(tmp_path / "tenant_configs")
        settings.REDIS_URL = "redis://127.0.0.1:1"  # unreachable -> in-memory fallback

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
        tenant_resp = client.post(
            "/api/v1/tenants",
            json={
                "slug": "opsco",
                "name": "Ops Co",
                "allowed_topics": ["general"],
                "blocked_topics": [],
                "default_provider": "openai",
                "default_model": "gpt-4",
            },
            headers={"X-API-Key": keys["super_admin"]},
        )
        assert tenant_resp.status_code == 200, tenant_resp.text
        return tenant_resp.json()["id"]

    def _create_keys(self):
        db = get_database_manager()
        repo = ApiKeyRepository(db)
        raw_super, prefix_super, hash_super = generate_api_key()
        raw_operator, prefix_operator, hash_operator = generate_api_key()
        asyncio.run(repo.create(name="smoke-super", key_hash=hash_super, prefix=prefix_super, role="super_admin"))
        asyncio.run(repo.create(name="smoke-operator", key_hash=hash_operator, prefix=prefix_operator, role="operator"))
        return {"super_admin": raw_super, "operator": raw_operator}

    def test_ingest_document_202_and_rbac(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                keys = self._create_keys()
                tenant_id = self._provision(client, keys)
                headers = {"X-API-Key": keys["super_admin"]}

                resp = client.post(
                    f"/api/v1/tenants/{tenant_id}/documents",
                    json={"content": "Refund policy: 30 days. " * 50, "source": "refunds.md"},
                    headers=headers,
                )
                assert resp.status_code == 202, resp.text
                body = resp.json()
                assert body["accepted"] is True
                assert body["job_type"] == "ingestion.process"
                assert body["idempotency_key"]

                from backend.app.infrastructure.queue import get_queue_manager

                assert asyncio.run(get_queue_manager().get_queue_length()) == 1

                # duplicate source -> idempotent, no second job
                client.post(
                    f"/api/v1/tenants/{tenant_id}/documents",
                    json={"content": "Refund policy: 30 days. " * 50, "source": "refunds.md"},
                    headers=headers,
                )
                assert asyncio.run(get_queue_manager().get_queue_length()) == 1

                # operator lacks knowledge:write
                denied = client.post(
                    f"/api/v1/tenants/{tenant_id}/documents",
                    json={"content": "x", "source": "a.md"},
                    headers={"X-API-Key": keys["operator"]},
                )
                assert denied.status_code == 403

                # empty content rejected
                invalid = client.post(
                    f"/api/v1/tenants/{tenant_id}/documents",
                    json={"content": "", "source": "a.md"},
                    headers=headers,
                )
                assert invalid.status_code == 422
        finally:
            restore()

    def test_eval_replay_202_404_rbac(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                keys = self._create_keys()
                tenant_id = self._provision(client, keys)
                headers = {"X-API-Key": keys["super_admin"]}

                resp = client.post(
                    "/api/v1/eval/replay",
                    json={"tenant_id": tenant_id, "session_id": "sess-1"},
                    headers=headers,
                )
                assert resp.status_code == 202, resp.text
                assert resp.json()["job_type"] == "eval.replay"

                missing = client.post(
                    "/api/v1/eval/replay",
                    json={"tenant_id": str(uuid4()), "session_id": "sess-2"},
                    headers=headers,
                )
                assert missing.status_code == 404

                denied = client.post(
                    "/api/v1/eval/replay",
                    json={"tenant_id": tenant_id, "session_id": "sess-3"},
                    headers={"X-API-Key": keys["operator"]},
                )
                assert denied.status_code == 403
        finally:
            restore()

    def test_retention_run_202_and_rbac(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                keys = self._create_keys()
                tenant_id = self._provision(client, keys)
                headers = {"X-API-Key": keys["super_admin"]}

                resp = client.post(
                    "/api/v1/retention/run",
                    json={
                        "tenant_id": tenant_id,
                        "older_than_seconds": 2592000,
                        "requeue_dead_letter": True,
                    },
                    headers=headers,
                )
                assert resp.status_code == 202, resp.text
                assert resp.json()["job_type"] == "cleanup.run"

                denied = client.post(
                    "/api/v1/retention/run",
                    json={"tenant_id": tenant_id, "older_than_seconds": 3600},
                    headers={"X-API-Key": keys["operator"]},
                )
                assert denied.status_code == 403
        finally:
            restore()
