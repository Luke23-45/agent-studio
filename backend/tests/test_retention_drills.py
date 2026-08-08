"""
P6-8 — Retention sweep drills (end-to-end).

Drives ``handle_retention_run`` against a real SQLite database and a fake
object store: thread archival at the thread window, spend/evidence purge at
their windows, per-tenant retention override, audit-chain survival,
dry-run safety, and cross-tenant isolation.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import update

from backend.app.worker.retention import DEFAULT_RETENTION, handle_retention_run

from backend.app.infrastructure.db import (
    AuditRepository,
    EvidenceRepository,
    SessionTokenRepository,
    SpendEventRepository,
    TenantRepository,
    ThreadRepository,
)
from backend.app.infrastructure.db.manager import DatabaseConfig, DatabaseManager
from backend.app.infrastructure.db.models import (
    Base,
    GuardrailEvidenceModel,
    SpendEventModel,
    ThreadModel,
)


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(
        DatabaseConfig(database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/retention.db")
    )

    async def _init():
        await manager.initialize()
        async with manager._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    yield manager
    asyncio.run(manager.close())


class _FileMeta:
    def __init__(self, **kwargs):
        self.key = kwargs["key"]
        self.size = kwargs["size"]
        self.content_type = kwargs["content_type"]
        self.checksum_sha256 = kwargs["checksum_sha256"]
        self.uploaded_at = kwargs["uploaded_at"]
        self.metadata = kwargs["metadata"]


class _FakeStorage:
    def __init__(self):
        self.objects: dict[str, dict] = {}

    async def upload_file(self, key, content, content_type="application/json", metadata=None):
        import hashlib

        self.objects[key] = {"content": content, "metadata": metadata or {}}
        return _FileMeta(
            key=key,
            size=len(content),
            content_type=content_type,
            checksum_sha256=hashlib.sha256(content).hexdigest(),
            uploaded_at=datetime.now(timezone.utc).isoformat(),
            metadata=metadata or {},
        )

    async def download_file(self, key):
        if key not in self.objects:
            raise FileNotFoundError(key)
        obj = self.objects[key]
        return obj["content"], _FileMeta(
            key=key,
            size=len(obj["content"]),
            content_type="",
            checksum_sha256="",
            uploaded_at="",
            metadata=obj["metadata"],
        )

    async def delete_file(self, key):
        return self.objects.pop(key, None) is not None

    async def list_files(self, prefix=""):
        return [k for k in self.objects if k.startswith(prefix)]


async def _seed_tenant(db, *, retention_days=None):
    data = {
        "id": str(uuid4()),
        "slug": f"ret-{uuid4().hex[:8]}",
        "name": "Retention Co",
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
    if retention_days is not None:
        data["retention_days"] = retention_days
    return await TenantRepository(db).create(data)


async def _seed_thread(db, tenant_id, *, created_at):
    repo = ThreadRepository(db)
    thread = await repo.create_thread(
        tenant_id, conversation_id=f"convo-{uuid4()}", request_id=f"create:{uuid4()}"
    )
    await repo.append_message(
        tenant_id,
        thread["id"],
        role="user",
        content="body",
        redacted_content="redacted",
        conversation_id=thread["conversation_id"],
        request_id=f"msg:{thread['id']}",
    )
    async with db.get_session() as session:
        await session.execute(
            update(ThreadModel)
            .where(ThreadModel.id == thread["id"])
            .values(created_at=created_at)
        )
    return thread


async def _seed_spend(db, tenant_id, *, created_at, model="gpt-4o"):
    event = await SpendEventRepository(db).add(
        {
            "tenant_id": tenant_id,
            "conversation_id": f"convo-{uuid4()}",
            "provider": "openai",
            "model": model,
            "input_tokens": 10,
            "output_tokens": 5,
            "usd": 0.001,
            "request_id": f"req-{uuid4()}",
        }
    )
    async with db.get_session() as session:
        await session.execute(
            update(SpendEventModel)
            .where(SpendEventModel.id == event["id"])
            .values(created_at=created_at)
        )
    return event


async def _seed_evidence(db, tenant_id, *, created_at):
    evidence = EvidenceRepository(db)
    await evidence.add(
        {
            "tenant_id": tenant_id,
            "conversation_id": f"convo-{uuid4()}",
            "session_id": f"sess-{uuid4()}",
            "direction": "outbound",
            "decision": "allowed",
            "allowed": True,
            "input_hash": f"h-{uuid4().hex}",
            "violations": [],
            "layers_evaluated": ["r1"],
            "processing_time_ms": 5,
        }
    )
    rows = await evidence.list_by_tenant(tenant_id)
    row = rows[0]
    async with db.get_session() as session:
        await session.execute(
            update(GuardrailEvidenceModel)
            .where(GuardrailEvidenceModel.id == row["id"])
            .values(created_at=created_at)
        )
    return row


def _days(n):
    return datetime.now(timezone.utc) - timedelta(days=n)


@pytest.mark.asyncio
async def test_sweep_archives_and_purges_within_windows(db, monkeypatch):
    from backend.app.infrastructure import db as db_module
    from backend.app.infrastructure import storage as storage_module

    storage = _FakeStorage()
    monkeypatch.setattr(storage_module, "get_storage_manager", lambda: storage)
    monkeypatch.setattr(db_module, "get_database_manager", lambda: db)

    tenant = await _seed_tenant(db)  # default posture: 90d threads
    old_thread = await _seed_thread(db, tenant["id"], created_at=_days(120))
    new_thread = await _seed_thread(db, tenant["id"], created_at=_days(1))
    old_spend = await _seed_spend(db, tenant["id"], created_at=_days(400))
    new_spend = await _seed_spend(db, tenant["id"], created_at=_days(1))
    old_evidence = await _seed_evidence(db, tenant["id"], created_at=_days(800))
    new_evidence = await _seed_evidence(db, tenant["id"], created_at=_days(1))
    audit = AuditRepository(db)
    audit_event = await audit.add(
        "retention.drill",
        "thread",
        resource_id=old_thread["id"],
        tenant_id=tenant["id"],
    )

    await handle_retention_run({"tenant_id": tenant["id"]})

    threads = ThreadRepository(db)
    assert (await threads.get_thread(tenant["id"], old_thread["id"]))["archived"] is True
    assert (await threads.get_thread(tenant["id"], new_thread["id"]))["archived"] is False

    storage = storage_module.get_storage_manager()
    keys = await storage.list_files()
    assert any(old_thread["id"] in k for k in keys)
    assert not any(new_thread["id"] in k for k in keys)

    spend_ids = {e["id"] for e in await SpendEventRepository(db).list_filtered(tenant["id"])}
    assert old_spend["id"] not in spend_ids
    assert new_spend["id"] in spend_ids

    evidence_ids = {
        e["id"] for e in await EvidenceRepository(db).list_by_tenant(tenant["id"])
    }
    assert old_evidence["id"] not in evidence_ids
    assert new_evidence["id"] in evidence_ids

    remaining = await audit.list_events(tenant_id=tenant["id"])
    assert any(e["id"] == audit_event["id"] for e in remaining)
    ok, _ = await audit.verify_chain(tenant_id=tenant["id"])
    assert ok, "audit hash chain must survive the retention sweep intact"


@pytest.mark.asyncio
async def test_per_tenant_retention_override(db, monkeypatch):
    from backend.app.infrastructure import db as db_module
    from backend.app.infrastructure import storage as storage_module

    storage = _FakeStorage()
    monkeypatch.setattr(storage_module, "get_storage_manager", lambda: storage)
    monkeypatch.setattr(db_module, "get_database_manager", lambda: db)

    tenant = await _seed_tenant(db, retention_days=15)
    twenty_day_thread = await _seed_thread(db, tenant["id"], created_at=_days(20))
    await _seed_thread(db, tenant["id"], created_at=_days(1))

    await handle_retention_run({"tenant_id": tenant["id"]})

    keys = await storage.list_files()
    assert any(
        twenty_day_thread["id"] in k for k in keys
    ), "tenant retention_days=15 must archive a 20-day-old thread"


@pytest.mark.asyncio
async def test_dry_run_changes_nothing(db, monkeypatch):
    from backend.app.infrastructure import db as db_module
    from backend.app.infrastructure import storage as storage_module

    storage = _FakeStorage()
    monkeypatch.setattr(storage_module, "get_storage_manager", lambda: storage)
    monkeypatch.setattr(db_module, "get_database_manager", lambda: db)

    tenant = await _seed_tenant(db)
    old_thread = await _seed_thread(db, tenant["id"], created_at=_days(120))
    old_spend = await _seed_spend(db, tenant["id"], created_at=_days(400))
    old_evidence = await _seed_evidence(db, tenant["id"], created_at=_days(800))

    await handle_retention_run({"tenant_id": tenant["id"], "dry_run": True})

    assert await storage.list_files() == []
    threads = ThreadRepository(db)
    assert (await threads.get_thread(tenant["id"], old_thread["id"]))["archived"] is False
    spend_ids = {e["id"] for e in await SpendEventRepository(db).list_filtered(tenant["id"])}
    assert old_spend["id"] in spend_ids
    evidence_ids = {
        e["id"] for e in await EvidenceRepository(db).list_by_tenant(tenant["id"])
    }
    assert old_evidence["id"] in evidence_ids


@pytest.mark.asyncio
async def test_sweep_isolated_per_tenant(db, monkeypatch):
    from backend.app.infrastructure import db as db_module
    from backend.app.infrastructure import storage as storage_module

    storage = _FakeStorage()
    monkeypatch.setattr(storage_module, "get_storage_manager", lambda: storage)
    monkeypatch.setattr(db_module, "get_database_manager", lambda: db)

    tenant_a = await _seed_tenant(db)
    tenant_b = await _seed_tenant(db)
    old_a = await _seed_thread(db, tenant_a["id"], created_at=_days(120))
    old_b = await _seed_thread(db, tenant_b["id"], created_at=_days(120))
    spend_b = await _seed_spend(db, tenant_b["id"], created_at=_days(400))

    await handle_retention_run({"tenant_id": tenant_a["id"]})

    keys = await storage.list_files()
    assert any(old_a["id"] in k for k in keys)
    assert not any(old_b["id"] in k for k in keys)
    spend_ids = {e["id"] for e in await SpendEventRepository(db).list_filtered(tenant_b["id"])}
    assert spend_b["id"] in spend_ids


@pytest.mark.asyncio
async def test_policy_windows_match_documented_defaults(db):
    assert DEFAULT_RETENTION.thread_retention_days == 90
    assert DEFAULT_RETENTION.spend_retention_days == 365
    assert DEFAULT_RETENTION.evidence_retention_days == 730
    assert DEFAULT_RETENTION.audit_retention_days == 730
    assert SessionTokenRepository  # token pruning still part of the sweep surface
