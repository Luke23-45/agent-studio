"""
P2-7 tool-result clearing tests (Arch 8.3):

- durable sub-transcript op: payloads reclaimed, tool_use record kept,
  redacted audit placeholder, idempotent re-runs, recency guard
- render-time marker: list_tool_result_seqs + route _load_history_turns
- service: feature gate, config, missing thread/tenant degradation
- worker handler: targeted job, disabled skip, registration
- orchestration: placeholder swap gated by the tenant feature
"""

from uuid import uuid4

import pytest

from backend.app.adapters.llm import LLMConfig, LLMProviderType, LLMResponse
from backend.app.application.clearing import (
    JOB_TOOL_RESULT_CLEAR,
    TOOL_CLEAR_FEATURE,
    clear_stale_tool_results,
    tool_clearing_config,
)
from backend.app.application.orchestration import create_orchestration_service
from backend.app.context import TOOL_RESULT_PLACEHOLDER, SessionContextLoader
from backend.app.domain.policy import PolicyAction, PolicySet
from backend.app.domain.tenant import TenantConfig
from backend.app.gateway.catalog import ModelCatalog, ModelSpec
from backend.app.infrastructure.db import (
    ConversationRepository,
    TenantRepository,
    ThreadRepository,
    init_database,
)
from backend.app.infrastructure.db.models import Base
from backend.app.infrastructure.db.threads import ThreadNotFoundError
from backend.app.worker import handlers as worker_handlers

TOOL_USE_PART = {
    "part_type": "tool_use",
    "content": {"id": "call_1", "name": "lookup_order", "input": {"order_id": "A-1"}},
    "redacted_content": {
        "id": "call_1",
        "name": "lookup_order",
        "input": {"order_id": "A-1"},
    },
}
TOOL_RESULT_PART = {
    "part_type": "tool_result",
    "content": {
        "tool_use_id": "call_1",
        "payload": {"items": ["customer secret PII"]},
    },
    "redacted_content": {
        "tool_use_id": "call_1",
        "payload": {"items": ["[redacted] secret"]},
    },
}


@pytest.fixture
async def db(tmp_path):
    manager = init_database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db")
    await manager.initialize()
    async with manager._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield manager
    await manager.close()


async def _tenant(db, **overrides):
    tenant_id = str(uuid4())
    data = {
        "id": tenant_id,
        "slug": f"slug-{uuid4()}",
        "name": "test tenant",
        "features": {},
    }
    data.update(overrides)
    await TenantRepository(db).create(data)
    return tenant_id


async def _thread(db, n_messages=5, tool_at_seq=3):
    tenant_id = await _tenant(db)
    conversation = await ConversationRepository(db).get_or_create(
        tenant_id, f"session-{uuid4()}"
    )
    threads = ThreadRepository(db)
    thread = await threads.create_thread(
        tenant_id, conversation_id=conversation["id"]
    )
    for seq in range(1, n_messages + 1):
        extra = None
        if seq == tool_at_seq:
            extra = [TOOL_USE_PART, TOOL_RESULT_PART]
        await threads.append_message(
            tenant_id,
            thread["id"],
            role="user" if seq % 2 else "assistant",
            content=f"msg {seq}",
            redacted_content=f"msg {seq}",
            conversation_id=conversation["id"],
            extra_parts=extra,
        )
    return tenant_id, thread


async def _tool_message_id(db, tenant_id, thread_id):
    rows = await ThreadRepository(db).read_tail(tenant_id, thread_id, limit=100)
    tooled = next(m for m in rows if m["seq"] == 3)
    return tooled["id"]


async def _clear_events(db, tenant_id, thread_id):
    events = await ThreadRepository(db).list_events(tenant_id, thread_id)
    return [e for e in events if e["event_type"] == "tool_result.clear"]


# ---- durable sub-transcript op ---------------------------------------------


@pytest.mark.asyncio
async def test_clear_reclaims_payload_keeps_tool_use_record(db):
    tenant_id, thread = await _thread(db)
    repo = ThreadRepository(db)
    message_id = await _tool_message_id(db, tenant_id, thread["id"])

    result = await repo.clear_tool_results(
        tenant_id, thread["id"], keep_recent_turns=1
    )
    assert result["cleared"] == 1
    assert result["messages_affected"] == 1

    parts = await repo.list_parts(tenant_id, message_id)
    by_type = {p["part_type"]: p for p in parts}
    assert by_type["tool_use"]["content"] == TOOL_USE_PART["content"]
    assert by_type["tool_use"]["redacted_content"] == TOOL_USE_PART["redacted_content"]
    assert by_type["tool_result"]["content"] == {
        "tool_use_id": "call_1",
        "cleared": True,
    }
    assert by_type["tool_result"]["redacted_content"]["cleared"] is True
    assert (
        by_type["tool_result"]["redacted_content"]["placeholder"]
        == TOOL_RESULT_PLACEHOLDER
    )

    events = await _clear_events(db, tenant_id, thread["id"])
    assert len(events) == 1
    assert events[0]["payload"]["cleared"] == 1
    assert events[0]["payload"]["messages_affected"] == 1


@pytest.mark.asyncio
async def test_clear_is_idempotent(db):
    tenant_id, thread = await _thread(db)
    repo = ThreadRepository(db)

    first = await repo.clear_tool_results(tenant_id, thread["id"])
    assert first["cleared"] == 1
    second = await repo.clear_tool_results(tenant_id, thread["id"])
    assert second == {"cleared": 0, "messages_affected": 0}
    assert len(await _clear_events(db, tenant_id, thread["id"])) == 1


@pytest.mark.asyncio
async def test_keep_recent_turns_guard(db):
    tenant_id, thread = await _thread(db, tool_at_seq=4)
    repo = ThreadRepository(db)

    guarded = await repo.clear_tool_results(tenant_id, thread["id"], keep_recent_turns=2)
    # latest seq 5, threshold 3 -> the seq-4 payload is still live
    assert guarded == {"cleared": 0, "messages_affected": 0}

    unguarded = await repo.clear_tool_results(tenant_id, thread["id"], keep_recent_turns=0)
    assert unguarded["cleared"] == 1


@pytest.mark.asyncio
async def test_clear_covers_multiple_messages(db):
    tenant_id, thread = await _thread(db, tool_at_seq=3)
    repo = ThreadRepository(db)
    # second tooled turn on the newest assistant message
    await repo.append_message(
        tenant_id,
        thread["id"],
        role="assistant",
        content="msg 6",
        redacted_content="msg 6",
        conversation_id=thread["conversation_id"],
        extra_parts=[TOOL_USE_PART, TOOL_RESULT_PART],
    )
    result = await repo.clear_tool_results(tenant_id, thread["id"])
    assert result["cleared"] == 2
    assert result["messages_affected"] == 2


@pytest.mark.asyncio
async def test_clear_unknown_thread_raises(db):
    with pytest.raises(ThreadNotFoundError):
        await ThreadRepository(db).clear_tool_results(
            str(uuid4()), "missing-thread"
        )


# ---- render-time marker -----------------------------------------------------


@pytest.mark.asyncio
async def test_list_tool_result_seqs_only_live_payloads(db):
    tenant_id, thread = await _thread(db)
    repo = ThreadRepository(db)

    assert await repo.list_tool_result_seqs(tenant_id, thread["id"]) == {3}
    await repo.clear_tool_results(tenant_id, thread["id"])
    assert await repo.list_tool_result_seqs(tenant_id, thread["id"]) == set()


@pytest.mark.asyncio
async def test_load_history_turns_marks_tool_payloads(db):
    from backend.app.api.routes.conversations import _load_history_turns

    tenant_id, thread = await _thread(db)
    repo = ThreadRepository(db)
    turns = await _load_history_turns(repo, thread["id"], tenant_id)
    by_seq = {t.seq: t for t in turns}
    assert by_seq[3].has_tool_payload is True
    assert by_seq[1].has_tool_payload is False

    await repo.clear_tool_results(tenant_id, thread["id"])
    turns = await _load_history_turns(repo, thread["id"], tenant_id)
    assert all(t.has_tool_payload is False for t in turns)


# ---- service layer ----------------------------------------------------------


def test_tool_clearing_config_from_features():
    off = TenantConfig(features={})
    config_off = tool_clearing_config(off)
    assert (config_off.enabled, config_off.keep_recent_turns) == (False, 2)
    on = TenantConfig(
        features={TOOL_CLEAR_FEATURE: True},
        tool_clearing={"keep_recent_turns": 5.0},
    )
    config_on = tool_clearing_config(on)
    assert (config_on.enabled, config_on.keep_recent_turns) == (True, 5)


@pytest.mark.asyncio
async def test_service_disabled_feature_is_noop(db):
    tenant_id, thread = await _thread(db)
    result = await clear_stale_tool_results(db, tenant_id, thread["id"])
    assert result == {"cleared": 0, "reason": "disabled"}
    assert await ThreadRepository(db).list_tool_result_seqs(tenant_id, thread["id"]) == {3}


@pytest.mark.asyncio
async def test_service_enabled_clears_with_tenant_config(db):
    tenant_id, thread = await _thread(db)
    config = TenantConfig(
        id=uuid4(),
        features={TOOL_CLEAR_FEATURE: True},
        tool_clearing={"keep_recent_turns": 2.0},
    )
    result = await clear_stale_tool_results(
        db, tenant_id, thread["id"], tenant_config=config
    )
    assert result["reason"] == "cleared"
    assert result["cleared"] == 1
    assert await ThreadRepository(db).list_tool_result_seqs(tenant_id, thread["id"]) == set()


@pytest.mark.asyncio
async def test_service_respects_keep_recent_turns(db):
    tenant_id, thread = await _thread(db)
    config = TenantConfig(
        id=uuid4(),
        features={TOOL_CLEAR_FEATURE: True},
        tool_clearing={"keep_recent_turns": 4.0},
    )
    result = await clear_stale_tool_results(
        db, tenant_id, thread["id"], tenant_config=config
    )
    assert result["reason"] == "nothing_to_clear"
    assert result["cleared"] == 0


@pytest.mark.asyncio
async def test_service_missing_thread_degrades(db):
    tenant_id = await _tenant(db)
    config = TenantConfig(id=uuid4(), features={TOOL_CLEAR_FEATURE: True})
    result = await clear_stale_tool_results(
        db, tenant_id, "missing-thread", tenant_config=config
    )
    assert result == {"cleared": 0, "reason": "thread_not_found"}


@pytest.mark.asyncio
async def test_service_missing_tenant_degrades(db):
    result = await clear_stale_tool_results(db, str(uuid4()), "any-thread")
    assert result == {"cleared": 0, "reason": "tenant_not_found"}


@pytest.mark.asyncio
async def test_service_reads_config_from_db_row(db):
    tenant_id, thread = await _thread(db)
    await TenantRepository(db).update(
        tenant_id, {"features": {TOOL_CLEAR_FEATURE: True}}
    )
    result = await clear_stale_tool_results(db, tenant_id, thread["id"])
    assert result["reason"] == "cleared"
    assert result["cleared"] == 1


# ---- orchestration render-time swap ----------------------------------------


class _AdapterStub:
    def __init__(self, chat, model):
        self.provider_type = LLMProviderType.OPENAI
        self.config = LLMConfig(model=model)
        self._chat = chat

    async def chat(self, messages):
        return await self._chat(messages)


def _orchestration(tenant_config):
    catalog = ModelCatalog(specs=[ModelSpec("openai", "gpt-4", context_window=2000)])
    return create_orchestration_service(
        tenant_config=tenant_config,
        policy_set=PolicySet(tenant_id=uuid4(), name="default"),
        llm_api_key="test-key",
        model_catalog=catalog,
        context_loader=SessionContextLoader(
            model_catalog=catalog, output_reserve_tokens=0
        ),
    )


def _state(tenant_config, history):
    return {
        "tenant_id": tenant_config.id,
        "session_id": "s1",
        "user_message": "hi",
        "redacted_message": "hi",
        "context": {},
        "conversation_history": history,
        "retrieved_docs": [],
        "model_response": None,
        "validation_result": {},
        "policy_action": PolicyAction.ALLOW,
        "confidence": 0.0,
        "handoff_required": False,
        "redact_attempts": 0,
        "budget_exceeded": False,
        "error": None,
    }


@pytest.mark.asyncio
async def test_orchestrator_swap_when_feature_enabled():
    tenant_config = TenantConfig(features={TOOL_CLEAR_FEATURE: True})
    service = _orchestration(tenant_config)
    chat_calls = []

    async def fake_chat(messages):
        chat_calls.append(messages)
        return LLMResponse(content="ok", model="gpt-4", usage={}, finish_reason="stop")

    service._get_llm_adapter = lambda: _AdapterStub(chat=fake_chat, model="gpt-4")

    result = await service._generate_response(
        _state(
            tenant_config,
            [
                {"role": "assistant", "content": "tool payload", "seq": 1},
                {"role": "assistant", "content": "tool payload", "seq": 2, "has_tool_payload": True},
            ],
        )
    )
    assert result["model_response"] == "ok"
    rendered = {m.content for m in chat_calls[0]}
    assert TOOL_RESULT_PLACEHOLDER in rendered
    assert "tool payload" in rendered


@pytest.mark.asyncio
async def test_orchestrator_passthrough_when_feature_off():
    tenant_config = TenantConfig(features={})
    service = _orchestration(tenant_config)
    chat_calls = []

    async def fake_chat(messages):
        chat_calls.append(messages)
        return LLMResponse(content="ok", model="gpt-4", usage={}, finish_reason="stop")

    service._get_llm_adapter = lambda: _AdapterStub(chat=fake_chat, model="gpt-4")

    result = await service._generate_response(
        _state(
            tenant_config,
            [
                {"role": "assistant", "content": "tool payload", "seq": 1, "has_tool_payload": True},
            ],
        )
    )
    assert result["model_response"] == "ok"
    rendered = [m.content for m in chat_calls[0]]
    assert "tool payload" in rendered
    assert TOOL_RESULT_PLACEHOLDER not in rendered


@pytest.mark.asyncio
async def test_orchestrator_accepts_context_turn_objects():
    from backend.app.context import ContextTurn

    tenant_config = TenantConfig(features={TOOL_CLEAR_FEATURE: True})
    service = _orchestration(tenant_config)
    chat_calls = []

    async def fake_chat(messages):
        chat_calls.append(messages)
        return LLMResponse(content="ok", model="gpt-4", usage={}, finish_reason="stop")

    service._get_llm_adapter = lambda: _AdapterStub(chat=fake_chat, model="gpt-4")

    result = await service._generate_response(
        _state(
            tenant_config,
            [
                ContextTurn(role="assistant", content="tool payload", seq=1),
                ContextTurn(
                    role="assistant", content="tool payload", seq=2, has_tool_payload=True
                ),
            ],
        )
    )
    assert result["model_response"] == "ok"
    rendered = {m.content for m in chat_calls[0]}
    assert TOOL_RESULT_PLACEHOLDER in rendered


# ---- worker handler ---------------------------------------------------------


def _patch_worker_env(monkeypatch, db):
    import backend.app.infrastructure.db as db_module

    monkeypatch.setattr(db_module, "get_database_manager", lambda: db)


@pytest.mark.asyncio
async def test_handler_targeted_clear(db, monkeypatch):
    _patch_worker_env(monkeypatch, db)
    tenant_id, thread = await _thread(db)
    await TenantRepository(db).update(
        tenant_id, {"features": {TOOL_CLEAR_FEATURE: True}}
    )

    await worker_handlers.handle_tool_result_clear(
        {"tenant_id": tenant_id, "thread_id": thread["id"]}
    )

    assert await ThreadRepository(db).list_tool_result_seqs(tenant_id, thread["id"]) == set()
    events = await _clear_events(db, tenant_id, thread["id"])
    assert len(events) == 1
    assert events[0]["payload"]["cleared"] == 1


@pytest.mark.asyncio
async def test_handler_disabled_feature_skips(db, monkeypatch):
    _patch_worker_env(monkeypatch, db)
    tenant_id, thread = await _thread(db)

    await worker_handlers.handle_tool_result_clear(
        {"tenant_id": tenant_id, "thread_id": thread["id"]}
    )

    assert await ThreadRepository(db).list_tool_result_seqs(tenant_id, thread["id"]) == {3}


@pytest.mark.asyncio
async def test_handler_missing_thread_is_benign(db, monkeypatch):
    _patch_worker_env(monkeypatch, db)
    tenant_id = await _tenant(db, features={TOOL_CLEAR_FEATURE: True})

    await worker_handlers.handle_tool_result_clear(
        {"tenant_id": tenant_id, "thread_id": "missing-thread"}
    )


@pytest.mark.asyncio
async def test_handler_requires_payload():
    with pytest.raises(ValueError):
        await worker_handlers.handle_tool_result_clear({"tenant_id": "x"})


def test_tool_result_clear_job_type_registered():
    from backend.app.infrastructure.queue.manager import init_queue

    qm = init_queue(redis_url="redis://127.0.0.1:1")
    handlers = worker_handlers.build_handlers()
    assert JOB_TOOL_RESULT_CLEAR in handlers
    assert JOB_TOOL_RESULT_CLEAR in qm._handlers
