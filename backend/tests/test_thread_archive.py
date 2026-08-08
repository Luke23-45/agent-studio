"""
P1-7 — Cold-tier thread archive tests.

Threads older than the tenant's retention window move to region-pinned
object storage: full durable payload (thread, messages, parts, events),
then the ``archived`` marker flips so hot queries ignore the thread.
Restore pulls the payload back, verifies the checksum + thread id, and
clears the marker. Archive and restore are idempotent; a failed upload
never flips the marker.
"""

import asyncio
import json
from uuid import uuid4

import pytest

from backend.app.application.archive.service import ThreadArchiveService
from backend.app.infrastructure.db import ThreadRepository, TenantRepository
from backend.app.infrastructure.db.manager import DatabaseConfig, DatabaseManager
from backend.app.infrastructure.db.models import Base
from backend.app.infrastructure.db.threads import ThreadNotFoundError


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(
        DatabaseConfig(database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/archive.db")
    )

    async def _init():
        await manager.initialize()
        async with manager._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    yield manager
    asyncio.run(manager.close())


class _FakeStorage:
    """In-memory object store that preserves upload metadata."""

    def __init__(self):
        self.objects: dict[str, dict] = {}

    async def upload_file(self, key, content, content_type="application/json", metadata=None):
        self.objects[key] = {
            "content": content,
            "metadata": metadata or {},
        }
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
        obj = self.objects[key]
        from datetime import datetime
        import hashlib
        return obj["content"], _FileMeta(
            key=key,
            size=len(obj["content"]),
            content_type="",
            checksum_sha256=hashlib.sha256(obj["content"]).hexdigest(),
            uploaded_at="",
            metadata=obj["metadata"],
        )

    async def delete_file(self, key):
        return self.objects.pop(key, None) is not None

    async def list_files(self, prefix=""):
        from datetime import datetime
        import hashlib
        return [
            _FileMeta(
                key=k,
                size=len(v["content"]),
                content_type="",
                checksum_sha256=hashlib.sha256(v["content"]).hexdigest(),
                uploaded_at="",
                metadata=v["metadata"],
            )
            for k, v in self.objects.items()
            if k.startswith(prefix)
        ]


class _FileMeta:
    def __init__(self, **kwargs):
        self.key = kwargs["key"]
        self.size = kwargs["size"]
        self.content_type = kwargs["content_type"]
        self.checksum_sha256 = kwargs["checksum_sha256"]
        self.uploaded_at = kwargs["uploaded_at"]
        self.metadata = kwargs["metadata"]


async def _seed_thread(db, tenant_id, conversation_id, messages=2):
    repo = ThreadRepository(db)
    thread = await repo.create_thread(
        tenant_id, conversation_id=conversation_id, request_id=f"create:{uuid4()}"
    )
    for i in range(messages):
        await repo.append_message(
            tenant_id,
            thread["id"],
            role="user" if i % 2 == 0 else "assistant",
            content=f"message body {i}",
            redacted_content=f"message {i}",
            conversation_id=conversation_id,
            request_id=f"msg:{thread['id']}:{i}",
            extra_parts=[
                {
                    "part_type": "tool_result",
                    "content": {"tool_use_id": f"tool-{i}", "output": "x" * 10},
                    "redacted_content": {"tool_use_id": f"tool-{i}"},
                }
            ] if i == 1 else None,
        )
    return thread


async def _seed_tenant(db):
    tenant = await TenantRepository(db).create(
        {
            "id": str(uuid4()),
            "slug": f"arch-{uuid4().hex[:8]}",
            "name": "Archive Co",
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
    return tenant


@pytest.mark.asyncio
async def test_archive_round_trip(db):
    tenant = await _seed_tenant(db)
    thread = await _seed_thread(db, tenant["id"], "conv-1")
    storage = _FakeStorage()
    service = ThreadArchiveService(db, storage)

    result = await service.archive_thread(tenant["id"], thread["id"])

    assert result["archived"] is True
    assert result["key"] == f"tenant/{tenant['id']}/archive/eu-west-1/threads/{thread['id']}.json"
    assert result["bytes"] > 0
    assert any(thread["id"] in k for k in storage.objects)

    repo = ThreadRepository(db)
    hot = await repo.get_thread(tenant["id"], thread["id"])
    assert hot["archived"] is True

    events = await repo.list_events(tenant["id"], thread["id"])
    assert events[-1]["event_type"] == "thread.archive"

    payload = json.loads(storage.objects[result["key"]]["content"].decode("utf-8"))
    assert payload["thread"]["id"] == thread["id"]
    assert len(payload["messages"]) == 2
    assert len(payload["parts"]) == 3  # 2 text + 1 tool_result
    assert any(e["event_type"] == "message.appended" for e in payload["events"])


@pytest.mark.asyncio
async def test_archive_marks_region_pinned_key(db):
    tenant = await _seed_tenant(db)
    thread = await _seed_thread(db, tenant["id"], "conv-1")
    service = ThreadArchiveService(db, _FakeStorage())

    result = await service.archive_thread(tenant["id"], thread["id"], region="us-east-1")
    assert result["key"] == f"tenant/{tenant['id']}/archive/us-east-1/threads/{thread['id']}.json"


@pytest.mark.asyncio
async def test_archive_idempotent_no_second_upload(db):
    tenant = await _seed_tenant(db)
    thread = await _seed_thread(db, tenant["id"], "conv-1")
    storage = _FakeStorage()
    service = ThreadArchiveService(db, storage)

    first = await service.archive_thread(tenant["id"], thread["id"])
    second = await service.archive_thread(tenant["id"], thread["id"])

    assert second["already_archived"] is True
    assert first["key"] == second["key"]
    assert len(storage.objects) == 1  # no duplicate upload


@pytest.mark.asyncio
async def test_archive_unknown_thread_or_tenant(db):
    tenant = await _seed_tenant(db)
    thread = await _seed_thread(db, tenant["id"], "conv-1")
    storage = _FakeStorage()
    service = ThreadArchiveService(db, storage)

    with pytest.raises(ThreadNotFoundError):
        await service.archive_thread(tenant["id"], str(uuid4()))
    with pytest.raises(ThreadNotFoundError):
        await service.archive_thread(str(uuid4()), thread["id"])
    assert storage.objects == {}


@pytest.mark.asyncio
async def test_restore_round_trip(db):
    tenant = await _seed_tenant(db)
    thread = await _seed_thread(db, tenant["id"], "conv-1")
    storage = _FakeStorage()
    service = ThreadArchiveService(db, storage)
    archived = await service.archive_thread(tenant["id"], thread["id"])

    restored = await service.restore_thread(tenant["id"], thread["id"])

    assert restored["archived"] is False
    assert restored["key"] == archived["key"]
    assert restored["messages"] == 2
    repo = ThreadRepository(db)
    hot = await repo.get_thread(tenant["id"], thread["id"])
    assert hot["archived"] is False
    events = await repo.list_events(tenant["id"], thread["id"])
    assert events[-1]["event_type"] == "thread.restore"


@pytest.mark.asyncio
async def test_restore_rejects_corrupt_payload(db):
    tenant = await _seed_tenant(db)
    thread = await _seed_thread(db, tenant["id"], "conv-1")
    storage = _FakeStorage()
    service = ThreadArchiveService(db, storage)
    archived = await service.archive_thread(tenant["id"], thread["id"])

    # Corrupt the payload after upload (metadata preserved, body replaced).
    key = archived["key"]
    body = storage.objects[key]["content"]
    storage.objects[key]["content"] = body.replace(b"message body", b"corrupted!!!")

    with pytest.raises(ValueError):
        await service.restore_thread(tenant["id"], thread["id"])
    repo = ThreadRepository(db)
    assert (await repo.get_thread(tenant["id"], thread["id"]))["archived"] is True


@pytest.mark.asyncio
async def test_restore_idempotent_hot_thread(db):
    tenant = await _seed_tenant(db)
    thread = await _seed_thread(db, tenant["id"], "conv-1")
    service = ThreadArchiveService(db, _FakeStorage())

    result = await service.restore_thread(tenant["id"], thread["id"])
    assert result["already_restored"] is True


@pytest.mark.asyncio
async def test_archive_tenant_window_only_stale_threads(db):
    tenant = await _seed_tenant(db)
    repo = ThreadRepository(db)
    fresh = await _seed_thread(db, tenant["id"], "conv-1")
    stale = await _seed_thread(db, tenant["id"], "conv-1")
    # Make the stale thread old enough to qualify.
    from datetime import UTC, datetime, timedelta
    from sqlalchemy import update
    from backend.app.infrastructure.db.models import ThreadModel

    async with db.get_session() as session:
        await session.execute(
            update(ThreadModel)
            .where(ThreadModel.id == stale["id"])
            .values(created_at=datetime.now(UTC) - timedelta(days=400))
        )

    storage = _FakeStorage()
    service = ThreadArchiveService(db, storage)
    sweep = await service.archive_tenant_window(
        tenant["id"], older_than_days=365, limit=10
    )

    ids = {r["thread_id"] for r in sweep["archived"]}
    assert stale["id"] in ids
    assert fresh["id"] not in ids
    assert (await repo.get_thread(tenant["id"], stale["id"]))["archived"] is True
    assert (await repo.get_thread(tenant["id"], fresh["id"]))["archived"] is False


@pytest.mark.asyncio
async def test_archive_cross_tenant_isolated(db):
    tenant_a = await _seed_tenant(db)
    tenant_b = await _seed_tenant(db)
    thread_a = await _seed_thread(db, tenant_a["id"], "conv-1")
    await _seed_thread(db, tenant_b["id"], "conv-1")
    storage = _FakeStorage()
    service = ThreadArchiveService(db, storage)

    result = await service.archive_thread(tenant_a["id"], thread_a["id"])

    assert result["key"].startswith(f"tenant/{tenant_a['id']}/archive/")
    assert all(k.startswith(f"tenant/{tenant_a['id']}/") for k in storage.objects)
