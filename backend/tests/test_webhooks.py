"""
Tests for webhook subscriptions (matrix 1.7): signing, delivery semantics,
durable events + replay, and Idempotency-Key on the conversation write.

Covered:
- HMAC signature header generation + verification (tolerance window)
- Deliverer: 2xx success, 4xx permanent failure (no retry), 5xx transient
- Publisher: persists event, enqueues one idempotent job per subscription
- Worker handler delivers a signed payload end-to-end (in-memory queue)
- POST /conversations with Idempotency-Key replays the stored response
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.adapters.llm import LLMProviderType
from backend.app.application.orchestration import create_orchestration_service
from backend.app.domain.policy import PolicySet
from backend.app.domain.tenant import TenantConfig
from backend.app.modules.webhooks import (
    WebhookDeliverer,
    WebhookEnvelope,
    sign_payload,
    signature_header,
    verify_signature,
)


class _FakeAdapter:
    provider_type = LLMProviderType.OPENAI

    def __init__(self):
        self.config = SimpleNamespace(model="gpt-4")

    async def chat(self, messages):
        return SimpleNamespace(content="A complete and useful answer.")


class TestSigner:
    def test_signature_header_roundtrip(self):
        secret = "super-secret-0123456789"
        body = b'{"event_id": "abc"}'
        header = signature_header(secret, body)
        assert header.startswith("t=")
        assert ",v1=" in header
        assert verify_signature(secret, body, header)

    def test_wrong_secret_fails(self):
        secret = "super-secret-0123456789"
        body = b"payload"
        header = signature_header(secret, body)
        assert not verify_signature("other-secret-9876543210", body, header)

    def test_tampered_body_fails(self):
        secret = "super-secret-0123456789"
        header = signature_header(secret, b"original")
        assert not verify_signature(secret, b"tampered", header)

    def test_stale_timestamp_rejected(self):
        secret = "super-secret-0123456789"
        body = b"payload"
        ts, digest = sign_payload(secret, body, timestamp=1000)
        header = f"t={ts},v1={digest}"
        assert not verify_signature(secret, body, header, now=2000)

    def test_malformed_header_rejected(self):
        assert not verify_signature("secret", b"x", "garbage")
        assert not verify_signature("secret", b"x", "t=abc,v1=def")


class TestDeliverer:
    @pytest.fixture
    def envelope(self):
        return WebhookEnvelope(
            event_id="evt-1", event_type="conversation.completed",
            tenant_id="tenant-1", data={"session_id": "s1"},
        )

    async def test_delivers_with_signed_headers(self, envelope, monkeypatch):
        captured = {}

        class _FakeResponse:
            status_code = 200

        async def fake_post(self, url, content=None, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = content
            return _FakeResponse()

        monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
        result = await WebhookDeliverer().deliver("https://hook.example/x", "secret-1234567890", envelope)
        assert result.success
        assert captured["headers"]["X-Neryva-Event-Id"] == "evt-1"
        assert captured["headers"]["X-Neryva-Event-Type"] == "conversation.completed"
        assert verify_signature("secret-1234567890", captured["body"], captured["headers"]["X-Neryva-Signature"])

    async def test_4xx_is_permanent_failure(self, envelope, monkeypatch):
        calls = []

        class _FakeResponse:
            status_code = 422

        async def fake_post(self, url, content=None, headers=None):
            calls.append(1)
            return _FakeResponse()

        monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
        result = await WebhookDeliverer().deliver("https://hook.example/x", "secret-1234567890", envelope)
        assert not result.success
        assert result.http_status == 422
        assert "subscriber rejected" in result.error

    async def test_5xx_is_transient_failure(self, envelope, monkeypatch):
        class _FakeResponse:
            status_code = 503

        async def fake_post(self, url, content=None, headers=None):
            return _FakeResponse()

        monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
        result = await WebhookDeliverer().deliver("https://hook.example/x", "secret-1234567890", envelope)
        assert not result.success
        assert "transient failure" in result.error

    async def test_network_error_is_failure(self, envelope, monkeypatch):
        async def fake_post(self, url, content=None, headers=None):
            raise TimeoutError("connection timed out")

        monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
        result = await WebhookDeliverer().deliver("https://hook.example/x", "secret-1234567890", envelope)
        assert not result.success
        assert "timed out" in result.error


class TestPublisher:
    def _service(self, repository=None, queue=None):
        from backend.app.modules.webhooks.publisher import WebhookPublisher

        return WebhookPublisher(db=SimpleNamespace(), repository=repository)

    async def test_publish_with_no_subscription_returns_none(self):
        from backend.app.modules.webhooks.publisher import WebhookPublisher

        class _Repo:
            async def list_subscriptions(self, tenant_id):
                return []

        publisher = WebhookPublisher(db=SimpleNamespace(), repository=_Repo())
        result = await publisher.publish("conversation.completed", "t1", {})
        assert result is None

    async def test_publish_enqueues_idempotent_jobs(self, monkeypatch):
        from backend.app.infrastructure.queue.manager import QueueConfig, QueueManager
        from backend.app.modules.webhooks.publisher import WebhookPublisher

        class _Repo:
            def __init__(self):
                self.events = []

            async def list_subscriptions(self, tenant_id):
                return [
                    {"id": "sub-1", "active": True, "events": ["conversation.completed"]},
                    {"id": "sub-2", "active": True, "events": ["conversation.completed", "guardrail.blocked"]},
                    {"id": "sub-3", "active": False, "events": ["conversation.completed"]},
                ]

            async def record_event(self, event_id, tenant_id, event_type, payload):
                self.events.append(event_id)

        repo = _Repo()
        queue = QueueManager(QueueConfig())
        await queue.initialize()
        monkeypatch.setattr(
            "backend.app.modules.webhooks.publisher.get_queue_manager",
            lambda: queue,
        )
        publisher = WebhookPublisher(db=SimpleNamespace(), repository=repo)
        event_id = await publisher.publish("conversation.completed", "t1", {"session_id": "s1"})

        assert event_id in repo.events
        jobs = []
        while True:
            job = await queue.dequeue(timeout=0.05)
            if job is None:
                break
            jobs.append(job)
        assert len(jobs) == 2  # sub-3 inactive -> excluded
        assert all(job.payload["event_id"] == event_id for job in jobs)
        assert {job.payload["subscription_id"] for job in jobs} == {"sub-1", "sub-2"}

        # idempotent: re-enqueueing the same event+subscription is a no-op
        from backend.app.infrastructure.queue.manager import Job

        duplicate = await queue.enqueue(
            Job(type="webhook.deliver", payload={"event_id": event_id, "subscription_id": "sub-1", "tenant_id": "t1"}),
            idempotency_key=f"webhook:{event_id}:sub-1",
        )
        assert duplicate is True  # idempotent accept (no error)
        assert await queue.dequeue(timeout=0.05) is None  # nothing new queued


class TestWorkerDelivery:
    async def test_handler_delivers_and_records(self, monkeypatch, tmp_path):
        from backend.app.infrastructure.db.manager import DatabaseConfig, DatabaseManager
        from backend.app.infrastructure.db.models import Base
        from backend.app.infrastructure.db.repositories import WebhookRepository
        from backend.app.worker.handlers import handle_webhook_deliver

        db = DatabaseManager(DatabaseConfig(database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/wh.db"))
        await db.initialize()
        async with db._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        repo = WebhookRepository(db)
        sub = await repo.create_subscription("t1", "https://hook.example/x", "secret-1234567890", ["conversation.completed"])
        event = await repo.record_event("evt-1", "t1", "conversation.completed", {"session_id": "s1"})

        captured = {}

        class _FakeResponse:
            status_code = 200

        async def fake_post(self, url, content=None, headers=None):
            captured["headers"] = headers
            captured["body"] = content
            return _FakeResponse()

        monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
        monkeypatch.setattr(
            "backend.app.infrastructure.db.get_database_manager",
            lambda: db,
        )

        await handle_webhook_deliver(
            {"event_id": "evt-1", "subscription_id": sub["id"], "tenant_id": "t1"}
        )

        assert verify_signature("secret-1234567890", captured["body"], captured["headers"]["X-Neryva-Signature"])
        deliveries = await repo.list_deliveries(event_id="evt-1")
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "success"
        updated = await repo.get_event("evt-1")
        assert sub["id"] in updated["delivered_to"]

        await db.close()


class TestIdempotency:
    def test_guard_roundtrip(self):
        from backend.app.infrastructure.cache import init_cache
        from backend.app.infrastructure.patterns.idempotency import IdempotencyGuard

        async def _run():
            cache = init_cache()
            await cache.initialize()
            guard = IdempotencyGuard()
            assert await guard.get_response("key-1") is None
            await guard.store_response("key-1", 200, {"response": "hello"}, scope="conv:acme")
            stored = await guard.get_response("key-1", scope="conv:acme")
            assert stored == {"status_code": 200, "body": {"response": "hello"}}
            assert await guard.get_response("key-1") is None  # different scope

        asyncio.run(_run())


class TestApiIdempotencySmoke:
    """End-to-end: same Idempotency-Key returns the stored response."""

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
        settings.DATABASE_URL = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/idem.db"
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
                "name": "Smoke Co",
                "allowed_topics": ["general"],
                "blocked_topics": [],
                "default_provider": "openai",
                "default_model": "gpt-4",
                "escalation_threshold": 0.5,
            },
        )
        assert resp.status_code == 200, resp.text

    def test_idempotency_key_replays_response(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                self._provision_tenant(client, "idemco")
                payload = {"tenant_slug": "idemco", "message": "hello"}
                headers = {"Idempotency-Key": "req-123"}
                first = client.post("/api/v1/conversations", json=payload, headers=headers)
                assert first.status_code == 200, first.text
                second = client.post("/api/v1/conversations", json=payload, headers=headers)
                assert second.status_code == 200
                assert second.json() == first.json()

                # different key -> fresh processing (still 200 with same shape)
                other = client.post("/api/v1/conversations", json=payload, headers={"Idempotency-Key": "req-456"})
                assert other.status_code == 200
        finally:
            restore()
