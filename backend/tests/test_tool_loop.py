"""
P2-9 tests: the orchestration loop on the session engine.

- tool protocol: adapter parsing (OpenAI-family + Anthropic) and schema
  advertisement on the wire
- the runtime tool loop: generate -> authorize -> execute -> regenerate,
  with tool parts emitted to the persistence hook as first-class parts
- bounded tool budget (Arch 9.3): exhaustion forces a tool-less final
  generation; late tool calls are dropped
- bounded re-ask on verify failure with escalation on repeated failure
  (Arch 9.2/9.3): empty responses and ungrounded answers
- stream tool detection (OpenAI / Anthropic fragment shapes) + non-
  streaming fallback on malformed fragments
- route-level tool-part persistence into the durable thread log
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.adapters.llm import LLMProviderType
from backend.app.adapters.llm.provider import (
    LLMConfig,
    LLMResponse,
    AnthropicAdapter,
    OpenAIAdapter,
)
from backend.app.application.orchestration import create_orchestration_service
from backend.app.application.orchestration.service import (
    OrchestrationService,
    StreamToolParseError,
)
from backend.app.application.tools import (
    ToolAlreadyRegisteredError,
    ToolRegistry,
    ToolSpec,
)
from backend.app.domain.policy import PolicyAction, PolicyRule, PolicySet, PolicyType
from backend.app.domain.tenant import TenantConfig
from backend.app.gateway.types import GatewayStreamEvent
from backend.tests.gateway_fakes import FakeGateway, delta, done, usage_event

LOOKUP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "lookup",
        "description": "look something up",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
    },
}


@pytest.fixture
def tenant():
    return TenantConfig(
        id=uuid4(),
        name="Acme",
        slug="acme",
        default_provider="openai",
        default_model="gpt-4",
        escalation_threshold=0.5,
        budgets={},
    )


@pytest.fixture
def allow_policy():
    """Allow-by-rule policy set (deny-by-default would block every turn)."""
    return PolicySet(
        id=uuid4(),
        tenant_id=uuid4(),
        name="allow-all",
        rules=[
            PolicyRule(
                name="allow-all",
                policy_type=PolicyType.OUTPUT_VALIDATION,
                action=PolicyAction.ALLOW,
                conditions={},
                priority=10,
            )
        ],
    )


def _service(tenant, policy, **kwargs):
    return create_orchestration_service(
        tenant_config=tenant,
        policy_set=policy,
        gateway=kwargs.pop("gateway", FakeGateway()),
        **kwargs,
    )


# ---- scripted adapters ------------------------------------------------------


class _ScriptedAdapter:
    """Chat adapter: pops scripted LLMResponses per call; records messages."""

    def __init__(self, responses, provider_type=LLMProviderType.OPENAI):
        self._responses = list(responses)
        self._provider_type = provider_type
        self.calls = 0
        self.messages_by_call: list[list] = []
        self.tools_seen: list[list] = []
        self.chat_called = 0
        self.config = SimpleNamespace(model="gpt-4")

    @property
    def provider_type(self):
        return self._provider_type

    def _script(self):
        return self._responses[min(self.calls - 1, len(self._responses) - 1)]

    async def chat(self, messages):
        self.chat_called += 1
        self.calls += 1
        self.messages_by_call.append(list(messages))
        self.tools_seen.append(list(self.config.tools))
        return self._script()


def _lookup_registry(calls: list | None = None):
    recorded = calls if calls is not None else []

    def executor(args):
        recorded.append(args)
        return {"content": f"answer:{args.get('q')}", "is_error": False}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="lookup", description="look something up",
                 parameters={"type": "object"}, executor=executor)
    )
    return registry, recorded


# ---- registry ---------------------------------------------------------------


class TestToolRegistry:
    def test_register_duplicate_raises(self):
        registry = ToolRegistry()
        registry.register(ToolSpec(name="a", description="d"))
        with pytest.raises(ToolAlreadyRegisteredError):
            registry.register(ToolSpec(name="a", description="d"))

    def test_schemas_openai_function_format(self):
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="lookup", description="look something up",
                parameters={"type": "object", "properties": {"q": {"type": "string"}}},
            )
        )
        assert registry.schemas() == [LOOKUP_SCHEMA]

    @pytest.mark.asyncio
    async def test_execute_unknown_tool_is_error_result(self):
        registry = ToolRegistry()
        result = await registry.execute("nope", {})
        assert result["is_error"] is True
        assert "unknown tool" in result["content"]

    @pytest.mark.asyncio
    async def test_executor_failure_captured_not_raised(self):
        def boom(args):
            raise RuntimeError("kaboom")

        registry = ToolRegistry()
        registry.register(ToolSpec(name="a", description="d", executor=boom))
        result = await registry.execute("a", {})
        assert result["is_error"] is True
        assert "kaboom" in result["content"]

    @pytest.mark.asyncio
    async def test_async_executor_awaited(self):
        async def slow(args):
            return {"content": "done"}

        registry = ToolRegistry()
        registry.register(ToolSpec(name="a", description="d", executor=slow))
        result = await registry.execute("a", {})
        assert result["content"] == "done"
        assert result["is_error"] is False


# ---- the runtime tool loop --------------------------------------------------


class TestToolLoop:
    @pytest.mark.asyncio
    async def test_tool_round_trip_through_graph(self, tenant, allow_policy):
        registry, recorded = _lookup_registry()
        adapter = _ScriptedAdapter(
            [
                LLMResponse(
                    content="",
                    model="gpt-4",
                    tool_calls=[{"id": "call_1", "name": "lookup", "arguments": {"q": "hi"}}],
                ),
                LLMResponse(content="final answer", model="gpt-4"),
            ]
        )
        parts: list[dict] = []
        service = _service(
            tenant, allow_policy, tool_registry=registry,
            tool_callback=parts.append,
        )
        service.gateway = FakeGateway(chat_stub=adapter)

        result = await service.process_message(
            user_message="hi", redacted_message="hi", session_id="s1"
        )

        assert result["model_response"] == "final answer"
        assert result["error"] is None
        assert result["tool_steps"] == 1
        assert result["tool_calls"] is None
        assert recorded == [{"q": "hi"}]
        # first generation advertised the tool schema
        assert any("lookup" in str(s) for s in adapter.tools_seen[0])
        # the follow-up generation carried the rendered tool result
        assert any(
            "[tool_result tool_use_id=call_1 name=lookup status=ok]" in m.content
            for m in adapter.messages_by_call[1]
        )
        # parts persisted through the hook: tool_use first, then tool_result
        assert [p["part_type"] for p in parts] == ["tool_use", "tool_result"]
        assert parts[0]["content"]["tool_use_id"] == "call_1"
        assert parts[1]["content"]["is_error"] is False
        assert [p["part_type"] for p in result["tool_parts"]] == ["tool_use", "tool_result"]

    @pytest.mark.asyncio
    async def test_tool_budget_exhaustion_forces_final_generation(
        self, tenant, allow_policy
    ):
        tenant.budgets = {"max_tool_steps": 2}
        registry, recorded = _lookup_registry()
        adapter = _ScriptedAdapter(
            [
                LLMResponse(content="", model="gpt-4", tool_calls=[
                    {"id": f"c{i}", "name": "lookup", "arguments": {"q": str(i)}}
                ])
                for i in range(4)
            ]
            + [LLMResponse(content="final without tools", model="gpt-4")]
        )
        service = _service(tenant, allow_policy, tool_registry=registry)
        service.gateway = FakeGateway(chat_stub=adapter)

        result = await service.process_message(
            user_message="hi", redacted_message="hi", session_id="s1"
        )

        assert result["model_response"] == "final without tools"
        assert result["tool_steps"] == 2
        assert result["tool_budget_exhausted"] is True
        assert result["error"] is None
        assert len(recorded) == 2
        # the budget-exhausted regeneration advertised NO tools
        assert adapter.tools_seen[3] == []
        assert adapter.tools_seen[2] != []  # the last tooled generation

    @pytest.mark.asyncio
    async def test_authorizer_denial_blocks_execution(self, tenant, allow_policy):
        registry, recorded = _lookup_registry()

        async def deny(call):
            return False

        parts: list[dict] = []
        adapter = _ScriptedAdapter(
            [
                LLMResponse(content="", model="gpt-4", tool_calls=[
                    {"id": "call_1", "name": "lookup", "arguments": {"q": "x"}}
                ]),
                LLMResponse(content="cannot do that", model="gpt-4"),
            ]
        )
        service = _service(
            tenant, allow_policy, tool_registry=registry,
            tool_authorizer=deny, tool_callback=parts.append,
        )
        service.gateway = FakeGateway(chat_stub=adapter)

        result = await service.process_message(
            user_message="hi", redacted_message="hi", session_id="s1"
        )

        assert recorded == []  # executor never ran
        assert result["model_response"] == "cannot do that"
        result_part = parts[1]["content"]
        assert result_part["is_error"] is True
        assert result_part["blocked"] is True
        assert "blocked by policy" in result_part["content"]
        # the model saw the denial and could recover
        assert any(
            "blocked by policy" in m.content for m in adapter.messages_by_call[1]
        )

    @pytest.mark.asyncio
    async def test_authorizer_raising_denies_fail_closed(self, tenant, allow_policy):
        registry, _ = _lookup_registry()

        def boom(call):
            raise RuntimeError("policy db down")

        adapter = _ScriptedAdapter(
            [
                LLMResponse(content="", model="gpt-4", tool_calls=[
                    {"id": "call_1", "name": "lookup", "arguments": {"q": "x"}}
                ]),
                LLMResponse(content="ok", model="gpt-4"),
            ]
        )
        service = _service(
            tenant, allow_policy, tool_registry=registry, tool_authorizer=boom
        )
        service.gateway = FakeGateway(chat_stub=adapter)

        result = await service.process_message(
            user_message="hi", redacted_message="hi", session_id="s1"
        )
        assert result["model_response"] == "ok"
        assert any(
            "blocked by policy" in m.content for m in adapter.messages_by_call[1]
        )

    @pytest.mark.asyncio
    async def test_unknown_tool_becomes_error_result(self, tenant, allow_policy):
        registry = ToolRegistry()
        parts: list[dict] = []
        adapter = _ScriptedAdapter(
            [
                LLMResponse(content="", model="gpt-4", tool_calls=[
                    {"id": "call_1", "name": "ghost", "arguments": {}}
                ]),
                LLMResponse(content="recovered", model="gpt-4"),
            ]
        )
        service = _service(
            tenant, allow_policy, tool_registry=registry, tool_callback=parts.append
        )
        service.gateway = FakeGateway(chat_stub=adapter)

        result = await service.process_message(
            user_message="hi", redacted_message="hi", session_id="s1"
        )
        assert result["model_response"] == "recovered"
        assert parts[1]["content"]["is_error"] is True
        assert "unknown tool: ghost" in parts[1]["content"]["content"]

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_run_concurrently(self, tenant, allow_policy):
        registry, recorded = _lookup_registry()
        parts: list[dict] = []
        adapter = _ScriptedAdapter(
            [
                LLMResponse(content="", model="gpt-4", tool_calls=[
                    {"id": "c1", "name": "lookup", "arguments": {"q": "a"}},
                    {"id": "c2", "name": "lookup", "arguments": {"q": "b"}},
                ]),
                LLMResponse(content="done", model="gpt-4"),
            ]
        )
        service = _service(
            tenant, allow_policy, tool_registry=registry, tool_callback=parts.append
        )
        service.gateway = FakeGateway(chat_stub=adapter)

        result = await service.process_message(
            user_message="hi", redacted_message="hi", session_id="s1"
        )
        assert sorted(a["q"] for a in recorded) == ["a", "b"]
        assert result["tool_steps"] == 2
        assert [p["part_type"] for p in parts] == [
            "tool_use", "tool_result", "tool_use", "tool_result",
        ]
        assert len(result["tool_results"]) == 2

    @pytest.mark.asyncio
    async def test_malformed_arguments_are_an_error_result(self, tenant, allow_policy):
        registry, recorded = _lookup_registry()
        adapter = _ScriptedAdapter(
            [
                LLMResponse(content="", model="gpt-4", tool_calls=[
                    {"id": "c1", "name": "lookup", "arguments": "not-a-dict"}
                ]),
                LLMResponse(content="ok", model="gpt-4"),
            ]
        )
        service = _service(tenant, allow_policy, tool_registry=registry)
        service.gateway = FakeGateway(chat_stub=adapter)

        result = await service.process_message(
            user_message="hi", redacted_message="hi", session_id="s1"
        )
        assert recorded == []
        assert result["model_response"] == "ok"
        assert any(
            "malformed tool arguments" in m.content for m in adapter.messages_by_call[1]
        )

    @pytest.mark.asyncio
    async def test_no_registry_means_no_tools_advertised(self, tenant, allow_policy):
        adapter = _ScriptedAdapter([LLMResponse(content="plain", model="gpt-4")])
        service = _service(tenant, allow_policy)
        service.gateway = FakeGateway(chat_stub=adapter)

        result = await service.process_message(
            user_message="hi", redacted_message="hi", session_id="s1"
        )
        assert result["model_response"] == "plain"
        assert adapter.tools_seen == [[]]


# ---- streaming tool detection -----------------------------------------------


class TestStreamToolDetection:
    @pytest.mark.asyncio
    async def test_stream_tool_fragments_reconstruct_and_loop(
        self, tenant, allow_policy
    ):
        registry, recorded = _lookup_registry()
        parts: list[dict] = []
        service = _service(
            tenant, allow_policy, tool_registry=registry, tool_callback=parts.append
        )
        service.gateway = FakeGateway(
            stream_scripts=[
                [
                    GatewayStreamEvent(type="tool_use_start", index=0, id="call_1", name="lookup"),
                    GatewayStreamEvent(type="tool_use_delta", index=0, args='{"q":'),
                    GatewayStreamEvent(type="tool_use_delta", index=0, args='"hi"}'),
                    usage_event({"input_tokens": 5, "output_tokens": 1, "reasoning_tokens": 0, "cached_tokens": 0}),
                    done(finish_reason="tool_use"),
                ],
                [
                    delta("final streamed answer"),
                    usage_event({"input_tokens": 5, "output_tokens": 1, "reasoning_tokens": 0, "cached_tokens": 0}),
                    done(),
                ],
            ]
        )

        deltas = []
        async for event in service.stream_message(
            user_message="hi", redacted_message="hi", session_id="s1"
        ):
            if event["type"] == "delta":
                deltas.append(event["content"])

        assert recorded == [{"q": "hi"}]
        assert deltas == ["final streamed answer"]
        assert service.gateway.stream_calls == 2
        assert [p["part_type"] for p in parts] == ["tool_use", "tool_result"]

    @pytest.mark.asyncio
    async def test_stream_anthropic_fragments_reconstruct_and_loop(
        self, tenant, allow_policy
    ):
        registry, recorded = _lookup_registry()
        service = _service(tenant, allow_policy, tool_registry=registry)
        service.gateway = FakeGateway(
            stream_scripts=[
                [
                    GatewayStreamEvent(type="tool_use_start", index=0, id="toolu_1", name="lookup"),
                    GatewayStreamEvent(type="tool_use_delta", index=0, args='{"q": "hi"}'),
                    usage_event({"input_tokens": 5, "output_tokens": 1, "reasoning_tokens": 0, "cached_tokens": 0}),
                    done(finish_reason="tool_use"),
                ],
                [
                    delta("claude answer"),
                    usage_event({"input_tokens": 5, "output_tokens": 1, "reasoning_tokens": 0, "cached_tokens": 0}),
                    done(),
                ],
            ]
        )

        deltas = []
        async for event in service.stream_message(
            user_message="hi", redacted_message="hi", session_id="s1"
        ):
            if event["type"] == "delta":
                deltas.append(event["content"])

        assert recorded == [{"q": "hi"}]
        assert deltas == ["claude answer"]

    @pytest.mark.asyncio
    async def test_stream_malformed_fragments_fall_back_to_chat(
        self, tenant, allow_policy
    ):
        registry, recorded = _lookup_registry()
        stub = _ScriptedAdapter([LLMResponse(content="recovered via chat", model="gpt-4")])
        service = _service(tenant, allow_policy, tool_registry=registry)
        service.gateway = FakeGateway(
            stream_scripts=[
                [
                    GatewayStreamEvent(type="tool_use_start", index=0, id="call_1", name="lookup"),
                    GatewayStreamEvent(type="tool_use_delta", index=0, args="{not json"),
                    done(finish_reason="tool_use"),
                ]
            ],
            chat_stub=stub,
        )

        async for event in service.stream_message(
            user_message="hi", redacted_message="hi", session_id="s1"
        ):
            pass

        assert recorded == []
        assert stub.chat_called == 1
        # stream attempted once, then chat fallback produced the answer
        assert service.gateway.stream_calls == 1
        assert service.gateway.generate_calls == 1

    def test_finalize_stream_tool_calls_strict(self):
        acc = {
            0: {"id": "c1", "name": "lookup", "args": '{"q": "hi"}'},
            1: {"id": "c2", "name": "lookup", "args": "broken"},
        }
        assert OrchestrationService._finalize_stream_tool_calls(acc) is None
        acc[1]["args"] = "not json at all"
        assert OrchestrationService._finalize_stream_tool_calls(acc) is None
        missing_name = {0: {"id": "c1", "name": "", "args": "{}"}}
        assert OrchestrationService._finalize_stream_tool_calls(missing_name) is None

    def test_stream_tool_parse_error_raised(self):
        with pytest.raises(StreamToolParseError):
            raise StreamToolParseError("bad")


# ---- verify: bounded re-ask and escalation ----------------------------------


class _Retrieval:
    def __init__(self, content):
        self.content = content

    async def retrieve(self, **kwargs):
        return SimpleNamespace(
            context="",
            results=[
                SimpleNamespace(
                    score=0.9,
                    document=SimpleNamespace(
                        content=self.content,
                        metadata={"source": "refunds.md", "document_id": "d1"},
                    ),
                )
            ]
        )


class _Escalation:
    def __init__(self):
        self.calls = []

    async def create_handoff(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(success=True, ticket_id="t-1", message="ok")


UNGROUNDED = "The aardvark eats zebras at noon."
GROUNDED = "Returns are allowed within 30 days of purchase."


class TestVerifyReask:
    @pytest.mark.asyncio
    async def test_ungrounded_answer_reasks_then_grounded(self, tenant, allow_policy):
        adapter = _ScriptedAdapter(
            [
                LLMResponse(content=UNGROUNDED, model="gpt-4"),
                LLMResponse(content=GROUNDED, model="gpt-4"),
            ]
        )
        service = _service(
            tenant, allow_policy, retrieval_service=_Retrieval(
                "The refund policy allows returns within 30 days of purchase."
            )
        )
        service.gateway = FakeGateway(chat_stub=adapter)

        result = await service.process_message(
            user_message="refund policy", redacted_message="refund policy",
            session_id="s1",
        )

        assert adapter.calls == 2
        # the corrective note was appended to the second generation
        assert any(
            "failed verification" in m.content for m in adapter.messages_by_call[1]
        )
        assert result["model_response"] == GROUNDED
        assert result["validation_result"]["valid"] is True
        assert result["handoff_required"] is False
        assert result["context"]["faithfulness"]["grounded"] is True

    @pytest.mark.asyncio
    async def test_repeated_verify_failure_escalates(self, tenant, allow_policy):
        tenant.budgets = {"max_verify_retries": 1}
        escalation = _Escalation()
        adapter = _ScriptedAdapter(
            [LLMResponse(content=UNGROUNDED, model="gpt-4")]
        )
        service = _service(
            tenant, allow_policy,
            retrieval_service=_Retrieval(
                "The refund policy allows returns within 30 days of purchase."
            ),
            escalation_service=escalation,
        )
        service.gateway = FakeGateway(chat_stub=adapter)

        result = await service.process_message(
            user_message="refund policy", redacted_message="refund policy",
            session_id="s1",
        )

        assert adapter.calls == 1  # first failure already hit the bound
        assert result["verify_exhausted"] is True
        assert result["handoff_required"] is True
        assert len(escalation.calls) == 1
        assert escalation.calls[0]["conversation_history"] is not None

    @pytest.mark.asyncio
    async def test_empty_response_reasks_then_escalates(self, tenant, allow_policy):
        escalation = _Escalation()
        adapter = _ScriptedAdapter(
            [LLMResponse(content="", model="gpt-4")]
        )
        service = _service(tenant, allow_policy, escalation_service=escalation)
        service.gateway = FakeGateway(chat_stub=adapter)

        result = await service.process_message(
            user_message="hi", redacted_message="hi", session_id="s1"
        )

        # default max_verify_retries=2 -> one re-ask, then escalation
        assert adapter.calls == 2
        assert result["verify_exhausted"] is True
        assert result["handoff_required"] is True
        assert any(
            "failed verification" in m.content for m in adapter.messages_by_call[1]
        )

    @pytest.mark.asyncio
    async def test_grounded_answer_passes_without_reask(self, tenant, allow_policy):
        adapter = _ScriptedAdapter([LLMResponse(content=GROUNDED, model="gpt-4")])
        service = _service(
            tenant, allow_policy, retrieval_service=_Retrieval(
                "The refund policy allows returns within 30 days of purchase."
            )
        )
        service.gateway = FakeGateway(chat_stub=adapter)

        result = await service.process_message(
            user_message="refund policy", redacted_message="refund policy",
            session_id="s1",
        )

        assert adapter.calls == 1
        assert result["validation_result"]["valid"] is True
        assert result["handoff_required"] is False


# ---- adapter wire protocol --------------------------------------------------


class _Recorder:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    async def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self.response


class TestAdapterToolProtocol:
    @pytest.mark.asyncio
    async def test_openai_chat_sends_tools_and_parses_calls(self, monkeypatch):
        class Fn:
            name = "lookup"
            arguments = '{"q": "hi"}'

        class TC:
            id = "call_1"
            function = Fn()

        class Message:
            content = None
            tool_calls = [TC()]

        class Choice:
            message = Message()
            finish_reason = "tool_calls"

        class Response:
            choices = [Choice()]
            model = "gpt-4"
            usage = None

        adapter = OpenAIAdapter("k", LLMConfig(model="gpt-4", tools=[LOOKUP_SCHEMA]))
        recorder = _Recorder(Response())
        monkeypatch.setattr(
            adapter, "_get_client",
            lambda: SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=recorder))
            ),
        )

        response = await adapter.chat([])
        assert response.content == ""
        assert response.tool_calls == [
            {"id": "call_1", "name": "lookup", "arguments": {"q": "hi"}}
        ]
        assert recorder.kwargs["tools"] == [LOOKUP_SCHEMA]

    @pytest.mark.asyncio
    async def test_openai_malformed_arguments_kept_verbatim(self, monkeypatch):
        class Fn:
            name = "lookup"
            arguments = "not-json"

        class TC:
            id = "call_1"
            function = Fn()

        class Message:
            content = None
            tool_calls = [TC()]

        class Choice:
            message = Message()
            finish_reason = "tool_calls"

        class Response:
            choices = [Choice()]
            model = "gpt-4"
            usage = None

        adapter = OpenAIAdapter("k", LLMConfig(model="gpt-4"))
        recorder = _Recorder(Response())
        monkeypatch.setattr(
            adapter, "_get_client",
            lambda: SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=recorder))
            ),
        )

        response = await adapter.chat([])
        assert response.tool_calls[0]["arguments"] == "not-json"

    @pytest.mark.asyncio
    async def test_anthropic_chat_parses_blocks_and_maps_tools(self, monkeypatch):
        class TextBlock:
            type = "text"
            text = "here you go"

        class ToolBlock:
            type = "tool_use"
            id = "toolu_1"
            name = "lookup"
            input = {"q": "hi"}

        class Response:
            content = [TextBlock(), ToolBlock()]
            model = "claude-x"
            usage = None
            stop_reason = "tool_use"

        adapter = AnthropicAdapter("k", LLMConfig(model="claude-x", tools=[LOOKUP_SCHEMA]))
        recorder = _Recorder(Response())
        monkeypatch.setattr(
            adapter, "_get_client",
            lambda: SimpleNamespace(messages=SimpleNamespace(create=recorder)),
        )

        response = await adapter.chat([])
        assert response.content == "here you go"
        assert response.tool_calls == [
            {"id": "toolu_1", "name": "lookup", "arguments": {"q": "hi"}}
        ]
        assert recorder.kwargs["tools"] == [
            {
                "name": "lookup",
                "description": "look something up",
                "input_schema": LOOKUP_SCHEMA["function"]["parameters"],
            }
        ]

    @pytest.mark.asyncio
    async def test_anthropic_chat_omits_tools_param_when_empty(self, monkeypatch):
        class TextBlock:
            type = "text"
            text = "plain"

        class Response:
            content = [TextBlock()]
            model = "claude-x"
            usage = None
            stop_reason = "end_turn"

        adapter = AnthropicAdapter("k", LLMConfig(model="claude-x"))
        recorder = _Recorder(Response())
        monkeypatch.setattr(
            adapter, "_get_client",
            lambda: SimpleNamespace(messages=SimpleNamespace(create=recorder)),
        )

        response = await adapter.chat([])
        assert response.content == "plain"
        assert response.tool_calls is None
        assert "tools" not in recorder.kwargs

    @pytest.mark.asyncio
    async def test_anthropic_tool_only_response_round_trips(self, monkeypatch):
        class ToolBlock:
            type = "tool_use"
            id = "toolu_1"
            name = "lookup"
            input = {"q": "hi"}

        class Response:
            content = [ToolBlock()]
            model = "claude-x"
            usage = None
            stop_reason = "tool_use"

        adapter = AnthropicAdapter("k", LLMConfig(model="claude-x"))
        recorder = _Recorder(Response())
        monkeypatch.setattr(
            adapter, "_get_client",
            lambda: SimpleNamespace(messages=SimpleNamespace(create=recorder)),
        )

        # previously crashed with AttributeError on content[0].text
        response = await adapter.chat([])
        assert response.content == ""
        assert response.tool_calls[0]["name"] == "lookup"


# ---- route-level tool-part persistence --------------------------------------


class TestRouteToolCallback:
    @pytest.fixture
    async def db(self, tmp_path):
        from backend.app.infrastructure.db import init_database
        from backend.app.infrastructure.db.models import Base

        manager = init_database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db")
        await manager.initialize()
        async with manager._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield manager
        await manager.close()

    @pytest.mark.asyncio
    async def test_callback_persists_parts_as_first_class_messages(self, db):
        from backend.app.api.routes.conversations import _build_tool_callback
        from backend.app.infrastructure.db.repositories import (
            ConversationRepository,
            TenantRepository,
        )
        from backend.app.infrastructure.db.threads import ThreadRepository

        tenant_id = str(uuid4())
        await TenantRepository(db).create(
            {"id": tenant_id, "slug": f"slug-{uuid4()}", "name": "t", "features": {}}
        )
        conversation = await ConversationRepository(db).get_or_create(
            tenant_id, f"session-{uuid4()}"
        )
        threads = ThreadRepository(db)
        thread = await threads.create_thread(
            tenant_id, conversation_id=conversation["id"]
        )
        user_turn = await threads.append_message(
            tenant_id, thread["id"], role="user", content="hi",
            redacted_content="hi", conversation_id=conversation["id"],
        )

        callback = _build_tool_callback(
            threads, tenant_id, thread["id"], conversation["id"],
            user_turn["id"], "req-1",
        )
        await callback(
            {"part_type": "tool_use", "content": {
                "tool_use_id": "call_1", "name": "lookup", "arguments": {"q": "x"}
            }}
        )
        await callback(
            {"part_type": "tool_result", "content": {
                "tool_use_id": "call_1", "name": "lookup",
                "content": "answer:x", "is_error": False,
            }}
        )

        rows = await threads.read_tail(tenant_id, thread["id"], limit=10)
        tool_messages = [m for m in rows if m["role"] == "assistant"]
        assert len(tool_messages) == 2
        first_parts = await threads.list_parts(tenant_id, tool_messages[0]["id"])
        second_parts = await threads.list_parts(tenant_id, tool_messages[1]["id"])
        # part_index 0 is the canonical text part; the tool part follows
        assert first_parts[0]["part_type"] == "text"
        assert first_parts[1]["part_type"] == "tool_use"
        assert first_parts[1]["content"]["tool_use_id"] == "call_1"
        assert second_parts[0]["part_type"] == "text"
        assert second_parts[1]["part_type"] == "tool_result"
        assert second_parts[1]["content"]["content"] == "answer:x"
        # redaction unavailable in test env: raw content preserved as-is
        assert second_parts[1]["redacted_content"]["content"] == "answer:x"
