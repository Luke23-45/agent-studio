"""
Tests for the SSE streaming path (P1):

- token deltas streamed via the gateway stream through the orchestration graph
- result event carries final response + confidence + policy action
- error path yields an error event
"""

import asyncio
from uuid import uuid4

import pytest

from backend.app.application.orchestration import create_orchestration_service
from backend.app.domain.policy import PolicyAction, PolicyRule, PolicySet
from backend.app.domain.tenant import TenantConfig
from backend.tests.gateway_fakes import FakeGateway, delta, done, error_event, usage_event


@pytest.fixture
def tenant():
    return TenantConfig(
        id=uuid4(),
        name="Acme",
        slug="acme",
        default_provider="openai",
        default_model="gpt-4",
        escalation_threshold=0.5,
    )


@pytest.fixture
def orchestration(tenant):
    service = create_orchestration_service(
        tenant_config=tenant,
        # Deny-by-default (Arch 2.5): explicit allow rule so the graph
        # terminates at END instead of routing through handoff.
        policy_set=PolicySet(
            tenant_id=tenant.id,
            name="default",
            rules=[PolicyRule(name="allow-all", action=PolicyAction.ALLOW, priority=1)],
        ),
        gateway=FakeGateway(),
    )
    return service


class TestStreamMessage:
    async def test_deltas_and_result(self, orchestration):
        chunks = ["Hello ", "world", " from the stream"]
        orchestration.gateway = FakeGateway(
            stream_scripts=[
                [delta(c) for c in chunks]
                + [usage_event({}), done()]
            ]
        )

        events = []
        async for event in orchestration.stream_message(
            user_message="hi", redacted_message="hi", session_id="s1"
        ):
            events.append(event)

        deltas = "".join(e["content"] for e in events if e["type"] == "delta")
        assert deltas == "".join(chunks)

        results = [e for e in events if e["type"] == "result"]
        assert len(results) == 1
        result = results[0]
        assert result["response"] == "".join(chunks)
        assert result["confidence"] > 0.0
        assert result["handoff_required"] is False
        assert orchestration.stream_callback is not None  # remains wired for the SSE layer

    async def test_error_path_yields_error_event(self, orchestration):
        orchestration.gateway = FakeGateway(
            stream_scripts=[
                [delta("partial"), error_event("provider timeout")]
            ]
        )

        events = []
        async for event in orchestration.stream_message(
            user_message="hi", redacted_message="hi", session_id="s1"
        ):
            events.append(event)

        errors = [e for e in events if e["type"] == "error"]
        assert len(errors) == 1
        assert "provider timeout" in errors[0]["error"]

    async def test_uses_stream_not_generate(self, orchestration):
        orchestration.gateway = FakeGateway(
            stream_scripts=[[delta("chunk"), usage_event({}), done()]]
        )
        async for _ in orchestration.stream_message(
            user_message="hi", redacted_message="hi", session_id="s1"
        ):
            pass
        assert orchestration.gateway.stream_calls == 1
        assert orchestration.gateway.generate_calls == 0


class TestSseFormatting:
    def test_frame_shape(self):
        from backend.app.api.routes.conversations import _sse

        frame = _sse("delta", {"content": "x"})
        assert frame == 'event: delta\ndata: {"content": "x"}\n\n'


class _SlowGateway:
    """Gateway double whose stream yields chunks with ``sleep`` between each,
    so the route's heartbeat timer (P4-1) has a chance to fire while the
    model is still generating."""

    def __init__(self, chunks, sleep):
        self.chunks = chunks
        self.sleep = sleep
        self.generate_calls = 0

    async def generate(self, request, tenant_config, gateway_cfg=None):
        self.generate_calls += 1
        return result("".join(self.chunks), model="gpt-4", provider="openai")

    async def stream(self, request, tenant_config, gateway_cfg=None):
        for i, c in enumerate(self.chunks):
            yield delta(c)
            if i < len(self.chunks) - 1:
                await asyncio.sleep(self.sleep)
        yield done()


class TestRouteHeartbeat:
    """P4-1: the streaming route injects heartbeat frames while the model is
    quiet. Runs against the real endpoint (TestClient + sqlite) with a short
    heartbeat interval and a slow gateway stream so a heartbeat lands mid-run."""

    def _isolated_client(self, tmp_path):
        import backend.app.api.routes.conversations as conversations_module
        from backend.app.settings import feature_flags as flags_module
        from backend.app.settings.env import settings

        original = {
            "auth": settings.AUTH_ENABLED,
            "presidio": flags_module.feature_flags.ENABLE_PRESIDIO,
            "db_url": settings.DATABASE_URL,
            "cfg_path": settings.TENANT_CONFIG_PATH,
            "hb": settings.STREAM_HEARTBEAT_SECONDS,
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
            settings.STREAM_HEARTBEAT_SECONDS = original["hb"]
            settings.OPENAI_API_KEY, settings.ANTHROPIC_API_KEY, settings.GOOGLE_API_KEY = original["keys"][:3]
            settings.AZURE_API_KEY, settings.CUSTOM_LLM_API_KEY = original["keys"][3:]
            object.__setattr__(flags_module.feature_flags, "ENABLE_PRESIDIO", original["presidio"])
            conversations_module._rag_services.clear()

        settings.AUTH_ENABLED = False
        object.__setattr__(flags_module.feature_flags, "ENABLE_PRESIDIO", False)
        settings.DATABASE_URL = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/hb.db"
        settings.TENANT_CONFIG_PATH = str(tmp_path / "tenant_configs")
        settings.STREAM_HEARTBEAT_SECONDS = 0.05
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

    def test_heartbeat_frames_emitted_while_model_is_quiet(self, tmp_path, monkeypatch):
        import backend.app.api.routes.conversations as conversations_module

        client, restore = self._isolated_client(tmp_path)
        try:
            monkeypatch.setattr(
                conversations_module, "get_gateway",
                lambda: _SlowGateway(["one ", "two ", "three"], sleep=0.12),
            )
            with client:
                resp = client.post(
                    "/api/v1/tenants",
                    json={
                        "slug": "hbco",
                        "name": "HB Co",
                        # Empty allowlist: no topic classifier in the test
                        # env, so the input passes validation and the turn
                        # actually streams (the point under test is the
                        # heartbeat while the model is quiet).
                        "allowed_topics": [],
                        "blocked_topics": [],
                        "default_provider": "openai",
                        "default_model": "gpt-4",
                        "escalation_threshold": 0.5,
                    },
                )
                assert resp.status_code == 200, resp.text

                events = []
                with client.stream(
                    "POST",
                    "/api/v1/conversations/stream",
                    json={"tenant_slug": "hbco", "message": "tell me a story"},
                ) as stream:
                    for line in stream.iter_lines():
                        if line.startswith("event: "):
                            events.append(line[len("event: "):])

                assert "session" in events
                assert "delta" in events
                assert "result" in events
                # The slow stream keeps the generator alive past one
                # STREAM_HEARTBEAT_SECONDS interval: a heartbeat lands
                # between the first frame and the terminal result.
                assert "heartbeat" in events
                assert events.index("heartbeat") < events.index("result")
        finally:
            restore()
