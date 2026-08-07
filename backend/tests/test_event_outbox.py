"""
P4-7 — Transactional outbox tests.

Covers the durable event hand-off added to Phase 4:
- ``EventOutboxRepository.record`` is idempotent by event id.
- The ``OutboxRelay`` drains pending rows and publishes each exactly once
  through the webhook publisher (delivery-keyed by event id).
- Ordering is preserved (oldest pending first).
- Crash-safety: a row left pending (crash between publish and ack) does
  not double-deliver on the next drain and never disappears.
- Worker handler is registered and drains end-to-end on the shared queue.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from backend.app.infrastructure.db.manager import DatabaseConfig, DatabaseManager
from backend.app.infrastructure.db.models import Base
from backend.app.infrastructure.db.repositories import WebhookRepository
from backend.app.modules.webhooks.outbox import EventOutboxRepository
from backend.app.modules.webhooks.publisher import EVENT_CONVERSATION_CREATED
from backend.app.modules.webhooks.relay import OutboxRelay, handle_outbox_relay
from backend.app.modules.webhooks import WebhookPublisher


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(
        DatabaseConfig(database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/outbox.db")
    )
    await manager.initialize()
    async with manager._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield manager
    await manager.close()


@pytest.mark.asyncio
async def test_record_is_idempotent_by_event_id(db):
    repo = EventOutboxRepository(db)
    first = await repo.record("evt-1", "t1", "conversation.created", {"a": 1})
    second = await repo.record("evt-1", "t1", "conversation.created", {"a": 1})
    assert second.get("idempotent_replay") is True
    assert await repo.count_pending() == 1


@pytest.mark.asyncio
async def test_pending_rows_oldest_first(db):
    repo = EventOutboxRepository(db)
    await repo.record("evt-1", "t1", "a.created", {"n": 1})
    await repo.record("evt-2", "t1", "b.created", {"n": 2})
    pending = await repo.list_pending()
    assert [r["event_id"] for r in pending] == ["evt-1", "evt-2"]


@pytest.mark.asyncio
async def test_relay_publishes_and_acks_each_event_once(db, monkeypatch):
    wh_repo = WebhookRepository(db)
    sub = await wh_repo.create_subscription(
        "t1", "https://hook.example/x", "secret-1234567890", [EVENT_CONVERSATION_CREATED]
    )
    outbox = EventOutboxRepository(db)
    await outbox.record("evt-1", "t1", EVENT_CONVERSATION_CREATED, {"session_id": "s1"})

    monkeypatch.setattr(
        "backend.app.modules.webhooks.publisher.get_queue_manager",
        lambda: AsyncMock(),
    )

    relay = OutboxRelay(db=db)
    published = await relay.drain(limit=10)

    assert published == 1
    event = await wh_repo.get_event("evt-1")
    assert event is not None
    assert event["event_type"] == EVENT_CONVERSATION_CREATED
    assert await outbox.count_pending() == 0


@pytest.mark.asyncio
async def test_relay_unqueued_when_no_matching_subscription(db, monkeypatch):
    outbox = EventOutboxRepository(db)
    await outbox.record("evt-2", "t2", EVENT_CONVERSATION_CREATED, {"session_id": "s2"})
    relay = OutboxRelay(db=db)
    published = await relay.drain(limit=10)
    assert published == 1
    assert await outbox.count_pending() == 0


@pytest.mark.asyncio
async def test_crash_after_publish_before_ack_is_exactly_once(db, monkeypatch):
    """Sid simulate the crash window: event fully published + webhook job
    recorded, but the outbox row never marked. The next drain re-runs the
    same event id; idempotency must prevent a duplicate webhook event."""
    wh_repo = WebhookRepository(db)
    await wh_repo.create_subscription(
        "t1", "https://hook.example/x", "secret-1234567890", [EVENT_CONVERSATION_CREATED]
    )
    outbox = EventOutboxRepository(db)
    await outbox.record("evt-1", "t1", EVENT_CONVERSATION_CREATED, {"session_id": "s"})

    publisher = WebhookPublisher(db=db, repository=wh_repo)
    monkeypatch.setattr(
        "backend.app.modules.webhooks.publisher.get_queue_manager",
        lambda: AsyncMock(),
    )
    first_eid = await publisher.publish(
        EVENT_CONVERSATION_CREATED,
        "t1",
        {"session_id": "s"},
        event_id="evt-1",
    )
    assert first_eid == "evt-1"
    assert await outbox.count_pending() == 1  # crash here: no ack

    relay = OutboxRelay(db=db)
    published = await relay.drain(limit=10)
    assert published == 1
    events = await wh_repo.list_events(event_type=EVENT_CONVERSATION_CREATED)
    assert len(events) == 1  # exactly once
    assert await outbox.count_pending() == 0


@pytest.mark.asyncio
async def test_worker_handler_drains_pending(db, monkeypatch):
    """The registered outbox.relay worker handler drains the durable rows."""
    outbox = EventOutboxRepository(db)
    await outbox.record("evt-1", "t1", EVENT_CONVERSATION_CREATED, {"session_id": "s"})

    relay = OutboxRelay(db=db)
    monkeypatch.setattr(
        "backend.app.modules.webhooks.relay.OutboxRelay",
        lambda *a, **k: relay,
    )
    await handle_outbox_relay({"limit": 10})
    assert await outbox.count_pending() == 0


@pytest.mark.asyncio
async def test_handler_registered_in_build(db, monkeypatch):
    from backend.app.worker.handlers import build_handlers, JOB_OUTBOX_RELAY

    manager = MagicMock()
    monkeypatch.setattr("backend.app.worker.handlers.get_queue_manager", lambda: manager)
    handlers = build_handlers()
    assert JOB_OUTBOX_RELAY in handlers
    assert callable(handlers[JOB_OUTBOX_RELAY])