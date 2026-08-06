"""
P2-8 durable memory tests (Arch 8.4):

- store: per-tenant/per-end-user scoping, expiry + erasure filtering
- retrieval: top-k keyword relevance, never a raw dump
- extractor: JSON-array parsing, strict validation, PII pass, watermark
  idempotency, closed-turn exclusion
- worker handler: targeted + disabled skip + registration
- orchestration: memory block rendered only when the tenant feature is on
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from backend.app.adapters.llm import LLMConfig, LLMProviderType, LLMResponse
from backend.app.application.memory import (
    JOB_MEMORY_EXTRACT,
    MEMORY_FEATURE,
    MemoryConfig,
    MemoryExtractionError,
    MemoryExtractor,
    erase_end_user_memory,
    extract_thread_memory,
    memory_config,
    retrieve_facts,
)
from backend.app.application.orchestration import create_orchestration_service
from backend.app.context import MEMORY_BLOCK_HEADER, SessionContextLoader
from backend.app.domain.pii import PIIRedactionResult
from backend.app.domain.policy import PolicyAction, PolicySet
from backend.app.domain.tenant import TenantConfig
from backend.app.gateway.catalog import ModelCatalog, ModelSpec
from backend.app.infrastructure.db import (
    ConversationRepository,
    MemoryRepository,
    TenantRepository,
    ThreadRepository,
    init_database,
)
from backend.app.infrastructure.db.models import Base
from backend.app.worker import handlers as worker_handlers
from backend.tests.gateway_fakes import FakeGateway


class _PiiStub:
    """Fail-closed stand-in: redacts email addresses."""

    def process_message(self, text: str) -> PIIRedactionResult:
        return PIIRedactionResult(
            original_text=text,
            redacted_text=text.replace("secret@example.com", "[EMAIL]"),
        )


class _FactsExtractor:
    def __init__(self, facts=None):
        self.facts = facts or ["prefers concise answers", "works at Acme Corp"]
        self.calls = []

    async def extract(self, turns):
        self.calls.append(turns)
        return list(self.facts)


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


async def _thread(db, n_messages=6, end_user_id=None):
    tenant_id = await _tenant(db)
    conversation = await ConversationRepository(db).get_or_create(
        tenant_id, f"session-{uuid4()}"
    )
    threads = ThreadRepository(db)
    thread = await threads.create_thread(
        tenant_id,
        conversation_id=conversation["id"],
        end_user_id=end_user_id,
    )
    for seq in range(1, n_messages + 1):
        await threads.append_message(
            tenant_id,
            thread["id"],
            role="user" if seq % 2 else "assistant",
            content=f"msg {seq}",
            redacted_content=f"msg {seq}",
            conversation_id=conversation["id"],
        )
    return tenant_id, thread


# ---- store ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_scoped_per_end_user(db):
    tenant_id, thread = await _thread(db, end_user_id="user-a")
    memory = MemoryRepository(db)
    await memory.add(
        tenant_id=tenant_id, end_user_id="user-a",
        thread_id=thread["id"], source_seq=1, content="likes dark mode",
    )
    await memory.add(
        tenant_id=tenant_id, end_user_id="user-b",
        thread_id=thread["id"], source_seq=1, content="likes light mode",
    )
    await memory.add(
        tenant_id=tenant_id, end_user_id=None,
        thread_id=thread["id"], source_seq=1, content="global fact",
    )

    own = await memory.retrieve(tenant_id, end_user_id="user-a")
    assert [f["content"] for f in own] == ["likes dark mode"]
    global_only = await memory.retrieve(tenant_id)
    assert [f["content"] for f in global_only] == ["global fact"]


@pytest.mark.asyncio
async def test_retrieve_excludes_erased_and_expired(db):
    tenant_id, thread = await _thread(db, end_user_id="user-a")
    memory = MemoryRepository(db)
    await memory.add(
        tenant_id=tenant_id, end_user_id="user-a", thread_id=thread["id"],
        source_seq=1, content="live fact",
    )
    expired_id = (await memory.add(
        tenant_id=tenant_id, end_user_id="user-a", thread_id=thread["id"],
        source_seq=2, content="expired fact",
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    ))["id"]

    await memory.erase_by_id(tenant_id, expired_id)
    assert [f["content"] for f in await memory.retrieve(tenant_id, end_user_id="user-a")] == [
        "live fact"
    ]


@pytest.mark.asyncio
async def test_erase_by_user_is_soft_and_counts(db):
    tenant_id, thread = await _thread(db, end_user_id="user-a")
    memory = MemoryRepository(db)
    for i in range(3):
        await memory.add(
            tenant_id=tenant_id, end_user_id="user-a", thread_id=thread["id"],
            source_seq=i + 1, content=f"fact {i}",
        )
    assert await memory.erase_by_user(tenant_id, "user-a") == 3
    assert await memory.retrieve(tenant_id, end_user_id="user-a") == []
    assert await memory.erase_by_user(tenant_id, "user-a") == 0


@pytest.mark.asyncio
async def test_prune_expired(db):
    tenant_id, thread = await _thread(db, end_user_id="user-a")
    memory = MemoryRepository(db)
    await memory.add(
        tenant_id=tenant_id, end_user_id="user-a", thread_id=thread["id"],
        source_seq=1, content="fresh",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    assert await memory.prune_expired(tenant_id) == 1
    assert await memory.retrieve(tenant_id, end_user_id="user-a") == []


@pytest.mark.asyncio
async def test_watermark_and_pending_threads(db):
    tenant_id, thread = await _thread(db, n_messages=5)
    memory = MemoryRepository(db)
    assert await memory.max_source_seq(tenant_id, thread["id"]) == 0
    assert [t["id"] for t in await memory.list_threads_pending_extraction()] == [thread["id"]]

    await memory.add(
        tenant_id=tenant_id, end_user_id=None, thread_id=thread["id"],
        source_seq=5, content="covered",
    )
    assert await memory.max_source_seq(tenant_id, thread["id"]) == 5
    assert await memory.list_threads_pending_extraction() == []


# ---- config -----------------------------------------------------------------


def test_memory_config_derivation():
    off = TenantConfig(features={})
    cfg = memory_config(off)
    assert (cfg.enabled, cfg.max_facts, cfg.expiry_days) == (False, 20, 30)
    on = TenantConfig(
        features={MEMORY_FEATURE: True},
        memory={"max_facts": 5.0, "expiry_days": 7.0},
    )
    cfg = memory_config(on)
    assert (cfg.enabled, cfg.max_facts, cfg.expiry_days) == (True, 5, 7)


def test_tenant_config_from_data_merges_memory_defaults():
    from backend.app.modules.tenant_config import tenant_config_from_data

    config = tenant_config_from_data(
        {"id": str(uuid4()), "name": "t", "slug": "s", "features": {}}
    )
    assert config.memory == {"max_facts": 20.0, "expiry_days": 30.0}
    merged = tenant_config_from_data(
        {
            "id": str(uuid4()), "name": "t", "slug": "s",
            "features": {}, "memory": {"max_facts": 3.0},
        }
    )
    assert merged.memory["max_facts"] == 3.0
    assert merged.memory["expiry_days"] == 30.0


# ---- extractor --------------------------------------------------------------


@pytest.mark.asyncio
async def test_extractor_parses_json_array():
    adapter = _StubAdapter('["prefers concise answers", "lives in Berlin"]')
    facts = await MemoryExtractor(adapter).extract(
        [{"role": "user", "redacted_content": "hi"}]
    )
    assert facts == ["prefers concise answers", "lives in Berlin"]


@pytest.mark.asyncio
async def test_extractor_rejects_bad_output():
    adapter = _StubAdapter("not json at all")
    with pytest.raises(MemoryExtractionError):
        await MemoryExtractor(adapter).extract(
            [{"role": "user", "redacted_content": "hi"}]
        )


# ---- extract_thread_memory --------------------------------------------------


@pytest.mark.asyncio
async def test_extract_stores_pii_filtered_facts_and_advances_watermark(db):
    tenant_id, thread = await _thread(db, n_messages=6, end_user_id="user-a")
    generator = _FactsExtractor(facts=["likes tea", "contact secret@example.com"])

    result = await extract_thread_memory(
        db, tenant_id, thread["id"], generator=generator, pii_service=_PiiStub()
    )
    assert result["reason"] == "extracted"
    assert result["extracted"] == 2
    # newest closed turn (latest message excluded) is the watermark
    assert result["source_seq"] == 5

    rows = await MemoryRepository(db).retrieve(tenant_id, end_user_id="user-a")
    contents = {r["content"] for r in rows}
    assert "likes tea" in contents
    assert "contact secret@example.com" not in contents
    assert "contact [EMAIL]" in contents
    assert {r["source_seq"] for r in rows} == {5}


@pytest.mark.asyncio
async def test_extract_is_idempotent_via_watermark(db):
    tenant_id, thread = await _thread(db, n_messages=6)
    generator = _FactsExtractor()

    first = await extract_thread_memory(
        db, tenant_id, thread["id"], generator=generator, pii_service=None
    )
    assert first["extracted"] == 2
    second = await extract_thread_memory(
        db, tenant_id, thread["id"], generator=generator, pii_service=None
    )
    # everything below the watermark is covered; only the in-flight turn
    # remains, so nothing is extracted and the LLM is never called again
    assert second["extracted"] == 0
    assert second["reason"] in ("no_new_turns", "no_closed_turns")
    assert len(generator.calls) == 1


@pytest.mark.asyncio
async def test_extract_excludes_latest_message_and_single_turn_thread(db):
    tenant_id, thread = await _thread(db, n_messages=2)
    generator = _FactsExtractor()
    result = await extract_thread_memory(
        db, tenant_id, thread["id"], generator=generator, pii_service=None
    )
    # newest message excluded -> only seq 1 closed; watermark seq 1
    assert result["extracted"] == 2
    assert result["source_seq"] == 1


@pytest.mark.asyncio
async def test_extract_missing_thread(db):
    result = await extract_thread_memory(
        db, str(uuid4()), "missing", generator=_FactsExtractor()
    )
    assert result == {"extracted": 0, "reason": "thread_not_found"}


# ---- retrieval --------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_facts_top_k_relevance(db):
    tenant_id, thread = await _thread(db, end_user_id="user-a")
    memory = MemoryRepository(db)
    for i, fact in enumerate(
        ["prefers dark mode", "works at Acme Corp", "likes long walks"],
        start=1,
    ):
        await memory.add(
            tenant_id=tenant_id, end_user_id="user-a", thread_id=thread["id"],
            source_seq=i, content=fact,
        )

    facts = await retrieve_facts(
        db, tenant_id, end_user_id="user-a", query="mode dark theme", limit=10
    )
    assert facts[0] == "prefers dark mode"
    facts = await retrieve_facts(db, tenant_id, end_user_id="user-a", limit=2)
    assert len(facts) == 2


@pytest.mark.asyncio
async def test_erase_end_user_memory_service(db):
    tenant_id, thread = await _thread(db, end_user_id="user-a")
    memory = MemoryRepository(db)
    await memory.add(
        tenant_id=tenant_id, end_user_id="user-a", thread_id=thread["id"],
        source_seq=1, content="fact",
    )
    assert await erase_end_user_memory(db, tenant_id, "user-a") == {"erased": 1}
    assert await retrieve_facts(db, tenant_id, end_user_id="user-a") == []


# ---- orchestration ----------------------------------------------------------


class _AdapterStub:
    def __init__(self, chat, model):
        self.provider_type = LLMProviderType.OPENAI
        self.config = LLMConfig(model=model)
        self._chat = chat

    async def chat(self, messages):
        return await self._chat(messages)


def _orchestration(tenant_config, retriever=None):
    catalog = ModelCatalog(specs=[ModelSpec("openai", "gpt-4", context_window=2000)])
    return create_orchestration_service(
        tenant_config=tenant_config,
        policy_set=PolicySet(tenant_id=uuid4(), name="default"),
        gateway=FakeGateway(),
        model_catalog=catalog,
        context_loader=SessionContextLoader(
            model_catalog=catalog, output_reserve_tokens=0
        ),
        memory_retriever=retriever,
    )


def _state(tenant_config):
    return {
        "tenant_id": tenant_config.id,
        "session_id": "s1",
        "user_message": "remind me",
        "redacted_message": "remind me",
        "context": {},
        "conversation_history": [],
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
async def test_orchestrator_renders_memory_block_when_feature_on():
    async def retriever(query):
        return ["prefers dark mode"]

    tenant_config = TenantConfig(features={MEMORY_FEATURE: True})
    service = _orchestration(tenant_config, retriever=retriever)
    chat_calls = []

    async def fake_chat(messages):
        chat_calls.append(messages)
        return LLMResponse(content="ok", model="gpt-4", usage={}, finish_reason="stop")

    service.gateway = FakeGateway(chat_stub=_AdapterStub(chat=fake_chat, model="gpt-4"))

    result = await service._generate_response(_state(tenant_config))
    assert result["model_response"] == "ok"
    memory_messages = [m for m in chat_calls[0] if m.role == "system"]
    assert any(MEMORY_BLOCK_HEADER in m.content for m in memory_messages)
    assert any("prefers dark mode" in m.content for m in memory_messages)


@pytest.mark.asyncio
async def test_orchestrator_skips_memory_when_feature_off():
    fetched = []

    async def retriever(query):
        fetched.append(query)
        return ["prefers dark mode"]

    tenant_config = TenantConfig(features={})
    service = _orchestration(tenant_config, retriever=retriever)
    chat_calls = []

    async def fake_chat(messages):
        chat_calls.append(messages)
        return LLMResponse(content="ok", model="gpt-4", usage={}, finish_reason="stop")

    service.gateway = FakeGateway(chat_stub=_AdapterStub(chat=fake_chat, model="gpt-4"))

    result = await service._generate_response(_state(tenant_config))
    assert result["model_response"] == "ok"
    assert fetched == []
    rendered = "".join(m.content for m in chat_calls[0])
    assert MEMORY_BLOCK_HEADER not in rendered


@pytest.mark.asyncio
async def test_orchestrator_survives_retriever_failure():
    async def retriever(query):
        raise RuntimeError("store down")

    tenant_config = TenantConfig(features={MEMORY_FEATURE: True})
    service = _orchestration(tenant_config, retriever=retriever)
    chat_calls = []

    async def fake_chat(messages):
        chat_calls.append(messages)
        return LLMResponse(content="ok", model="gpt-4", usage={}, finish_reason="stop")

    service.gateway = FakeGateway(chat_stub=_AdapterStub(chat=fake_chat, model="gpt-4"))

    result = await service._generate_response(_state(tenant_config))
    assert result["model_response"] == "ok"
    assert result["error"] is None


# ---- worker handler ---------------------------------------------------------


def _patch_worker_env(monkeypatch, db):
    import backend.app.infrastructure.db as db_module
    import backend.app.infrastructure.keys.service as keys_service

    async def _resolve_key(self, tenant_config):
        return "test-key"

    monkeypatch.setattr(db_module, "get_database_manager", lambda: db)
    monkeypatch.setattr(keys_service.ProviderKeyService, "resolve", _resolve_key)


@pytest.mark.asyncio
async def test_handler_targeted_extract(db, monkeypatch):
    _patch_worker_env(monkeypatch, db)
    tenant_id, thread = await _thread(db, n_messages=6, end_user_id="user-a")
    await TenantRepository(db).update(
        tenant_id, {"features": {MEMORY_FEATURE: True}}
    )

    await worker_handlers.handle_memory_extract(
        {"tenant_id": tenant_id, "thread_id": thread["id"]},
        generator_builder=lambda tenant_config, api_key: _FactsExtractor(),
    )

    rows = await MemoryRepository(db).retrieve(tenant_id, end_user_id="user-a")
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_handler_disabled_feature_skips(db, monkeypatch):
    _patch_worker_env(monkeypatch, db)
    tenant_id, thread = await _thread(db)

    await worker_handlers.handle_memory_extract(
        {"tenant_id": tenant_id, "thread_id": thread["id"]},
        generator_builder=lambda tenant_config, api_key: _FactsExtractor(),
    )

    assert await MemoryRepository(db).retrieve(tenant_id) == []


@pytest.mark.asyncio
async def test_handler_sweep_extracts_pending_threads(db, monkeypatch):
    _patch_worker_env(monkeypatch, db)
    tenant_id, thread = await _thread(db, n_messages=6)
    await TenantRepository(db).update(
        tenant_id, {"features": {MEMORY_FEATURE: True}}
    )

    await worker_handlers.handle_memory_extract(
        {}, generator_builder=lambda tenant_config, api_key: _FactsExtractor()
    )

    assert len(await MemoryRepository(db).retrieve(tenant_id)) == 2


def test_memory_extract_job_type_registered():
    from backend.app.infrastructure.queue.manager import init_queue

    qm = init_queue(redis_url="redis://127.0.0.1:1")
    handlers = worker_handlers.build_handlers()
    assert JOB_MEMORY_EXTRACT in handlers
    assert JOB_MEMORY_EXTRACT in qm._handlers


class _StubAdapter:
    def __init__(self, content):
        self.provider_type = LLMProviderType.OPENAI
        self.config = LLMConfig(model="stub-memory")
        self._content = content

    async def chat(self, messages):
        return LLMResponse(content=self._content, model="stub-memory", usage={})
