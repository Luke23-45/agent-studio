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
