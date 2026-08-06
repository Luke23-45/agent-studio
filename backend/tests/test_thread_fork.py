"""
P1-5 fork tests:

- fork at a boundary copies history (seq, parts, parent chains remapped)
- original thread is immutable apart from its appended thread.forked event
- both sides resume independently afterwards
- fork at 0 yields an empty thread; beyond the end is rejected
- cross-tenant fork is rejected
"""

from uuid import uuid4

import pytest

from backend.app.infrastructure.db import (
    ConversationRepository,
    ThreadRepository,
    init_database,
)
from backend.app.infrastructure.db.models import Base
from backend.app.infrastructure.db.threads import ThreadNotFoundError
from backend.app.session.service import InvalidTargetError, ThreadMutationService


@pytest.fixture
async def db(tmp_path):
    manager = init_database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db")
    await manager.initialize()
    async with manager._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield manager
    await manager.close()


async def _thread(db, tenant_id="t1") -> dict:
    conv = await ConversationRepository(db).get_or_create(tenant_id, f"session-{uuid4()}")
    return await ThreadRepository(db).create_thread(
        tenant_id, conversation_id=conv["id"]
    )


async def _three_turns(db, thread) -> list[dict]:
    repo = ThreadRepository(db)
    tenant = thread["tenant_id"]
    msgs = []
    parent = None
    for i, role in enumerate(["user", "assistant", "user"]):
        msg = await repo.append_message(
            tenant, thread["id"], role=role, content=f"m{i + 1}",
            conversation_id=thread["conversation_id"], parent_message_id=parent,
        )
        msgs.append(msg)
        parent = msg["id"]
    return msgs


@pytest.mark.asyncio
async def test_fork_copies_history_with_remapped_chain(db):
    thread = await _thread(db)
    msgs = await _three_turns(db, thread)  # seq 1,2,3

    result = await ThreadMutationService(db).fork("t1", thread["id"], at_seq=3)
    new = result["thread"]
    assert new["id"] != thread["id"]
    assert new["conversation_id"] != thread["conversation_id"]

    repo = ThreadRepository(db)
    copied = await repo.list_messages("t1", new["id"])
    assert copied["has_more"] is False
    assert [m["seq"] for m in copied["messages"]] == [1, 2, 3]
    assert [m["content"] for m in copied["messages"]] == ["m1", "m2", "m3"]
    assert [m["id"] for m in copied["messages"]] != [m["id"] for m in msgs]

    by_id = {m["id"]: m for m in copied["messages"]}
    parent_chain = by_id[copied["messages"][1]["id"]]["parent_message_id"]
    assert parent_chain == copied["messages"][0]["id"]

    for m in copied["messages"]:
        assert m["metadata_json"]["forked_from"] == thread["id"]
        assert m["metadata_json"]["forked_at_seq"] == 3

    parts = await repo.list_parts("t1", copied["messages"][0]["id"])
    assert parts[0]["content"] == {"text": "m1"}

    events = await repo.list_events("t1", new["id"])
    assert [e["event_type"] for e in events] == ["thread.created", "thread.forked"]


@pytest.mark.asyncio
async def test_source_thread_unchanged_but_marks_fork(db):
    thread = await _thread(db)
    msgs = await _three_turns(db, thread)

    result = await ThreadMutationService(db).fork("t1", thread["id"], at_seq=2)

    repo = ThreadRepository(db)
    source = await repo.list_messages("t1", thread["id"])
    assert [m["id"] for m in source["messages"]] == [m["id"] for m in msgs]
    assert all(m["metadata_json"] == {} for m in source["messages"])

    events = await repo.list_events("t1", thread["id"])
    fork_events = [e for e in events if e["event_type"] == "thread.forked"]
    assert len(fork_events) == 1
    assert fork_events[0]["payload"] == {
        "new_thread_id": result["new_thread_id"],
        "at_seq": 2,
    }


@pytest.mark.asyncio
async def test_both_sides_resume_independently(db):
    thread = await _thread(db)
    msgs = await _three_turns(db, thread)
    result = await ThreadMutationService(db).fork("t1", thread["id"], at_seq=2)
    new = result["thread"]

    repo = ThreadRepository(db)
    await repo.append_message(
        "t1", thread["id"], role="assistant", content="source continues",
        conversation_id=thread["conversation_id"], parent_message_id=msgs[-1]["id"],
    )
    await repo.append_message(
        "t1", new["id"], role="assistant", content="fork continues",
        conversation_id=new["conversation_id"],
        parent_message_id=(await repo.list_messages("t1", new["id"]))["messages"][-1]["id"],
    )

    source_tail = await repo.read_tail("t1", thread["id"], limit=1)
    fork_tail = await repo.read_tail("t1", new["id"], limit=1)
    assert source_tail[-1]["content"] == "source continues"
    assert fork_tail[-1]["content"] == "fork continues"
    assert source_tail[-1]["seq"] == 4
    assert fork_tail[-1]["seq"] == 3


@pytest.mark.asyncio
async def test_fork_at_zero_is_empty_and_beyond_end_rejected(db):
    thread = await _thread(db)
    await _three_turns(db, thread)
    service = ThreadMutationService(db)

    empty = await service.fork("t1", thread["id"], at_seq=0)
    page = await ThreadRepository(db).list_messages("t1", empty["new_thread_id"])
    assert page["messages"] == []

    with pytest.raises(InvalidTargetError):
        await service.fork("t1", thread["id"], at_seq=4)


@pytest.mark.asyncio
async def test_cross_tenant_fork_rejected(db):
    thread = await _thread(db, "tenant-a")
    await _three_turns(db, thread)

    with pytest.raises(ThreadNotFoundError):
        await ThreadMutationService(db).fork("tenant-b", thread["id"], at_seq=1)
