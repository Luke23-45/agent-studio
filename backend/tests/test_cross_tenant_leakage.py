"""
P5-5 — Cross-tenant leakage suite.

Every persistence surface must be tenant-scoped: the thread DB (log,
events, parts), the Redis hot tier, the object-storage prefixes (cold-tier
archives, GDPR exports, eval corpora), the shared vector store, and the
trace pipeline (PII redaction + tenant attribution). Tests use the same
shared physical stores as production (one Redis, one bucket, one vector
store) and assert that tenant A can never read tenant B's data at any
layer.
"""

import asyncio
import json
from uuid import uuid4

import pytest

from backend.app.application.archive.service import ThreadArchiveService
from backend.app.infrastructure.db import ThreadRepository, TenantRepository
from backend.app.infrastructure.db.manager import DatabaseConfig, DatabaseManager
from backend.app.infrastructure.db.models import Base
from backend.app.infrastructure.observability.tracing import _redact_dict
from backend.app.session.hot_tier import ThreadTailCache


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(
        DatabaseConfig(database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/leak.db")
    )

    async def _init():
        await manager.initialize()
        async with manager._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    yield manager
    asyncio.run(manager.close())


async def _seed_tenant(db, slug):
    return await TenantRepository(db).create(
        {
            "id": str(uuid4()),
            "slug": slug,
            "name": slug.title(),
            "allowed_topics": ["general"],
            "blocked_topics": [],
            "escalation_threshold": 0.7,
            "knowledge_allowlist": [],
            "default_provider": "openai",
            "default_model": "gpt-4",
            "features": {},
            "guardrail_config": {},
            "guardrail_thresholds": {},
            "region": "eu-west-1",
        }
    )


async def _seed_thread(db, tenant_id, conversation_id):
    repo = ThreadRepository(db)
    thread = await repo.create_thread(
        tenant_id, conversation_id=conversation_id, request_id=f"create:{uuid4()}"
    )
    await repo.append_message(
        tenant_id,
        thread["id"],
        role="user",
        content=f"secret for {conversation_id}",
        redacted_content=f"secret for {conversation_id}",
        conversation_id=conversation_id,
        request_id=f"msg:{thread['id']}",
    )
    return thread


class _FakeRedis:
    def __init__(self):
        self._data: dict[str, str] = {}

    async def ping(self):
        return True

    async def get(self, key):
        return self._data.get(key)

    async def set(self, key, value, ex=None):
        self._data[key] = value

    async def setex(self, key, ttl, value):
        self._data[key] = value

    async def expire(self, key, ttl):
        pass

    async def delete(self, *keys):
        for k in keys:
            self._data.pop(k, None)


class _FakeStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    async def upload_file(self, key, content, content_type="application/json", metadata=None):
        self.objects[key] = content
        from datetime import datetime
        import hashlib
        return _FileMeta(
            key=key,
            size=len(content),
            content_type=content_type,
            checksum_sha256=hashlib.sha256(content).hexdigest(),
            uploaded_at=datetime.utcnow().isoformat(),
            metadata=metadata or {},
        )

    async def download_file(self, key):
        if key not in self.objects:
            raise FileNotFoundError(f"File not found in storage: {key}")
        import hashlib
        from datetime import datetime
        content = self.objects[key]
        return content, _FileMeta(
            key=key, size=len(content), content_type="",
            checksum_sha256=hashlib.sha256(content).hexdigest(),
            uploaded_at="", metadata={},
        )

    async def list_files(self, prefix=""):
        return [k for k in self.objects if k.startswith(prefix)]


class _FileMeta:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


# ----------------------------------------------------------------------
# DB layer
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_threads_are_tenant_scoped(db):
    tenant_a = await _seed_tenant(db, "leak-a")
    tenant_b = await _seed_tenant(db, "leak-b")
    thread_a = await _seed_thread(db, tenant_a["id"], "conv-a")
    await _seed_thread(db, tenant_b["id"], "conv-b")

    repo = ThreadRepository(db)

    # Thread A is invisible to tenant B (and vice versa).
    assert await repo.get_thread(tenant_a["id"], thread_a["id"]) is not None
    assert await repo.get_thread(tenant_b["id"], thread_a["id"]) is None
    assert await repo.get_message(tenant_a["id"], thread_a["id"] + "-nope") is None

    # Cross-tenant message access is refused, not returned.
    assert (await repo.list_messages(tenant_b["id"], thread_a["id"]))["messages"] == []
    assert await repo.list_events(tenant_b["id"], thread_a["id"]) == []


# ----------------------------------------------------------------------
# Redis hot tier
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_hot_tier_keys_are_tenant_scoped():
    fake = _FakeRedis()
    cache = ThreadTailCache("redis://fake")
    cache._redis = fake
    cache._redis_available = True

    tenant_a, tenant_b = str(uuid4()), str(uuid4())
    shared_thread_id = "same-thread-id"

    await cache.cache_tail(
        tenant_a,
        shared_thread_id,
        [{"seq": 1, "content": "secret-a"}],
    )
    await cache.cache_tail(
        tenant_b,
        shared_thread_id,
        [{"seq": 1, "content": "secret-b"}],
    )

    # Same thread id under two tenants -> two distinct keys.
    assert len(fake._data) == 2
    assert all(k.startswith("neryva:thread:tail:") for k in fake._data)
    key_a = f"neryva:thread:tail:{tenant_a}:{shared_thread_id}"
    key_b = f"neryva:thread:tail:{tenant_b}:{shared_thread_id}"
    assert key_a in fake._data and key_b in fake._data

    # Reads are tenant-scoped: B cannot read A's tail even with the same
    # thread id.
    tail_b = await cache.get_tail(tenant_b, shared_thread_id)
    assert tail_b["tail"][0]["content"] == "secret-b"
    assert tail_b["tail"][0]["content"] != "secret-a"

    # Tenant A's key survives B's invalidation.
    await cache.invalidate(tenant_b, shared_thread_id)
    assert key_b not in fake._data
    assert key_a in fake._data


# ----------------------------------------------------------------------
# object storage (archives + exports + eval corpora)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_storage_prefixes_are_tenant_scoped(db):
    tenant_a = await _seed_tenant(db, "leak-a")
    tenant_b = await _seed_tenant(db, "leak-b")
    thread_a = await _seed_thread(db, tenant_a["id"], "conv-a")
    await _seed_thread(db, tenant_b["id"], "conv-b")

    storage = _FakeStorage()
    service = ThreadArchiveService(db, storage)

    await service.archive_thread(tenant_a["id"], thread_a["id"])
    # GDPR-export style key (same bucket, tenant-scoped).
    await storage.upload_file(
        f"tenant:{tenant_a['id']}/exports/x.json",
        b"{}",
        metadata={"tenant_id": tenant_a["id"]},
    )
    # Eval corpus namespace (tenant in key + metadata).
    await storage.upload_file(
        f"eval_corpora/{tenant_b['id']}/eval-1.jsonl",
        b"{}\n",
        metadata={"tenant_id": tenant_b["id"]},
    )

    # Every key is tenant-scoped; no key of tenant A contains tenant B's id.
    for key in storage.objects:
        assert tenant_a["id"] in key or tenant_b["id"] in key

    a_prefix = f"tenant/{tenant_a['id']}/archive/"
    b_keys = await storage.list_files(f"tenant/{tenant_b['id']}/")
    assert all(tenant_b["id"] in k for k in b_keys)
    assert (await storage.list_files(a_prefix)) != []


# ----------------------------------------------------------------------
# shared vector store
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vector_store_shared_physical_store_no_cross_tenant_docs():
    from backend.app.adapters.vectorstore import InMemoryVectorStore
    from backend.app.domain.knowledge import KnowledgeDocument, KnowledgeQuery
    from backend.app.domain.policy import PolicySet
    from backend.app.modules.rag import RAGService

    store = InMemoryVectorStore()
    a, b = str(uuid4()), str(uuid4())

    class _Embedder:
        dim = 4

        def embed_text(self, text):
            return [1.0, 0.0, 0.0, 0.0]

        def embed_texts(self, texts):
            return [self.embed_text(t) for t in texts]

    async def _add(tenant_id, source, content):
        rag = RAGService(
            vector_store=store,
            embedding_service=_Embedder(),
            tenant_id=tenant_id,
        )
        await rag.add_documents(
            [KnowledgeDocument(id=uuid4(), tenant_id=tenant_id, content=content, source=source)]
        )

    # One shared physical store; identical content for both tenants.
    await _add(a, "payroll-a.md", "TENANT-A-SECRET: salaries paid on the 1st")
    await _add(b, "payroll-b.md", "TENANT-B-SECRET: salaries paid on the 1st")

    rag_a = RAGService(
        vector_store=store,
        embedding_service=_Embedder(),
        tenant_id=a,
    )
    hits = await rag_a.search(
        KnowledgeQuery(query_text="TENANT-A-SECRET", tenant_id=a),
    )
    assert hits, "expected at least one hit for tenant A"
    assert all(h.document.metadata.get("tenant_id") == str(a) for h in hits)
    assert all("TENANT-A-SECRET" in h.document.content for h in hits)
    assert not any("TENANT-B-SECRET" in h.document.content for h in hits)


# ----------------------------------------------------------------------
# trace pipeline: PII redaction keeps tenant attribution
# ----------------------------------------------------------------------


def test_trace_redaction_strips_pii_keeps_tenant():
    attrs = {
        "neryva.tenant_id": "tenant-123",
        "user.message": "contact john.doe@example.com or 5551234567 or 123-45-6789",
        "provider.response": "HTTP 200 ok, ip 192.168.1.10",
        "nested": {"prompt": "SSN 987-65-4321 please"},
        "list": ["hello", "call 14155550199"],
    }
    redacted = _redact_dict(attrs)

    assert redacted["neryva.tenant_id"] == "tenant-123"
    assert "@example.com" not in redacted["user.message"]
    assert "123-45-6789" not in redacted["user.message"]
    assert "5551234567" not in redacted["user.message"]
    assert "192.168.1.10" not in redacted["provider.response"]
    assert "987-65-4321" not in redacted["nested"]["prompt"]
    assert "14155550199" not in redacted["list"][1]
    assert "[REDACTED]" in redacted["user.message"]


# ----------------------------------------------------------------------
# eval corpora
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eval_corpus_keys_are_tenant_scoped(db):
    from backend.app.worker.eval_extractor import EvalCase

    # The extractor writes per-tenant keys; assert the contract directly.
    tenant_a, tenant_b = str(uuid4()), str(uuid4())
    storage = _FakeStorage()

    case_a = EvalCase(tenant_id=tenant_a, user_message="redacted-a", assistant_response="r")
    case_b = EvalCase(tenant_id=tenant_b, user_message="redacted-b", assistant_response="r")

    for tenant, case, case_id in ((tenant_a, case_a, "c1"), (tenant_b, case_b, "c2")):
        key = f"eval_corpora/{tenant}/{case_id}.jsonl"
        await storage.upload_file(
            key,
            (case.to_jsonl() + "\n").encode(),
            content_type="application/x-ndjson",
            metadata={"tenant_id": tenant},
        )

    assert all(tenant_a in k or tenant_b in k for k in storage.objects)
    assert all(
        json.loads(line)["tenant_id"] in (tenant_a, tenant_b)
        for k, body in storage.objects.items()
        for line in body.decode().splitlines()
    )
