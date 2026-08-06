"""
P2-5 background summary refresh tests (Arch 8.2):

- idempotent per-thread refresh (no growth -> skip; growth -> compact)
- stale-thread query (only threads with a summary, active, not archived,
  newest seq outgrown the boundary by the delta)
- worker handler: targeted refresh, sweep, tenant-without-key skip
- summary.refresh job type registration
"""

import json
from uuid import uuid4

import pytest

from backend.app.adapters.llm import LLMConfig, LLMProviderType, LLMResponse
from backend.app.application.compaction import (
    CompactionConfig,
    CompactionRefreshConfig,
    LLMSummaryGenerator,
    find_stale_threads,
    refresh_thread_summary,
)
from backend.app.infrastructure.db import (
    ConversationRepository,
    TenantRepository,
    ThreadRepository,
    init_database,
)
from backend.app.infrastructure.db.models import Base
from backend.app.worker import handlers as worker_handlers

VALID_PAYLOAD = {
    "objective": "Resolve billing question",
    "key_facts": ["customer pays monthly"],
    "decisions": [],
    "pending_work": [],
    "next_moves": [],
}


class StubAdapter:
    def __init__(self):
        self.provider_type = LLMProviderType.OPENAI
        self.config = LLMConfig(model="stub-summarizer")
        self.calls = []

    async def chat(self, messages):
        self.calls.append(messages)
        return LLMResponse(
            content=json.dumps(VALID_PAYLOAD), model="stub-summarizer", usage={}
        )


@pytest.fixture
async def db(tmp_path):
    manager = init_database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db")
    await manager.initialize()
    async with manager._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield manager
    await manager.close()


async def _tenant(db, tenant_id):
    await TenantRepository(db).create(
        {
            "id": tenant_id,
            "slug": f"slug-{uuid4()}",
            "name": "test tenant",
            "allowed_topics": [],
            "blocked_topics": [],
            "escalation_threshold": 0.7,
            "knowledge_allowlist": [],
            "default_provider": "openai",
            "default_model": "gpt-4",
            "features": {},
            "guardrail_config": {},
            "guardrail_thresholds": {},
        }
    )
    return tenant_id


async def _thread(db, n_messages=8, chars=5):
    tenant_id = str(uuid4())
    await _tenant(db, tenant_id)
    conversation = await ConversationRepository(db).get_or_create(
        tenant_id, f"session-{uuid4()}"
    )
    thread = await ThreadRepository(db).create_thread(
        tenant_id, conversation_id=conversation["id"]
    )
    for seq in range(1, n_messages + 1):
        content = f"msg {seq}" if chars == 5 else "y" * chars
        await ThreadRepository(db).append_message(
            tenant_id,
            thread["id"],
            role="user" if seq % 2 else "assistant",
            content=content,
            redacted_content=content,
            conversation_id=conversation["id"],
        )
    return tenant_id, thread


def _generator():
    return LLMSummaryGenerator(StubAdapter())


# ---- refresh_thread_summary ----------------------------------------------


@pytest.mark.asyncio
async def test_refresh_skips_insufficient_growth(db):
    tenant_id, thread = await _thread(db)
    result = await refresh_thread_summary(
        db,
        tenant_id,
        thread["id"],
        generator=_generator(),
        config=CompactionRefreshConfig(min_growth_turns=10),
    )
    assert result["refreshed"] is False
    assert result["reason"] == "insufficient_growth"
    row = await ThreadRepository(db).get_thread(tenant_id, thread["id"])
    assert row["summary_position"] is None


@pytest.mark.asyncio
async def test_refresh_skips_missing_thread(db):
    result = await refresh_thread_summary(
        db,
        "tenant-x",
        "missing",
        generator=_generator(),
    )
    assert result["refreshed"] is False
    assert result["reason"] == "thread_not_found"


@pytest.mark.asyncio
async def test_refresh_compacts_after_growth(db):
    tenant_id, thread = await _thread(db, n_messages=15)
    result = await refresh_thread_summary(
        db,
        tenant_id,
        thread["id"],
        generator=_generator(),
        config=CompactionRefreshConfig(min_growth_turns=3),
        compaction_config=CompactionConfig(keep_tokens=6),
    )
    assert result["refreshed"] is True
    # keep_tokens=6, 2 tokens/message: newest 3 turns (13..15) stay live.
    assert result["summary_position"] == 12
    row = await ThreadRepository(db).get_thread(tenant_id, thread["id"])
    assert row["summary_version"] == 1
    assert row["summary_block"]["content"].startswith("Objective:")


@pytest.mark.asyncio
async def test_refresh_idempotent_without_growth_after_compact(db):
    tenant_id, thread = await _thread(db, n_messages=15)
    # After the first compact the boundary sits 3 turns behind the tail
    # (15 - 12); a threshold of 4 makes the second refresh a no-op.
    config = CompactionRefreshConfig(min_growth_turns=4)
    generator = _generator()
    first = await refresh_thread_summary(
        db,
        tenant_id,
        thread["id"],
        generator=generator,
        config=config,
        compaction_config=CompactionConfig(keep_tokens=6),
    )
    assert first["refreshed"] is True
    calls_after_first = len(generator.adapter.calls)

    second = await refresh_thread_summary(
        db,
        tenant_id,
        thread["id"],
        generator=generator,
        config=config,
        compaction_config=CompactionConfig(keep_tokens=6),
    )
    assert second["refreshed"] is False
    assert second["reason"] == "insufficient_growth"
    assert len(generator.adapter.calls) == calls_after_first
    row = await ThreadRepository(db).get_thread(tenant_id, thread["id"])
    assert row["summary_version"] == 1


# ---- find_stale_threads --------------------------------------------------


async def _boundary_message_id(db, tenant_id, thread_id, seq):
    page = await ThreadRepository(db).list_messages(tenant_id, thread_id, limit=200)
    return next(m["id"] for m in page["messages"] if m["seq"] == seq)


@pytest.mark.asyncio
async def test_find_stale_threads_only_summary_threads_outgrown(db):
    await _thread(db)  # no summary -> never a candidate
    tenant_a, thread_a = await _thread(db, n_messages=20)
    tenant_b, thread_b = await _thread(db, n_messages=15)

    # Thread A: boundary at seq 5 -> 15 new turns (stale).
    await ThreadRepository(db).set_summary(
        tenant_a,
        thread_a["id"],
        summary_block={"content": "s", "payload": {}, "head_until_seq": 5},
        summary_position=5,
        boundary_message_id=await _boundary_message_id(db, tenant_a, thread_a["id"], 5),
    )
    # Thread B: boundary at seq 14 -> 1 new turn (fresh).
    await ThreadRepository(db).set_summary(
        tenant_b,
        thread_b["id"],
        summary_block={"content": "s", "payload": {}, "head_until_seq": 14},
        summary_position=14,
        boundary_message_id=await _boundary_message_id(db, tenant_b, thread_b["id"], 14),
    )

    stale = await find_stale_threads(
        db, CompactionRefreshConfig(min_growth_turns=10)
    )
    stale_keys = {(t["tenant_id"], t["id"]) for t in stale}
    assert (tenant_a, thread_a["id"]) in stale_keys
    assert (tenant_b, thread_b["id"]) not in stale_keys
    entry = next(t for t in stale if t["id"] == thread_a["id"])
    assert entry["latest_seq"] == 20


@pytest.mark.asyncio
async def test_find_stale_threads_excludes_archived_and_inactive(db):
    tenant_id, thread = await _thread(db, n_messages=20)
    await ThreadRepository(db).set_summary(
        tenant_id,
        thread["id"],
        summary_block={"content": "s", "payload": {}, "head_until_seq": 2},
        summary_position=2,
        boundary_message_id=await _boundary_message_id(db, tenant_id, thread["id"], 2),
    )
    async with db.get_session() as session:
        from sqlalchemy import update

        from backend.app.infrastructure.db.models import ThreadModel

        await session.execute(
            update(ThreadModel)
            .where(ThreadModel.id == thread["id"])
            .values(archived=True)
        )
        await session.commit()

    stale = await find_stale_threads(db, CompactionRefreshConfig(min_growth_turns=1))
    assert (tenant_id, thread["id"]) not in {
        (t["tenant_id"], t["id"]) for t in stale
    }


# ---- worker handler ------------------------------------------------------


def _stub_generator_builder(tenant_config, api_key):
    return LLMSummaryGenerator(StubAdapter())


def _patch_worker_env(monkeypatch, db):
    import backend.app.infrastructure.db as db_module
    import backend.app.infrastructure.keys.service as keys_service

    async def _resolve_key(self, tenant_config):
        return "test-key"

    monkeypatch.setattr(db_module, "get_database_manager", lambda: db)
    monkeypatch.setattr(keys_service.ProviderKeyService, "resolve", _resolve_key)


@pytest.mark.asyncio
async def test_handler_targeted_refresh(db, monkeypatch):
    _patch_worker_env(monkeypatch, db)
    # 20 messages x 2000 chars (500 tokens) = 10k tokens > keep_tokens 8000
    # from the default CompactionConfig, so the handler compacts.
    tenant_id, thread = await _thread(db, n_messages=20, chars=2000)

    await worker_handlers.handle_summary_refresh(
        {"tenant_id": tenant_id, "thread_id": thread["id"]},
        generator_builder=_stub_generator_builder,
    )

    row = await ThreadRepository(db).get_thread(tenant_id, thread["id"])
    assert row["summary_version"] == 1
    assert row["summary_position"] == 4


@pytest.mark.asyncio
async def test_handler_skips_tenant_without_key(db, monkeypatch):
    import backend.app.infrastructure.db as db_module
    import backend.app.infrastructure.keys.service as keys_service

    async def _no_key(self, tenant_config):
        raise keys_service.ProviderKeyNotFoundError("no key")

    monkeypatch.setattr(db_module, "get_database_manager", lambda: db)
    monkeypatch.setattr(keys_service.ProviderKeyService, "resolve", _no_key)
    tenant_id, thread = await _thread(db, n_messages=15)

    await worker_handlers.handle_summary_refresh(
        {"tenant_id": tenant_id, "thread_id": thread["id"]},
        generator_builder=_stub_generator_builder,
    )

    row = await ThreadRepository(db).get_thread(tenant_id, thread["id"])
    assert row["summary_version"] == 0


@pytest.mark.asyncio
async def test_handler_sweep_refreshes_stale_threads(db, monkeypatch):
    _patch_worker_env(monkeypatch, db)
    # 20 messages x 2000 chars (500 tokens) = 10k tokens > keep_tokens 8000,
    # so the sweep's refresh actually compacts the stale thread.
    tenant_a, thread_a = await _thread(db, n_messages=20, chars=2000)
    await ThreadRepository(db).set_summary(
        tenant_a,
        thread_a["id"],
        summary_block={"content": "s", "payload": {}, "head_until_seq": 2},
        summary_position=2,
        boundary_message_id=await _boundary_message_id(db, tenant_a, thread_a["id"], 2),
    )

    await worker_handlers.handle_summary_refresh(
        {}, generator_builder=_stub_generator_builder
    )

    row = await ThreadRepository(db).get_thread(tenant_a, thread_a["id"])
    assert row["summary_version"] == 2
    assert row["summary_position"] > 2


def test_summary_refresh_job_type_registered():
    from backend.app.infrastructure.queue.manager import init_queue

    qm = init_queue(redis_url="redis://127.0.0.1:1")
    handlers = worker_handlers.build_handlers()
    assert worker_handlers.JOB_SUMMARY_REFRESH in handlers
    assert worker_handlers.JOB_SUMMARY_REFRESH in qm._handlers
