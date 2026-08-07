"""
P7-5 — P6-7 redacted-corpus replay through the harness (AGENTS.md boundary).

- POST /harness/replay-corpus replays inline redacted cases through the
  production gateway (pinned) on the internal slot, preserving a
  fork_replay session
- with no providers configured the gateway fails per case and the replay
  still completes with the run preserved (failure is per-case, not fatal)
- operator role is required; non-operator roles are rejected with 403
- bad payloads are rejected (both storage_key and cases, or neither)
"""

import asyncio

from backend.app.api.dependencies.auth import generate_api_key, hash_api_key
from backend.app.gateway.service import get_gateway, init_gateway
from backend.app.infrastructure.db import ApiKeyRepository, get_database_manager
from backend.app.settings.env import settings


class TestHarnessCorpusReplay:
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
        settings.DATABASE_URL = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/harness.db"
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

        init_gateway(db=get_database_manager())
        from backend.app.infrastructure.storage import init_storage

        init_storage()

        from starlette.testclient import TestClient

        from backend.app.main import app

        return TestClient(app), restore

    def _create_key(self, role: str) -> str:
        db = get_database_manager()
        raw, prefix, key_hash = generate_api_key()
        asyncio.run(
            ApiKeyRepository(db).create(
                name=f"harness-{role}",
                key_hash=key_hash,
                prefix=prefix,
                role=role,
            )
        )
        return raw

    def test_replay_inline_cases_fails_gracefully_per_case(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                key = self._create_key("super_admin")
                headers = {"X-API-Key": key}

                resp = client.post(
                    "/api/v1/harness/replay-corpus",
                    json={
                        "provider": "openai",
                        "model": "gpt-4",
                        "cases": [
                            {"id": "c1", "thread_id": "t1", "user_message": "Hello"},
                            {"id": "c2", "user_message": "Refund policy?"},
                        ],
                    },
                    headers=headers,
                )
                assert resp.status_code == 200, resp.text
                body = resp.json()

                summary = body["summary"]
                assert summary["replayed"] == 2
                assert summary["succeeded"] + summary["failed"] == 2
                assert summary["avg_latency_ms"] >= 0

                results = body["results"]
                assert len(results) == 2
                assert {r["caseId"] for r in results} == {"c1", "c2"}
                assert all(r["userMessage"] for r in results)

                session = body["session"]
                assert session["kind"] == "fork_replay"
                assert session["provider"] == "openai"
                assert session["model"] == "gpt-4"
                assert session["status"] == "active"
                # Two bubbles per case (user + assistant/error).
                assert len(session["messages"]) == 4
        finally:
            restore()

    def test_replay_is_operator_only(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                key = self._create_key("auditor")
                denied = client.post(
                    "/api/v1/harness/replay-corpus",
                    json={
                        "provider": "openai",
                        "model": "gpt-4",
                        "cases": [{"user_message": "Hello"}],
                    },
                    headers={"X-API-Key": key},
                )
                assert denied.status_code == 403, denied.text

                operator = self._create_key("operator")
                allowed = client.post(
                    "/api/v1/harness/replay-corpus",
                    json={
                        "provider": "openai",
                        "model": "gpt-4",
                        "cases": [{"user_message": "Hello"}],
                    },
                    headers={"X-API-Key": operator},
                )
                assert allowed.status_code == 200, allowed.text
        finally:
            restore()

    def test_replay_rejects_bad_payloads(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                key = self._create_key("operator")
                headers = {"X-API-Key": key}
                base = {"provider": "openai", "model": "gpt-4"}

                neither = client.post(
                    "/api/v1/harness/replay-corpus", json=base, headers=headers
                )
                assert neither.status_code == 400, neither.text

                both = client.post(
                    "/api/v1/harness/replay-corpus",
                    json={
                        **base,
                        "storage_key": "eval_corpora/x.jsonl",
                        "cases": [{"user_message": "Hello"}],
                    },
                    headers=headers,
                )
                assert both.status_code == 400, both.text

                empty_cases = client.post(
                    "/api/v1/harness/replay-corpus",
                    json={**base, "cases": []},
                    headers=headers,
                )
                assert empty_cases.status_code == 400, empty_cases.text

                bad_namespace = client.post(
                    "/api/v1/harness/replay-corpus",
                    json={**base, "storage_key": "private/x.jsonl"},
                    headers=headers,
                )
                assert bad_namespace.status_code == 400, bad_namespace.text

                missing_corpus = client.post(
                    "/api/v1/harness/replay-corpus",
                    json={**base, "storage_key": "eval_corpora/nope.jsonl"},
                    headers=headers,
                )
                assert missing_corpus.status_code == 404, missing_corpus.text
        finally:
            restore()
