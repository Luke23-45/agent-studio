"""
P2-3/P2-4 compaction tests (Arch 8.2):

- trigger evaluation (preemptive 0.70 / reactive 0.95 / none)
- summary schema validation + deterministic rendering
- LLM summary generator (JSON-only, redacted-only conversation, bounded out)
- split mechanics (newest ~keep_tokens stays live)
- atomic checkpoint swap via ThreadRepository.set_summary (version bump,
  compaction event, boundary part) and no-op / aborted / invalid paths
- P2-4: chunk-and-merge for heads larger than the summarizer window
- P2-4: per-session breaker (trip at 3 failures) + lossy truncation fallback
  with degraded markers
- orchestration integration: preemptive trigger, provider-overflow
  one-shot recovery, second overflow degrades without retry
"""

import json
from uuid import uuid4

import pytest

from backend.app.adapters.llm import LLMConfig, LLMProviderType, LLMResponse
from backend.app.application.compaction import (
    CompactionAborted,
    CompactionBreaker,
    CompactionBreakerConfig,
    CompactionConfig,
    CompactionService,
    DEGRADED_SUMMARY_CONTENT,
    LLMSummaryGenerator,
    SummarySchema,
    SummaryValidationError,
)
from backend.app.application.orchestration import create_orchestration_service
from backend.app.context import SessionContextLoader
from backend.app.domain.policy import PolicyAction, PolicySet
from backend.app.domain.tenant import TenantConfig
from backend.app.gateway.catalog import ModelCatalog, ModelSpec
from backend.app.infrastructure.db import ConversationRepository, ThreadRepository, init_database
from backend.app.infrastructure.db.models import Base
from backend.tests.gateway_fakes import FakeGateway

VALID_PAYLOAD = {
    "objective": "Resolve billing question",
    "key_facts": ["customer pays monthly", "plan: pro"],
    "decisions": ["refund of 20"],
    "pending_work": ["send receipt"],
    "next_moves": ["escalate to finance"],
}


def _render(payload=None):
    return SummarySchema.render(payload or VALID_PAYLOAD)


class StubSummarizer:
    """Drop-in for LLMSummaryGenerator (duck-typed against summarize())."""

    def __init__(self, payload=None, error=None, window=100_000):
        self.payload = payload or VALID_PAYLOAD
        self.error = error
        self.window = window
        self.summarize_calls = []

    def summarizer_window(self) -> int:
        return self.window

    async def summarize(self, head_turns):
        self.summarize_calls.append(list(head_turns))
        if self.error:
            raise self.error
        return dict(self.payload)


# ---- triggers -----------------------------------------------------------


def test_evaluate_no_action_below_threshold():
    service = CompactionService(threads=None, generator=None)
    decision = service.evaluate(
        estimated_tokens=100, budget_tokens=200, has_summary=False
    )
    assert decision.action is None
    assert not decision.triggered
    assert decision.estimated_tokens == 100
    assert decision.budget_tokens == 200


def test_evaluate_preemptive_and_reactive_boundaries():
    service = CompactionService(threads=None, generator=None)
    assert service.evaluate(
        estimated_tokens=140, budget_tokens=200, has_summary=False
    ).action == "preemptive"
    assert service.evaluate(
        estimated_tokens=190, budget_tokens=200, has_summary=True
    ).action == "reactive"
    assert service.evaluate(
        estimated_tokens=200, budget_tokens=0, has_summary=False
    ).action is None


# ---- summary schema -----------------------------------------------------


def test_schema_validates_payload():
    assert SummarySchema.validate(VALID_PAYLOAD) == []
    assert SummarySchema.validate({"objective": "x", **{k: [] for k in VALID_PAYLOAD if k != "objective"}}) == []


def test_schema_rejects_invalid_payloads():
    errors = SummarySchema.validate("not a dict")
    assert errors == ["summary payload must be a JSON object"]
    missing = dict(VALID_PAYLOAD)
    del missing["decisions"]
    assert "missing field 'decisions'" in SummarySchema.validate(missing)
    bad = dict(VALID_PAYLOAD)
    bad["objective"] = "   "
    bad["key_facts"] = ["ok", 42]
    errors = SummarySchema.validate(bad)
    assert "'objective' must be a non-empty string" in errors
    assert "'key_facts' must be a list of strings" in errors


def test_render_deterministic():
    first = _render()
    second = _render()
    assert first == second
    assert first.startswith("Objective: Resolve billing question")
    assert "Key Facts:" in first
    assert "- customer pays monthly" in first
    assert "Next Moves:" in first
    assert "- escalate to finance" in first


# ---- LLM summary generator ----------------------------------------------


class FakeAdapter:
    def __init__(self, reply, error=None):
        self.provider_type = LLMProviderType.OPENAI
        self.config = LLMConfig(model="fake-summarizer")
        self.reply = reply
        self.error = error
        self.calls = []

    async def chat(self, messages):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return LLMResponse(
            content=self.reply, model="fake-summarizer", usage={}
        )


def _generator(reply=None, error=None):
    return LLMSummaryGenerator(
        FakeAdapter(
            reply=reply if reply is not None else json.dumps(VALID_PAYLOAD),
            error=error,
        )
    )


@pytest.mark.asyncio
async def test_summarize_valid_json():
    generator = _generator()
    payload = await generator.summarize(
        [
            {"role": "user", "redacted_content": "hello"},
            {"role": "assistant", "redacted_content": "world"},
        ]
    )
    assert payload == VALID_PAYLOAD
    prompt = generator.adapter.calls[0][0].content
    assert "hello" in prompt
    assert "world" in prompt


@pytest.mark.asyncio
async def test_summarize_handles_fenced_json():
    generator = _generator(reply=f"```json\n{json.dumps(VALID_PAYLOAD)}\n```")
    payload = await generator.summarize([{"role": "user", "redacted_content": "x"}])
    assert payload == VALID_PAYLOAD


@pytest.mark.asyncio
async def test_summarize_invalid_json_raises():
    generator = _generator(reply="not json at all")
    with pytest.raises(SummaryValidationError, match="invalid JSON"):
        await generator.summarize([{"role": "user", "redacted_content": "x"}])


@pytest.mark.asyncio
async def test_summarize_schema_violation_raises():
    generator = _generator(reply=json.dumps({"objective": "only"}))
    with pytest.raises(SummaryValidationError, match="missing field 'key_facts'"):
        await generator.summarize([{"role": "user", "redacted_content": "x"}])


@pytest.mark.asyncio
async def test_summarize_empty_head_aborts():
    generator = _generator()
    with pytest.raises(CompactionAborted, match="empty head"):
        await generator.summarize([{"role": "user", "redacted_content": ""}])


@pytest.mark.asyncio
async def test_summarize_adapter_failure_aborts():
    generator = _generator(error=RuntimeError("boom"))
    with pytest.raises(CompactionAborted, match="summarizer call failed"):
        await generator.summarize([{"role": "user", "redacted_content": "x"}])


def test_summarizer_window_math():
    catalog = ModelCatalog(
        specs=[ModelSpec("openai", "fake-summarizer", context_window=50_000)]
    )
    generator = LLMSummaryGenerator(
        FakeAdapter(reply="{}"),
        config=CompactionConfig(max_output_tokens=4096),
        model_catalog=catalog,
    )
    assert generator.summarizer_window() == 50_000 - 4096 - 4096


# ---- P2-4: chunk-and-merge -----------------------------------------------


def _generator_with_window(catalog_window, adapter):
    catalog = ModelCatalog(
        specs=[ModelSpec("openai", "fake-summarizer", context_window=catalog_window)]
    )
    return LLMSummaryGenerator(
        adapter,
        model_catalog=catalog,
    ), catalog


def _big_turn(seq, chars=400):
    return {"role": "user", "redacted_content": "x" * chars}


@pytest.mark.asyncio
async def test_summarize_chunk_and_merge_oversize_head():
    # catalog 8300 -> summarizer window 8300 - 4096 - 4096 = 108 tokens;
    # the head (8 x 100 tokens) does not fit -> chunk-and-merge.
    adapter = FakeAdapter(reply=json.dumps(VALID_PAYLOAD))
    generator, _ = _generator_with_window(8300, adapter)
    head = [_big_turn(seq) for seq in range(1, 9)]
    payload = await generator.summarize(head)
    assert payload == VALID_PAYLOAD
    # Chunks were summarized separately, then merged.
    assert len(adapter.calls) >= 2
    last_prompt = adapter.calls[-1][0].content
    assert "summary:" in last_prompt  # merge round reads summarized chunks


@pytest.mark.asyncio
async def test_summarize_single_turn_over_window_aborts():
    # catalog 8280 -> window 88 < a single 100-token turn.
    adapter = FakeAdapter(reply=json.dumps(VALID_PAYLOAD))
    generator, _ = _generator_with_window(8280, adapter)
    with pytest.raises(CompactionAborted, match="single turn"):
        await generator.summarize([_big_turn(1, chars=400)])


@pytest.mark.asyncio
async def test_summarize_nonconvergent_merge_aborts():
    # Every render is far larger than the window -> merge cannot converge.
    huge_render = {
        "objective": "z" * 2000,
        "key_facts": ["y" * 2000],
        "decisions": [],
        "pending_work": [],
        "next_moves": [],
    }
    adapter = FakeAdapter(reply=json.dumps(huge_render))
    generator, _ = _generator_with_window(8300, adapter)
    with pytest.raises(CompactionAborted):
        await generator.summarize([_big_turn(1), _big_turn(2)])


# ---- split mechanics ----------------------------------------------------


def _msg(seq, content):
    return {
        "id": f"m{seq}",
        "seq": seq,
        "role": "user" if seq % 2 else "assistant",
        "content": content,
        "redacted_content": content,
    }


def test_split_keeps_newest_within_keep_tokens():
    service = CompactionService(
        threads=None,
        generator=None,
        config=CompactionConfig(keep_tokens=6),
    )
    messages = [_msg(seq, "msg %d" % seq) for seq in range(1, 9)]
    head, tail = service._split(messages)
    assert [m["seq"] for m in head] == [1, 2, 3, 4, 5]
    assert [m["seq"] for m in tail] == [6, 7, 8]


def test_split_head_empty_when_everything_fits():
    service = CompactionService(
        threads=None, generator=None, config=CompactionConfig(keep_tokens=10_000)
    )
    messages = [_msg(seq, "m") for seq in range(1, 9)]
    head, tail = service._split(messages)
    assert head == []
    assert [m["seq"] for m in tail] == [1, 2, 3, 4, 5, 6, 7, 8]


# ---- atomic checkpoint swap (sqlite) ------------------------------------


@pytest.fixture
async def db(tmp_path):
    manager = init_database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db")
    await manager.initialize()
    async with manager._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield manager
    await manager.close()


async def _thread_with_messages(db, n=8, chars=5):
    tenant_id = f"tenant-{uuid4()}"
    conversation = await ConversationRepository(db).get_or_create(tenant_id, f"session-{uuid4()}")
    thread = await ThreadRepository(db).create_thread(tenant_id, conversation_id=conversation["id"])
    for seq in range(1, n + 1):
        role = "user" if seq % 2 else "assistant"
        content = f"msg {seq}" if chars == 5 else "y" * chars
        await ThreadRepository(db).append_message(
            tenant_id,
            thread["id"],
            role=role,
            content=content,
            redacted_content=content,
            conversation_id=conversation["id"],
        )
    return tenant_id, thread, conversation


@pytest.mark.asyncio
async def test_compact_atomic_swap(db):
    tenant_id, thread, _ = await _thread_with_messages(db)
    stub = StubSummarizer()
    service = CompactionService(
        threads=ThreadRepository(db),
        generator=stub,
        config=CompactionConfig(keep_tokens=6),
    )

    result = await service.compact(tenant_id, thread["id"], request_id="req-1")

    assert result["compacted"] is True
    assert result["summary_position"] == 5
    assert result["summary_version"] == 1
    assert stub.summarize_calls[0][-1]["seq"] == 5
    summarized = stub.summarize_calls[0]
    assert [m["seq"] for m in summarized] == [1, 2, 3, 4, 5]

    row = await ThreadRepository(db).get_thread(tenant_id, thread["id"])
    assert row["summary_version"] == 1
    assert row["summary_position"] == 5
    assert row["summary_block"]["content"] == _render()
    assert row["summary_block"]["payload"] == VALID_PAYLOAD
    assert row["summary_block"]["head_until_seq"] == 5

    events = await ThreadRepository(db).list_events(tenant_id, thread["id"])
    compaction_events = [e for e in events if e["event_type"] == "compaction"]
    assert len(compaction_events) == 1
    assert compaction_events[0]["payload"]["position"] == 5
    assert compaction_events[0]["request_id"] == "req-1"

    boundary = compaction_events[0]["payload"]["boundary_message_id"]
    parts = await ThreadRepository(db).list_parts(tenant_id, boundary)
    compaction_parts = [p for p in parts if p["part_type"] == "compaction"]
    assert len(compaction_parts) == 1
    assert compaction_parts[0]["content"]["summary"] == VALID_PAYLOAD
    assert compaction_parts[0]["redacted_content"]["summary"] == _render()


@pytest.mark.asyncio
async def test_compact_noop_when_head_empty(db):
    tenant_id, thread, _ = await _thread_with_messages(db)
    stub = StubSummarizer()
    service = CompactionService(
        threads=ThreadRepository(db),
        generator=stub,
        config=CompactionConfig(keep_tokens=10_000),
    )
    result = await service.compact(tenant_id, thread["id"])
    assert result["compacted"] is False
    assert result["summary"] is None
    assert stub.summarize_calls == []
    row = await ThreadRepository(db).get_thread(tenant_id, thread["id"])
    assert row["summary_version"] == 0
    assert row["summary_position"] is None
    assert row["summary_block"] is None


@pytest.mark.asyncio
async def test_compact_version_bump_only_on_growth(db):
    """P2-6: a re-compact without new turns is a noop (no cache-breaking
    rewrite); a checkpoint bump requires turns beyond the boundary."""
    tenant_id, thread, conversation = await _thread_with_messages(db)
    stub = StubSummarizer()
    service = CompactionService(
        threads=ThreadRepository(db),
        generator=stub,
        config=CompactionConfig(keep_tokens=6),
    )
    await service.compact(tenant_id, thread["id"])
    first = await ThreadRepository(db).get_thread(tenant_id, thread["id"])
    assert first["summary_version"] == 1
    assert first["summary_position"] == 5

    # No growth since the boundary -> nothing to summarize -> noop.
    await service.compact(tenant_id, thread["id"])
    same = await ThreadRepository(db).get_thread(tenant_id, thread["id"])
    assert same["summary_version"] == 1
    assert same["summary_position"] == 5
    events = await ThreadRepository(db).list_events(tenant_id, thread["id"])
    assert [e["event_type"] for e in events if e["event_type"] == "compaction"] == [
        "compaction"
    ]

    # Growth beyond the boundary -> second checkpoint (version bump).
    for seq in range(9, 13):
        role = "user" if seq % 2 else "assistant"
        content = f"msg {seq}"
        await ThreadRepository(db).append_message(
            tenant_id,
            thread["id"],
            role=role,
            content=content,
            redacted_content=content,
            conversation_id=conversation["id"],
        )
    await service.compact(tenant_id, thread["id"])
    second = await ThreadRepository(db).get_thread(tenant_id, thread["id"])
    assert second["summary_version"] == 2
    assert second["summary_position"] == 9
    assert len(second["summary_block"]["layers"]) == 2
    events = await ThreadRepository(db).list_events(tenant_id, thread["id"])
    assert [e["event_type"] for e in events if e["event_type"] == "compaction"] == [
        "compaction",
        "compaction",
    ]


@pytest.mark.asyncio
async def test_compact_interrupted_keeps_prior_checkpoint(db):
    tenant_id, thread, _ = await _thread_with_messages(db)
    stub = StubSummarizer(error=CompactionAborted("summarizer call failed: boom"))
    service = CompactionService(
        threads=ThreadRepository(db),
        generator=stub,
        config=CompactionConfig(keep_tokens=6),
    )
    with pytest.raises(CompactionAborted):
        await service.compact(tenant_id, thread["id"])

    row = await ThreadRepository(db).get_thread(tenant_id, thread["id"])
    assert row["summary_version"] == 0
    assert row["summary_block"] is None
    events = await ThreadRepository(db).list_events(tenant_id, thread["id"])
    assert [e for e in events if e["event_type"] == "compaction"] == []


@pytest.mark.asyncio
async def test_compact_validation_failure_is_atomic(db):
    tenant_id, thread, _ = await _thread_with_messages(db)
    stub = StubSummarizer(payload={"objective": "only"})
    service = CompactionService(
        threads=ThreadRepository(db),
        generator=stub,
        config=CompactionConfig(keep_tokens=6),
    )
    with pytest.raises(SummaryValidationError):
        await service.compact(tenant_id, thread["id"])

    row = await ThreadRepository(db).get_thread(tenant_id, thread["id"])
    assert row["summary_version"] == 0
    events = await ThreadRepository(db).list_events(tenant_id, thread["id"])
    assert [e for e in events if e["event_type"] == "compaction"] == []


@pytest.mark.asyncio
async def test_compact_chunk_and_merges_oversize_head(db):
    tenant_id, thread, _ = await _thread_with_messages(db, n=8, chars=300)
    # Head = seqs 1..7 with content 300 chars (75 tokens each) = 525 tokens,
    # summarizer window = 8300 - 8192 = 108 -> chunk-and-merge required;
    # each individual turn (75) still fits the window.
    adapter = FakeAdapter(reply=json.dumps(VALID_PAYLOAD))
    generator, _ = _generator_with_window(8300, adapter)
    service = CompactionService(
        threads=ThreadRepository(db),
        generator=generator,
        config=CompactionConfig(keep_tokens=6),
    )
    result = await service.compact(tenant_id, thread["id"])
    assert result["compacted"] is True
    assert result["summary_position"] == 8
    assert result["degraded"] is False
    assert len(adapter.calls) >= 2
    row = await ThreadRepository(db).get_thread(tenant_id, thread["id"])
    assert row["summary_block"]["content"] == _render()
    assert row["summary_version"] == 1


# ---- P2-4: breaker + lossy truncation fallback ----------------------------


@pytest.mark.asyncio
async def test_breaker_trips_after_three_failures():
    breaker = CompactionBreaker(CompactionBreakerConfig(max_failures=3))
    assert not await breaker.is_tripped("t:1")
    assert await breaker.record_failure("t:1") == 1
    assert await breaker.record_failure("t:1") == 2
    assert not await breaker.is_tripped("t:1")
    assert await breaker.record_failure("t:1") == 3
    assert await breaker.is_tripped("t:1")


@pytest.mark.asyncio
async def test_breaker_recovers_after_timeout(monkeypatch):
    import backend.app.application.compaction.breaker as breaker_module

    now = [1000.0]
    monkeypatch.setattr(breaker_module.time, "monotonic", lambda: now[0])
    breaker = CompactionBreaker(
        CompactionBreakerConfig(max_failures=3, recovery_timeout=300.0)
    )
    for _ in range(3):
        await breaker.record_failure("t:1")
    assert await breaker.is_tripped("t:1")
    now[0] += 301.0
    assert not await breaker.is_tripped("t:1")


@pytest.mark.asyncio
async def test_breaker_success_resets_counter():
    breaker = CompactionBreaker(CompactionBreakerConfig(max_failures=3))
    await breaker.record_failure("t:1")
    await breaker.record_failure("t:1")
    await breaker.record_success("t:1")
    assert await breaker.record_failure("t:1") == 1
    assert not await breaker.is_tripped("t:1")
    assert breaker.state_summary("t:1")["consecutive_failures"] == 1


@pytest.mark.asyncio
async def test_compact_lossy_truncation_when_tripped(db):
    tenant_id, thread, _ = await _thread_with_messages(db, n=15)
    breaker = CompactionBreaker(CompactionBreakerConfig(max_failures=2))
    for _ in range(2):
        await breaker.record_failure(f"{tenant_id}:{thread['id']}")

    service = CompactionService(
        threads=ThreadRepository(db),
        generator=StubSummarizer(),
        config=CompactionConfig(keep_tokens=6),
        breaker=breaker,
    )
    result = await service.compact(tenant_id, thread["id"])
    assert result["compacted"] is True
    assert result["degraded"] is True
    assert result["summary"] == DEGRADED_SUMMARY_CONTENT
    # Newest 10 turns stay live; the boundary is the 11th-from-newest (seq 5).
    assert result["summary_position"] == 5

    row = await ThreadRepository(db).get_thread(tenant_id, thread["id"])
    assert row["summary_block"]["content"] == DEGRADED_SUMMARY_CONTENT
    assert row["summary_block"]["degraded"] is True
    assert row["summary_block"]["payload"] == {}
    events = await ThreadRepository(db).list_events(tenant_id, thread["id"])
    compaction_events = [e for e in events if e["event_type"] == "compaction"]
    assert compaction_events[0]["payload"]["degraded"] is True
    parts = await ThreadRepository(db).list_parts(
        tenant_id, compaction_events[0]["payload"]["boundary_message_id"]
    )
    assert parts[-1]["content"]["degraded"] is True


@pytest.mark.asyncio
async def test_compact_lossy_truncation_noop_when_all_fits(db):
    tenant_id, thread, _ = await _thread_with_messages(db, n=4)
    breaker = CompactionBreaker(CompactionBreakerConfig(max_failures=1))
    for _ in range(1):
        await breaker.record_failure(f"{tenant_id}:{thread['id']}")

    service = CompactionService(
        threads=ThreadRepository(db),
        generator=StubSummarizer(),
        config=CompactionConfig(keep_tokens=10_000),
        breaker=breaker,
    )
    result = await service.compact(tenant_id, thread["id"])
    assert result["compacted"] is False
    row = await ThreadRepository(db).get_thread(tenant_id, thread["id"])
    assert row["summary_position"] is None


@pytest.mark.asyncio
async def test_compact_failures_trip_breaker_and_success_resets(db):
    tenant_id, thread, _ = await _thread_with_messages(db, n=15)
    breaker = CompactionBreaker(CompactionBreakerConfig(max_failures=2))
    key = f"{tenant_id}:{thread['id']}"
    service = CompactionService(
        threads=ThreadRepository(db),
        generator=StubSummarizer(error=CompactionAborted("summarizer call failed: boom")),
        config=CompactionConfig(keep_tokens=6),
        breaker=breaker,
    )
    for _ in range(2):
        with pytest.raises(CompactionAborted):
            await service.compact(tenant_id, thread["id"])
    assert await breaker.is_tripped(key)

    # Next compact takes the truncation fallback while tripped.
    service.generator = StubSummarizer()
    result = await service.compact(tenant_id, thread["id"])
    assert result["degraded"] is True

    # A successful compact (after recovery) resets the breaker.
    await breaker.record_success(key)
    result = await service.compact(tenant_id, thread["id"])
    assert result["degraded"] is False
    assert result["summary"] == _render()
    assert not await breaker.is_tripped(key)


# ---- orchestration integration ------------------------------------------


def _tenant():
    return TenantConfig(
        id=uuid4(), name="t", slug="t",
        allowed_topics=["support"], escalation_threshold=0.5,
    )


async def _run_generate(service, conversation_history=None):
    state = {
        "tenant_id": service.tenant_config.id,
        "session_id": "s1",
        "user_message": "how do I cancel?",
        "redacted_message": "how do I cancel?",
        "context": {},
        "conversation_history": conversation_history or [],
        "retrieved_docs": [],
        "model_response": None,
        "validation_result": {},
        "policy_action": PolicyAction.ALLOW,
        "confidence": 0.0,
        "handoff_required": False,
        "redact_attempts": 0,
        "error": None,
    }
    return await service._generate_response(state)


@pytest.mark.asyncio
async def test_orchestration_preemptive_trigger_once_per_turn():
    catalog = ModelCatalog(
        specs=[ModelSpec("openai", "gpt-4", context_window=200)]
    )
    callback_calls = []

    async def fake_callback(info):
        callback_calls.append(info)
        return {"summary": "compacted context", "position": 1}

    service = create_orchestration_service(
        tenant_config=_tenant(),
        policy_set=PolicySet(tenant_id=uuid4(), name="default"),
        gateway=FakeGateway(),
        model_catalog=catalog,
        context_loader=SessionContextLoader(model_catalog=catalog, output_reserve_tokens=0),
        compaction_callback=fake_callback,
    )
    chat_calls = []

    async def fake_chat(messages):
        chat_calls.append(messages)
        return LLMResponse(
            content="Here is your answer", model="gpt-4", usage={}, finish_reason="stop"
        )

    service.gateway = FakeGateway(chat_stub=_AdapterStub(chat=fake_chat, model="gpt-4"))

    # One long turn pushes the estimate past 70% of 200 but stays under 200.
    state = await _run_generate(
        service,
        conversation_history=[{"role": "user", "content": "y" * 200, "seq": 1}],
    )

    assert len(callback_calls) == 1
    assert callback_calls[0]["overflow"] is False
    assert callback_calls[0]["budget_tokens"] == 200
    assert len(chat_calls) == 1
    assert state["context_summary"] == "compacted context"
    assert state["context_summary_position"] == 1
    assert state["model_response"] == "Here is your answer"


@pytest.mark.asyncio
async def test_orchestration_overflow_recovery_retries_once():
    callback_calls = []

    async def fake_callback(info):
        callback_calls.append(info)
        return {"summary": "compacted context", "position": 0}

    service = create_orchestration_service(
        tenant_config=_tenant(),
        policy_set=PolicySet(tenant_id=uuid4(), name="default"),
        gateway=FakeGateway(),
        compaction_callback=fake_callback,
    )
    chat_calls = []

    async def fake_chat(messages):
        chat_calls.append(messages)
        if len(chat_calls) == 1:
            raise RuntimeError("maximum context length exceeded")
        return LLMResponse(
            content="recovered answer", model="fake", usage={}, finish_reason="stop"
        )

    service.gateway = FakeGateway(chat_stub=_AdapterStub(chat=fake_chat, model="fake-model"))

    state = await _run_generate(service)

    assert len(chat_calls) == 2
    assert len(callback_calls) == 1
    assert callback_calls[0]["overflow"] is True
    assert state["model_response"] == "recovered answer"
    assert state["error"] is None
    assert state["context_summary"] == "compacted context"
    assert state["context_summary_position"] == 0


@pytest.mark.asyncio
async def test_orchestration_second_overflow_is_hard_error():
    callback_calls = []

    async def fake_callback(info):
        callback_calls.append(info)
        return {"summary": "compacted context", "position": 0}

    service = create_orchestration_service(
        tenant_config=_tenant(),
        policy_set=PolicySet(tenant_id=uuid4(), name="default"),
        gateway=FakeGateway(),
        compaction_callback=fake_callback,
    )
    chat_calls = []

    async def fake_chat(messages):
        chat_calls.append(messages)
        raise RuntimeError("maximum context length exceeded")

    service.gateway = FakeGateway(chat_stub=_AdapterStub(chat=fake_chat, model="fake-model"))

    state = await _run_generate(service)

    assert len(chat_calls) == 2
    assert len(callback_calls) == 1
    assert state["model_response"] is None
    assert state["error"] is not None


@pytest.mark.asyncio
async def test_orchestration_no_compaction_without_callback():
    service = create_orchestration_service(
        tenant_config=_tenant(),
        policy_set=PolicySet(tenant_id=uuid4(), name="default"),
        gateway=FakeGateway(),
    )
    service.gateway = FakeGateway(
        chat_stub=_AdapterStub(chat=None, model="fake-model", fail="maximum context length exceeded")
    )
    state = await _run_generate(service)
    assert state["error"] is not None
    assert state["model_response"] is None


class _AdapterStub:
    """Minimal adapter stand-in: real provider_type/config + injected chat."""

    def __init__(self, chat, model, fail=None):
        self.provider_type = LLMProviderType.OPENAI
        self.config = LLMConfig(model=model)
        self._chat = chat
        self._fail = fail

    async def chat(self, messages):
        if self._fail:
            raise RuntimeError(self._fail)
        return await self._chat(messages)
