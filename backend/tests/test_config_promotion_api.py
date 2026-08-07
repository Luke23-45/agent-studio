"""
P5-2 config promotion pipeline — HTTP acceptance (Arch 12, P0-11).

Exercises the versioned config endpoints end-to-end on an isolated app:

- draft -> publish changes a version's status; publish is gated on the
  eval suite (a failing suite => 422)
- canary rollout (:PERCENT), and auto-rollback on regression re-publishes
  the prior version while blocking re-promotion of the regressed one
- out-of-range canary_percent is rejected
"""

import asyncio
import json

import pytest

from backend.app.api.dependencies.auth import generate_api_key, hash_api_key
from backend.app.infrastructure.db import ApiKeyRepository, get_database_manager
from backend.app.settings.env import settings


def _config_payload(tenant_id: str, slug: str, model: str = "gpt-4") -> dict:
    return {
        "id": str(tenant_id),
        "name": "Isolation Co",
        "slug": slug,
        "allowed_topics": ["general"],
        "blocked_topics": [],
        "default_provider": "openai",
        "default_model": model,
    }


class TestConfigPromotionApi:
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
        settings.DATABASE_URL = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/promo.db"
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

    def _create_key(self, role: str = "super_admin", tenant_id=None) -> str:
        db = get_database_manager()
        raw, prefix, key_hash = generate_api_key()
        asyncio.run(
            ApiKeyRepository(db).create(
                name=f"promo-{role}",
                key_hash=key_hash,
                prefix=prefix,
                role=role,
                tenant_id=tenant_id,
            )
        )
        return raw

    def test_config_promotion_pipeline(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                key = self._create_key()
                headers = {"X-API-Key": key}
                tenant = client.post(
                    "/api/v1/tenants",
                    json={
                        "slug": "promoco",
                        "name": "Isolation Co",
                        "allowed_topics": ["general"],
                        "blocked_topics": [],
                        "default_provider": "openai",
                        "default_model": "gpt-4",
                    },
                    headers=headers,
                )
                assert tenant.status_code == 200, tenant.text
                tenant_id = tenant.json()["id"]
                model_slow = "gpt-4"

                v1 = client.post(
                    f"/api/v1/tenants/{tenant_id}/config-versions",
                    json={"config": _config_payload(tenant_id, "promoco", model_slow)},
                    headers=headers,
                )
                assert v1.status_code == 200, v1.text
                version1 = v1.json()["version"]
                assert v1.json()["validation_status"] == "validated"

                pub = client.post(
                    f"/api/v1/tenants/{tenant_id}/config-versions/{version1}/publish",
                    headers=headers,
                )
                assert pub.status_code == 200, pub.text
                assert pub.json()["status"] == "published"

                # A draft whose eval suite fails can never be promoted.
                v2 = client.post(
                    f"/api/v1/tenants/{tenant_id}/config-versions",
                    json={"config": _config_payload(tenant_id, "promoco", "gpt-4o")},
                    headers=headers,
                )
                version2 = v2.json()["version"]
                failed = client.post(
                    f"/api/v1/tenants/{tenant_id}/config-versions/{version2}/evaluate",
                    json={"suite": "config", "passed": False, "details": {"accuracy": 0.3}},
                    headers=headers,
                )
                assert failed.status_code == 200, failed.text
                assert failed.json()["eval_status"] == "failed"
                blocked = client.post(
                    f"/api/v1/tenants/{tenant_id}/config-versions/{version2}/publish",
                    headers=headers,
                )
                assert blocked.status_code == 422, blocked.text

                passed = client.post(
                    f"/api/v1/tenants/{tenant_id}/config-versions/{version2}/evaluate",
                    json={"suite": "config", "passed": True, "details": {"accuracy": 0.9}},
                    headers=headers,
                )
                assert passed.json()["eval_status"] == "passed"
                canary = client.post(
                    f"/api/v1/tenants/{tenant_id}/config-versions/{version2}/publish",
                    params={"canary_percent": 30},
                    headers=headers,
                )
                assert canary.status_code == 200, canary.text
                assert canary.json()["canary_percent"] == 30

                # Auto-rollback on regression -> back to v1, v2 regressed.
                rolled = client.post(
                    f"/api/v1/tenants/{tenant_id}/config-versions/{version2}/auto-rollback",
                    params={"reason": "error rate > 5%"},
                    headers=headers,
                )
                assert rolled.status_code == 200, rolled.text
                assert rolled.json()["version"] == version1
                republish = client.post(
                    f"/api/v1/tenants/{tenant_id}/config-versions/{version2}/publish",
                    headers=headers,
                )
                assert republish.status_code == 422, republish.text
        finally:
            restore()

    def test_config_canary_percent_out_of_range_rejected(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                key = self._create_key()
                headers = {"X-API-Key": key}
                tenant = client.post(
                    "/api/v1/tenants",
                    json={
                        "slug": "promoco2",
                        "name": "Isolation Co",
                        "allowed_topics": [],
                        "blocked_topics": [],
                        "default_provider": "openai",
                        "default_model": "gpt-4",
                    },
                    headers=headers,
                )
                tenant_id = tenant.json()["id"]
                v1 = client.post(
                    f"/api/v1/tenants/{tenant_id}/config-versions",
                    json={"config": _config_payload(tenant_id, "promoco2")},
                    headers=headers,
                ).json()["version"]
                bad = client.post(
                    f"/api/v1/tenants/{tenant_id}/config-versions/{v1}/publish",
                    params={"canary_percent": 250},
                    headers=headers,
                )
                assert bad.status_code == 422, bad.text
        finally:
            restore()