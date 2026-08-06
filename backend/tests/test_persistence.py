"""
Tests for the repository layer against a file-backed SQLite database.
"""

from uuid import uuid4

import pytest

from backend.app.infrastructure.db import (
    ApiKeyRepository,
    AuditRepository,
    ConversationRepository,
    EscalationRepository,
    EvidenceRepository,
    PolicyRepository,
    TenantRepository,
    init_database,
)
from backend.app.infrastructure.db.models import Base


@pytest.fixture
async def db(tmp_path):
    manager = init_database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db")
    await manager.initialize()
    async with manager._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield manager
    await manager.close()


def _tenant_data(slug: str) -> dict:
    return {
        "id": str(uuid4()),
        "slug": slug,
        "name": f"Tenant {slug}",
        "allowed_topics": ["billing"],
        "blocked_topics": [],
        "escalation_threshold": 0.6,
        "knowledge_allowlist": [],
        "default_provider": "openai",
        "default_model": "gpt-4",
        "features": {},
        "guardrail_config": {},
        "guardrail_thresholds": {},
    }


@pytest.mark.asyncio
async def test_tenant_repository_crud(db):
    repo = TenantRepository(db)
    data = _tenant_data("acme")

    created = await repo.create(data)
    assert created["slug"] == "acme"

    by_slug = await repo.get_by_slug("acme")
    assert by_slug is not None and by_slug["id"] == data["id"]

    by_id = await repo.get_by_id(data["id"])
    assert by_id is not None and by_id["name"] == data["name"]

    updated = await repo.update(data["id"], {"escalation_threshold": 0.9})
    assert updated is not None and updated["escalation_threshold"] == 0.9

    listed = await repo.list_all()
    assert len(listed) == 1

    assert await repo.delete(data["id"]) is True
    assert await repo.get_by_id(data["id"]) is None


@pytest.mark.asyncio
async def test_conversation_repository_session_isolation(db):
    repo = ConversationRepository(db)
    tenant_id = str(uuid4())
    session_id = "session-1"

    conv1 = await repo.get_or_create(tenant_id, session_id)
    conv2 = await repo.get_or_create(tenant_id, session_id)
    assert conv1["id"] == conv2["id"]

    await repo.add_message(conv1["id"], "user", "hello")
    await repo.add_message(conv1["id"], "assistant", "hi there", metadata={"conf": 0.8})

    await repo.mark_escalated(conv1["id"])
    fetched = await repo.get_by_session(tenant_id, session_id)
    assert fetched is not None and fetched["is_escalated"] is True

    listed = await repo.list_by_tenant(tenant_id)
    assert len(listed) == 1
    assert await repo.list_by_tenant(str(uuid4())) == []


@pytest.mark.asyncio
async def test_evidence_and_audit_repositories(db):
    evidence = EvidenceRepository(db)
    await evidence.add({
        "tenant_id": str(uuid4()),
        "direction": "input",
        "decision": "ALLOW",
        "allowed": True,
        "input_hash": "a" * 64,
        "violations": [],
        "layers_evaluated": ["regex_fastpath"],
        "processing_time_ms": 1.5,
        "metadata": {},
    })

    audit = AuditRepository(db)
    event = await audit.add(
        action="tenant.created",
        resource_type="tenant",
        resource_id=str(uuid4()),
        actor_type="api_key",
        actor_id="key-1",
        details={"slug": "x"},
    )
    assert event["action"] == "tenant.created"
    events = await audit.list_events(limit=10)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_api_key_repository_lifecycle(db):
    repo = ApiKeyRepository(db)
    key_hash = "b" * 64

    record = await repo.create(
        name="test key", key_hash=key_hash, prefix="nrv_live_abc12345",
        role="operator",
    )
    assert record["revoked"] is False

    found = await repo.get_by_hash(key_hash)
    assert found is not None and found["name"] == "test key"
    assert await repo.get_by_hash("c" * 64) is None

    await repo.touch_usage(record["id"])
    found = await repo.get_by_hash(key_hash)
    assert found["usage_count"] == 1

    assert await repo.revoke(record["id"]) is True
    assert (await repo.get_by_hash(key_hash))["revoked"] is True
    assert await repo.count_active() == 0


@pytest.mark.asyncio
async def test_escalation_repository(db):
    repo = EscalationRepository(db)
    tenant_id = str(uuid4())
    record = await repo.add({
        "id": str(uuid4()),
        "tenant_id": tenant_id,
        "session_id": "s-1",
        "category": "general",
        "severity": "high",
        "status": "open",
        "reason": "low_confidence",
        "summary": "needs human",
        "details": {"confidence": 0.2},
        "channel": "generic",
    })
    assert record["status"] == "open"

    open_records = await repo.list_by_tenant(tenant_id, status="open")
    assert len(open_records) == 1
    assert await repo.list_by_tenant(tenant_id, status="resolved") == []
    assert len(await repo.list_all(status=None)) == 1


@pytest.mark.asyncio
async def test_policy_repository(db):
    repo = PolicyRepository(db)
    tenant_id = str(uuid4())

    await repo.create_set(tenant_id, "default", rules=[
        {"name": "block adult content", "policy_type": "topic_filter",
         "action": "block", "conditions": {"topic": "adult"}, "priority": 10},
    ])

    fetched = await repo.get_by_tenant(tenant_id)
    assert fetched is not None and fetched["name"] == "default"
    assert len(fetched["rules"]) == 1
    assert fetched["rules"][0]["action"] == "block"

    assert await repo.get_by_tenant(str(uuid4())) is None
