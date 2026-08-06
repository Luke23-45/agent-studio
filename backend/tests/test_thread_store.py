"""
P1-1 schema constraints + P1-2 thread repository tests:

- unique (thread_id, seq) on messages and thread_events
- unique request_id -> retried append never double-writes
- atomic append: message + text part + event in one transaction
- cursor pagination (after_seq, limit, has_more)
- cross-tenant isolation (every query tenant-scoped)
- extra parts (tool_use etc.) written in order after the text part
"""

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from backend.app.infrastructure.db import ConversationRepository, ThreadRepository, init_database
from backend.app.infrastructure.db.models import (
    Base,
    MessageModel,
    ThreadEventModel,
)


@pytest.fixture
async def db(tmp_path):
    manager = init_database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db")
    await manager.initialize()
    async with manager._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield manager
    await manager.close()


async def _thread(db, tenant_id, surface_id=None, end_user_id=None) -> dict:
    conv = await ConversationRepository(db).get_or_create(tenant_id, f"session-{uuid4()}")
    return await ThreadRepository(db).create_thread(
        tenant_id,
        conversation_id=conv["id"],
        surface_id=surface_id,
        end_user_id=end_user_id,
    )


# ---- P1-1: schema constraints -------------------------------------------


@pytest.mark.asyncio
async def test_unique_thread_seq_violation_raises(db):
    thread = await _thread(db, "t1")
    thread_id, tenant_id = thread["id"], "t1"

    async with db.get_session() as session:
        session.add(
            MessageModel(
                id=str(uuid4()), conversation_id=thread["conversation_id"],
                tenant_id=tenant_id, thread_id=thread_id, seq=1,
                role="user", content="a", redacted_content="a",
            )
        )
        session.add(
            MessageModel(
                id=str(uuid4()), conversation_id=thread["conversation_id"],
                tenant_id=tenant_id, thread_id=thread_id, seq=1,
                role="user", content="b", redacted_content="b",
            )
        )
        with pytest.raises(Exception):  # IntegrityError
            await session.flush()
        await session.rollback()


@pytest.mark.asyncio
async def test_unique_event_seq_violation_raises(db):
    thread = await _thread(db, "t1")

    async with db.get_session() as session:
        session.add(
            ThreadEventModel(
                id=str(uuid4()), thread_id=thread["id"], tenant_id="t1",
                seq=1, event_type="x", payload={},
            )
        )
        session.add(
            ThreadEventModel(
                id=str(uuid4()), thread_id=thread["id"], tenant_id="t1",
                seq=1, event_type="y", payload={},
            )
        )
        with pytest.raises(Exception):  # IntegrityError
            await session.flush()
        await session.rollback()


# ---- P1-2: append path ----------------------------------------------------


@pytest.mark.asyncio
async def test_append_message_writes_message_parts_and_event(db):
    thread = await _thread(db, "t1")
    repo = ThreadRepository(db)

    msg = await repo.append_message(
        "t1", thread["id"],
        role="user",
        content="Hello",
        redacted_content="Hello",
        conversation_id=thread["conversation_id"],
    )
    assert msg["seq"] == 1
    assert msg["deduped"] is False

    parts = await repo.list_parts("t1", msg["id"])
    assert len(parts) == 1
    assert parts[0]["part_type"] == "text"
    assert parts[0]["part_index"] == 0
    assert parts[0]["content"] == {"text": "Hello"}

    events = await repo.list_events("t1", thread["id"])
    assert [e["event_type"] for e in events] == ["thread.created", "message.appended"]


@pytest.mark.asyncio
async def test_seq_increments_across_appends(db):
    thread = await _thread(db, "t1")
    repo = ThreadRepository(db)
    first = await repo.append_message(
        "t1", thread["id"], role="user", content="a",
        conversation_id=thread["conversation_id"],
    )
    second = await repo.append_message(
        "t1", thread["id"], role="assistant", content="b",
        conversation_id=thread["conversation_id"],
        parent_message_id=first["id"],
    )
    assert (first["seq"], second["seq"]) == (1, 2)
    assert second["parent_message_id"] == first["id"]


@pytest.mark.asyncio
async def test_request_id_dedup_returns_original(db):
    thread = await _thread(db, "t1")
    repo = ThreadRepository(db)

    original = await repo.append_message(
        "t1", thread["id"], role="user", content="pay me",
        conversation_id=thread["conversation_id"], request_id="req-1",
    )
    retry = await repo.append_message(
        "t1", thread["id"], role="user", content="pay me",
        conversation_id=thread["conversation_id"], request_id="req-1",
    )
    assert retry["deduped"] is True
    assert retry["id"] == original["id"]
    assert retry["seq"] == original["seq"]

    page = await repo.list_messages("t1", thread["id"])
    assert len(page["messages"]) == 1


@pytest.mark.asyncio
async def test_extra_parts_written_in_order(db):
    thread = await _thread(db, "t1")
    repo = ThreadRepository(db)
    msg = await repo.append_message(
        "t1", thread["id"], role="assistant", content="thinking...",
        conversation_id=thread["conversation_id"],
        extra_parts=[
            {"part_type": "reasoning", "content": {"text": "hidden"}},
            {"part_type": "tool_use", "content": {"name": "search", "args": {}}},
        ],
    )
    parts = await repo.list_parts("t1", msg["id"])
    assert [p["part_type"] for p in parts] == ["text", "reasoning", "tool_use"]
    assert [p["part_index"] for p in parts] == [0, 1, 2]


# ---- P1-2: read paths -----------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_pagination_ordering(db):
    thread = await _thread(db, "t1")
    repo = ThreadRepository(db)
    for i in range(5):
        await repo.append_message(
            "t1", thread["id"], role="user", content=f"msg-{i}",
            conversation_id=thread["conversation_id"],
        )

    page1 = await repo.list_messages("t1", thread["id"], limit=2)
    assert len(page1["messages"]) == 2
    assert page1["has_more"] is True
    assert [m["seq"] for m in page1["messages"]] == [1, 2]

    last_seq = page1["messages"][-1]["seq"]
    page2 = await repo.list_messages("t1", thread["id"], after_seq=last_seq, limit=2)
    assert [m["seq"] for m in page2["messages"]] == [3, 4]
    assert page2["has_more"] is True

    page3 = await repo.list_messages("t1", thread["id"], after_seq=4, limit=2)
    assert [m["seq"] for m in page3["messages"]] == [5]
    assert page3["has_more"] is False


@pytest.mark.asyncio
async def test_read_tail_newest_window(db):
    thread = await _thread(db, "t1")
    repo = ThreadRepository(db)
    for i in range(5):
        await repo.append_message(
            "t1", thread["id"], role="user", content=f"msg-{i}",
            conversation_id=thread["conversation_id"],
        )
    tail = await repo.read_tail("t1", thread["id"], limit=3)
    assert [m["seq"] for m in tail] == [3, 4, 5]
    bounded = await repo.read_tail("t1", thread["id"], from_seq=4, limit=10)
    assert [m["seq"] for m in bounded] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_cross_tenant_isolation(db):
    repo = ThreadRepository(db)
    thread_a = await _thread(db, "tenant-a")
    thread_b = await _thread(db, "tenant-b")

    await repo.append_message(
        "tenant-a", thread_a["id"], role="user", content="secret-a",
        conversation_id=thread_a["conversation_id"],
    )
    await repo.append_message(
        "tenant-b", thread_b["id"], role="user", content="secret-b",
        conversation_id=thread_b["conversation_id"],
    )

    assert await repo.get_thread("tenant-a", thread_b["id"]) is None
    page = await repo.list_messages("tenant-b", thread_a["id"])
    assert page["messages"] == []
    assert await repo.get_message("tenant-a", (await repo.list_messages("tenant-b", thread_b["id"]))["messages"][0]["id"]) is None
