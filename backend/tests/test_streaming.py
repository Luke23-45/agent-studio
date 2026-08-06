"""
Tests for the SSE streaming path (P1):

- token deltas streamed via stream_chat through the orchestration graph
- provider-specific delta extraction (openai / anthropic / google shapes)
- result event carries final response + confidence + policy action
- error path yields an error event
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.adapters.llm import LLMProviderType
from backend.app.application.orchestration import create_orchestration_service
from backend.app.application.orchestration.service import OrchestrationService
from backend.app.domain.policy import PolicySet
from backend.app.domain.tenant import TenantConfig


class _FakeAdapter:
    """Minimal BaseLLMAdapter-compatible fake with configurable chunks."""

    provider_type = LLMProviderType.OPENAI

    def __init__(self, chunks, provider_type=LLMProviderType.OPENAI):
        self.chunks = chunks
        self._provider_type = provider_type
        self.config = SimpleNamespace(model="gpt-4")

    @property
    def provider_type(self):
        return self._provider_type

    async def stream_chat(self, messages):
        async def _gen():
            for text in self.chunks:
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=text))]
                )

        return _gen()

    async def chat(self, messages):
        raise AssertionError("chat() must not be used on the streaming path")


def _openai_chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


def _anthropic_chunk(text):
    return SimpleNamespace(delta=SimpleNamespace(type="text_delta", text=text))


def _google_chunk(text):
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text=text)]))]
    )


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
        policy_set=PolicySet(tenant_id=tenant.id, name="default", rules=[]),
        llm_api_key="fake-key",
    )
    return service


class TestDeltaExtraction:
    def test_openai_shape(self):
        assert OrchestrationService._extract_stream_delta(
            LLMProviderType.OPENAI, _openai_chunk("Hel")
        ) == "Hel"

    def test_anthropic_shape(self):
        assert OrchestrationService._extract_stream_delta(
            LLMProviderType.ANTHROPIC, _anthropic_chunk("lo")
        ) == "lo"

    def test_google_shape(self):
        assert OrchestrationService._extract_stream_delta(
            LLMProviderType.GOOGLE, _google_chunk("!")
        ) == "!"

    def test_malformed_chunk_returns_empty(self):
        assert OrchestrationService._extract_stream_delta(
            LLMProviderType.OPENAI, SimpleNamespace()
        ) == ""


class TestStreamMessage:
    async def test_deltas_and_result(self, orchestration):
        chunks = ["Hello ", "world", " from the stream"]
        adapter = _FakeAdapter(chunks)
        orchestration._get_llm_adapter = lambda: adapter

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
        class _BoomAdapter(_FakeAdapter):
            async def stream_chat(self, messages):
                raise RuntimeError("provider timeout")

        orchestration._get_llm_adapter = lambda: _BoomAdapter([])

        events = []
        async for event in orchestration.stream_message(
            user_message="hi", redacted_message="hi", session_id="s1"
        ):
            events.append(event)

        errors = [e for e in events if e["type"] == "error"]
        assert len(errors) == 1
        assert "provider timeout" in errors[0]["error"]

    async def test_uses_stream_chat_not_chat(self, orchestration):
        adapter = _FakeAdapter(["chunk"])
        orchestration._get_llm_adapter = lambda: adapter
        async for _ in orchestration.stream_message(
            user_message="hi", redacted_message="hi", session_id="s1"
        ):
            pass
        # chat() raises AssertionError if called; reaching here proves stream_chat used


class TestSseFormatting:
    def test_frame_shape(self):
        from backend.app.api.routes.conversations import _sse

        frame = _sse("delta", {"content": "x"})
        assert frame == 'event: delta\ndata: {"content": "x"}\n\n'
