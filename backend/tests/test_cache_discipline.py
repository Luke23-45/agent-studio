"""
P2-6 cache-discipline tests (Arch 8.2):

- layered summary blocks: one system message per immutable layer
- prompt-cache prefix stability across compaction boundaries (core acceptance)
- legacy single-string summary fallback (pre-P2-6 blocks)
- whole-block omission when the layer stack cannot fit
- cache_control markers on the stable prefix only (system + summary layers)
- compaction service: append layers (first layer byte-identical), consolidate
  at the layer cap, truncation clears the layer stack, legacy-block migration
- Anthropic wire format: system text blocks + per-message cache_control;
  multi-system messages are no longer dropped (bugfix)
- per-tenant prompt-cache hit-rate metrics (provider-normalized)
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.adapters.llm import (
    LLMConfig,
    LLMProviderType,
    LLMResponse,
    AnthropicAdapter,
)
from backend.app.adapters.llm.provider import LLMMessage
from backend.app.application.compaction import (
    CompactionBreaker,
    CompactionBreakerConfig,
    CompactionConfig,
    CompactionService,
    SummarySchema,
)
from backend.app.application.orchestration import create_orchestration_service
from backend.app.context import (
    CACHE_CONTROL_METADATA,
    SUMMARY_BLOCK_HEADER,
    SessionContextLoader,
)
from backend.app.context.assembler import ContextTurn
from backend.app.context.metrics import PromptCacheMetricsCollector
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


class PayloadSeqStub:
    """Summarizer returning a distinct payload per call (layer identity)."""

    def __init__(self):
        self.summarize_calls = []

    async def summarize(self, head_turns):
        self.summarize_calls.append(list(head_turns))
        payload = dict(VALID_PAYLOAD)
        payload["objective"] = f"Objective {len(self.summarize_calls)}"
        return payload


# ---- assembler: layered summary blocks -----------------------------------


@pytest.fixture
def loader():
    catalog = ModelCatalog(specs=[ModelSpec("openai", "gpt-4", context_window=2000)])
    return SessionContextLoader(model_catalog=catalog, output_reserve_tokens=0)


def _turns(n):
    return [
        ContextTurn.redacted(
            role="user" if i % 2 else "assistant", content=f"turn {i}", seq=i
        )
        for i in range(1, n + 1)
    ]


def test_assembler_renders_one_message_per_layer(loader):
    ctx = loader.assemble(
        provider="openai",
        model="gpt-4",
        system_prompt="SYS",
        current_message="hi",
        history_turns=_turns(8),
        summary_layers=[{"content": "L1", "position": 4}, {"content": "L2", "position": 8}],
        summary_position=8,
    )
    assert [m["role"] for m in ctx.messages] == ["system", "system", "system", "user"]
    assert ctx.messages[1]["content"] == f"{SUMMARY_BLOCK_HEADER}\nL1"
    assert ctx.messages[2]["content"] == "L2"  # bare: layer 1 carries the header
    assert ctx.token_counts["summary"] > 0
    assert ctx.messages[0]["metadata"] == CACHE_CONTROL_METADATA
    assert ctx.messages[1]["metadata"] == CACHE_CONTROL_METADATA


def test_assembler_cache_prefix_stable_across_compaction(loader):
    """Core acceptance: the system + earlier layers are byte-identical before
    and after a compaction boundary; only the appended layer + tail move."""
    before = loader.assemble(
        provider="openai",
        model="gpt-4",
        system_prompt="SYS",
        current_message="m1",
        history_turns=_turns(8),
        summary_layers=[{"content": "L1", "position": 5}],
        summary_position=5,
    )
    after = loader.assemble(
        provider="openai",
        model="gpt-4",
        system_prompt="SYS",
        current_message="m2",
        history_turns=_turns(14),
        summary_layers=[
            {"content": "L1", "position": 5},
            {"content": "L2", "position": 12},
        ],
        summary_position=12,
    )
    assert before.messages[:2] == after.messages[:2]
    assert after.messages[2] == {
        "role": "system",
        "content": "L2",
        "metadata": CACHE_CONTROL_METADATA,
    }
    # Tail moves forward: no turn at/below the boundary is rendered twice.
    assert len(before.messages) == 6  # sys + L1 + tail 6,7,8 + user
    assert len(after.messages) == 6  # sys + L1 + L2 + tail 13,14 + user


def test_assembler_legacy_summary_fallback_single_layer(loader):
    ctx = loader.assemble(
        provider="openai",
        model="gpt-4",
        system_prompt="SYS",
        current_message="hi",
        summary="legacy block",
    )
    assert ctx.messages[1]["content"] == f"{SUMMARY_BLOCK_HEADER}\nlegacy block"
    assert len(ctx.messages) == 3


def test_assembler_summary_block_omitted_whole(loader):
    small = SessionContextLoader(
        model_catalog=ModelCatalog(
            specs=[ModelSpec("openai", "gpt-4", context_window=12)]
        ),
        output_reserve_tokens=0,
    )
    ctx = small.assemble(
        provider="openai",
        model="gpt-4",
        system_prompt="SYS",
        current_message="hi",
        history_turns=_turns(8),
        summary_layers=[{"content": "y" * 200, "position": 4}],
        summary_position=4,
    )
    assert "summary" in ctx.omitted_blocks
    assert all(m["role"] != "system" for m in ctx.messages[1:])


def test_assembler_cache_markers_disabled(loader):
    plain = SessionContextLoader(
        model_catalog=loader._model_catalog,
        output_reserve_tokens=0,
        cache_markers=False,
    )
    ctx = plain.assemble(
        provider="openai",
        model="gpt-4",
        system_prompt="SYS",
        current_message="hi",
        summary_layers=[{"content": "L1", "position": 4}],
        summary_position=4,
    )
    assert ctx.messages[0]["metadata"] == {}
    assert ctx.messages[1]["metadata"] == {}


# ---- compaction service: append / consolidate / truncate ------------------


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
    conversation = await ConversationRepository(db).get_or_create(
        tenant_id, f"session-{uuid4()}"
    )
    thread = await ThreadRepository(db).create_thread(
        tenant_id, conversation_id=conversation["id"]
    )
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


def _service(db, stub, config=None):
    return CompactionService(
        threads=ThreadRepository(db),
        generator=stub,
        config=config or CompactionConfig(keep_tokens=6),
    )


@pytest.mark.asyncio
async def test_compaction_appends_layers_keeping_first_immutable(db):
    tenant_id, thread, conversation = await _thread_with_messages(db)
    stub = PayloadSeqStub()
    service = _service(db, stub)

    first = await service.compact(tenant_id, thread["id"])
    assert first["summary_position"] == 5
    block1 = (await ThreadRepository(db).get_thread(tenant_id, thread["id"]))[
        "summary_block"
    ]
    assert len(block1["layers"]) == 1
    assert block1["layers"][0]["position"] == 5

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
    second = await service.compact(tenant_id, thread["id"])
    assert second["summary_position"] == 9
    assert len(second["layers"]) == 2

    block2 = (await ThreadRepository(db).get_thread(tenant_id, thread["id"]))[
        "summary_block"
    ]
    # First layer byte-identical across the boundary; only layer 2 is new.
    assert block2["layers"][0]["content"] == block1["layers"][0]["content"]
    assert block2["layers"][0]["position"] == 5
    assert block2["layers"][1]["position"] == 9
    assert block2["layers"][1]["content"] != block1["layers"][0]["content"]
    assert block2["content"] == (
        block1["layers"][0]["content"] + "\n\n" + block2["layers"][1]["content"]
    )
    # The second compaction only summarized the growth (seqs 6..9).
    assert [m["seq"] for m in stub.summarize_calls[1]] == [6, 7, 8, 9]


@pytest.mark.asyncio
async def test_compaction_consolidates_at_layer_cap(db):
    tenant_id, thread, conversation = await _thread_with_messages(db)
    stub = PayloadSeqStub()
    service = _service(db, stub, CompactionConfig(keep_tokens=6, max_summary_layers=2))

    await service.compact(tenant_id, thread["id"])  # 1 layer @5
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
    await service.compact(tenant_id, thread["id"])  # 2 layers @9

    for seq in range(13, 17):
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
    result = await service.compact(tenant_id, thread["id"])  # consolidate
    assert result["compacted"] is True
    assert result["summary_position"] == 13
    assert len(result["layers"]) == 1
    assert result["layers"][0]["position"] == 13

    block = (await ThreadRepository(db).get_thread(tenant_id, thread["id"]))[
        "summary_block"
    ]
    assert len(block["layers"]) == 1
    assert block["content"] == block["layers"][0]["content"]
    # Consolidation re-summarized the ENTIRE head (not just the growth).
    assert [m["seq"] for m in stub.summarize_calls[-1]] == list(range(1, 14))


@pytest.mark.asyncio
async def test_compaction_truncation_clears_layers(db):
    tenant_id, thread, _ = await _thread_with_messages(db, n=15)
    breaker = CompactionBreaker(CompactionBreakerConfig(max_failures=2))
    for _ in range(2):
        await breaker.record_failure(f"{tenant_id}:{thread['id']}")

    service = CompactionService(
        threads=ThreadRepository(db),
        generator=PayloadSeqStub(),
        config=CompactionConfig(keep_tokens=6),
        breaker=breaker,
    )
    result = await service.compact(tenant_id, thread["id"])
    assert result["degraded"] is True
    block = (await ThreadRepository(db).get_thread(tenant_id, thread["id"]))[
        "summary_block"
    ]
    assert block["layers"] == []


@pytest.mark.asyncio
async def test_compaction_legacy_block_migrates_to_layers(db):
    tenant_id, thread, conversation = await _thread_with_messages(db)
    # Pre-P2-6 checkpoint: no "layers" key in the block.
    await ThreadRepository(db).set_summary(
        tenant_id,
        thread["id"],
        summary_block={"content": "legacy summary", "payload": {}, "head_until_seq": 5},
        summary_position=5,
        boundary_message_id=(await ThreadRepository(db).list_messages(
            tenant_id, thread["id"]
        ))["messages"][4]["id"],
    )
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
    stub = PayloadSeqStub()
    service = _service(db, stub)
    result = await service.compact(tenant_id, thread["id"])
    assert result["compacted"] is True
    block = (await ThreadRepository(db).get_thread(tenant_id, thread["id"]))[
        "summary_block"
    ]
    assert len(block["layers"]) == 2
    assert block["layers"][0]["content"] == "legacy summary"
    assert block["layers"][0]["position"] == 5
    assert block["layers"][1]["position"] == 9


# ---- Anthropic wire format ------------------------------------------------


class _FakeAnthropic:
    """Stub anthropic client capturing messages.create kwargs."""

    def __init__(self):
        self.kwargs = None
        captured = self

        class _Messages:
            async def create(self, **kwargs):
                captured.kwargs = kwargs
                return SimpleNamespace(
                    content=[SimpleNamespace(text="ok")],
                    model="claude-x",
                    usage=SimpleNamespace(
                        input_tokens=10,
                        output_tokens=5,
                        cache_read_input_tokens=3,
                        cache_creation_input_tokens=0,
                    ),
                    stop_reason="end_turn",
                )

        self.messages = _Messages()


def _anthropic_adapter():
    return AnthropicAdapter("key", LLMConfig(model="claude-x"))


@pytest.mark.asyncio
async def test_anthropic_system_blocks_with_cache_control(monkeypatch):
    client = _FakeAnthropic()
    adapter = _anthropic_adapter()
    monkeypatch.setattr(adapter, "_get_client", lambda: client)

    response = await adapter.chat(
        [
            LLMMessage(
                "system", "SYS", metadata=CACHE_CONTROL_METADATA
            ),
            LLMMessage("system", "L1", metadata=CACHE_CONTROL_METADATA),
            LLMMessage("user", "hi"),
        ]
    )
    assert client.kwargs["system"] == [
        {
            "type": "text",
            "text": "SYS",
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": "L1",
            "cache_control": {"type": "ephemeral"},
        },
    ]
    assert client.kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert response.usage["cached_tokens"] == 3
    assert response.usage["input_tokens"] == 10


@pytest.mark.asyncio
async def test_anthropic_system_joined_when_unmarked(monkeypatch):
    """Bugfix: ALL system messages are preserved (earlier code kept only the
    last one); without markers the joined-string form keeps the wire stable."""
    client = _FakeAnthropic()
    adapter = _anthropic_adapter()
    monkeypatch.setattr(adapter, "_get_client", lambda: client)

    await adapter.chat(
        [
            LLMMessage("system", "SYS"),
            LLMMessage("system", "knowledge block"),
            LLMMessage("user", "hi"),
        ]
    )
    assert client.kwargs["system"] == "SYS\n\nknowledge block"
    assert client.kwargs["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_anthropic_chat_message_marker_block_form(monkeypatch):
    client = _FakeAnthropic()
    adapter = _anthropic_adapter()
    monkeypatch.setattr(adapter, "_get_client", lambda: client)

    await adapter.chat(
        [
            LLMMessage("system", "SYS"),
            LLMMessage("user", "hi", metadata=CACHE_CONTROL_METADATA),
        ]
    )
    assert client.kwargs["system"] == "SYS"  # unmarked -> string form
    assert client.kwargs["messages"] == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "hi"}],
            "cache_control": {"type": "ephemeral"},
        }
    ]


# ---- per-tenant hit-rate metrics ------------------------------------------


def test_cache_metrics_provider_normalized_hit_rates():
    collector = PromptCacheMetricsCollector()
    collector.record("t1", "anthropic", 100, 50)
    collector.record("t1", "openai", 100, 40)
    collector.record("t2", "openai", 10, 20)  # clamped to 1.0

    anthropic = collector.snapshot("t1", "anthropic")
    assert anthropic["requests"] == 1
    assert anthropic["input_tokens"] == 100
    assert anthropic["cached_tokens"] == 50
    assert anthropic["hit_rate"] == pytest.approx(50 / 150, abs=1e-4)

    openai = collector.snapshot("t1", "openai")
    assert openai["hit_rate"] == pytest.approx(0.4, abs=1e-4)

    assert collector.snapshot("t2", "openai")["hit_rate"] == 1.0
    assert collector.snapshot("missing", "anthropic")["hit_rate"] == 0.0

    snapshot = collector.snapshot_all()
    assert set(snapshot) == {"t1:anthropic", "t1:openai", "t2:openai"}


def test_cache_metrics_aggregates_and_resets():
    collector = PromptCacheMetricsCollector()
    collector.record("t1", "anthropic", 100, 50)
    collector.record("t1", "anthropic", 200, 150)
    row = collector.snapshot("t1", "anthropic")
    assert row["requests"] == 2
    assert row["input_tokens"] == 300
    assert row["cached_tokens"] == 200
    assert row["hit_rate"] == pytest.approx(200 / 500, abs=1e-4)
    collector.reset()
    assert collector.snapshot("t1", "anthropic")["requests"] == 0


# ---- orchestration wiring --------------------------------------------------


def _tenant():
    return TenantConfig(
        id=uuid4(), name="t", slug="t",
        allowed_topics=["support"], escalation_threshold=0.5,
    )


class _AdapterStub:
    def __init__(self, chat, model):
        self.provider_type = LLMProviderType.OPENAI
        self.config = LLMConfig(model=model)
        self._chat = chat

    async def chat(self, messages):
        return await self._chat(messages)


@pytest.mark.asyncio
async def test_orchestration_carries_layers_and_cache_markers():
    catalog = ModelCatalog(specs=[ModelSpec("openai", "gpt-4", context_window=2000)])
    service = create_orchestration_service(
        tenant_config=_tenant(),
        policy_set=PolicySet(tenant_id=uuid4(), name="default"),
        gateway=FakeGateway(),
        model_catalog=catalog,
        context_loader=SessionContextLoader(
            model_catalog=catalog, output_reserve_tokens=0
        ),
    )
    chat_calls = []

    async def fake_chat(messages):
        chat_calls.append(messages)
        return LLMResponse(
            content="ok", model="gpt-4", usage={}, finish_reason="stop"
        )

    service.gateway = FakeGateway(chat_stub=_AdapterStub(chat=fake_chat, model="gpt-4"))

    state = {
        "tenant_id": service.tenant_config.id,
        "session_id": "s1",
        "user_message": "hi",
        "redacted_message": "hi",
        "context": {},
        "conversation_history": [
            {"role": "user", "content": "t1", "seq": 1},
            {"role": "assistant", "content": "t2", "seq": 2},
            {"role": "user", "content": "t3", "seq": 3},
        ],
        "retrieved_docs": [],
        "model_response": None,
        "validation_result": {},
        "policy_action": PolicyAction.ALLOW,
        "confidence": 0.0,
        "handoff_required": False,
        "redact_attempts": 0,
        "budget_exceeded": False,
        "context_summary": "L1\nL2",
        "context_summary_position": 2,
        "context_summary_layers": [
            {"content": "L1", "position": 1},
            {"content": "L2", "position": 2},
        ],
        "error": None,
    }
    result = await service._generate_response(state)

    assert result["model_response"] == "ok"
    messages = chat_calls[0]
    assert [m.role for m in messages] == [
        "system",
        "system",
        "system",
        "user",
        "user",
    ]
    assert messages[0].metadata == CACHE_CONTROL_METADATA
    assert messages[1].content == f"{SUMMARY_BLOCK_HEADER}\nL1"
    assert messages[1].metadata == CACHE_CONTROL_METADATA
    assert messages[2].content == "L2"
    # Tail: only turns beyond the boundary (seq 3), then the current message.
    assert messages[3].content == "t3"
    assert messages[4].content == "hi"
