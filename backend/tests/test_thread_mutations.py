"""
P1-4 append-only regeneration/editing tests:

- regenerate appends an assistant message parented to the original;
  the original is never mutated
- edit appends a new user message + regenerated successor chain
- audit rows show both attempts (original + replacement)
- invalid targets (wrong role / empty content) and cross-tenant access
  are rejected
"""

from uuid import uuid4

import pytest

from backend.app.infrastructure.db import (
    AuditRepository,
    ConversationRepository,
    ThreadRepository,
    init_database,
)
from backend.app.infrastructure.db.models import Base
from backend.app.infrastructure.db.threads import (
    MessageNotFoundError,
    ThreadNotFoundError,
)
from backend.app.session.service import InvalidTargetError, ThreadMutationService


@pytest.fixture
async def db(tmp_path):
    manager = init_database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db")
    await manager.initialize()
    async with manager._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield manager
    await manager.close()


async def _thread(db, tenant_id="t1", end_user_id=None) -> dict:
    conv = await ConversationRepository(db).get_or_create(tenant_id, f"session-{uuid4()}")
    return await ThreadRepository(db).create_thread(
        tenant_id,
        conversation_id=conv["id"],
        end_user_id=end_user_id,
    )


async def _user_turn(db, thread, text="hello") -> dict:
    return await ThreadRepository(db).append_message(
        thread["tenant_id"] if "tenant_id" in thread else "t1",
        thread["id"],
        role="user",
        content=text,
        conversation_id=thread["conversation_id"],
    )


async def _assistant_turn(db, thread, user_msg, text="reply", parent=None) -> dict:
    return await ThreadRepository(db).append_message(
        thread["tenant_id"] if "tenant_id" in thread else "t1",
        thread["id"],
        role="assistant",
        content=text,
        conversation_id=thread["conversation_id"],
        parent_message_id=parent or user_msg["id"],
    )


@pytest.mark.asyncio
async def test_regenerate_chains_to_original_and_keeps_it_immutable(db):
    thread = await _thread(db)
    repo = ThreadRepository(db)
    user = await _user_turn(db, thread)
    original = await _assistant_turn(db, thread, user, text="first answer")

    result = await ThreadMutationService(db).regenerate(
        "t1", thread["id"], original["id"]
    )

    assert result["new_message_id"] != original["id"]
    new_msg = result["message"]
    assert new_msg["role"] == "assistant"
    assert new_msg["parent_message_id"] == original["id"]
    assert new_msg["metadata_json"].get("status") == "pending_generation"
    assert new_msg["metadata_json"].get("regenerated_from") == original["id"]
    assert new_msg["seq"] > original["seq"]

    untouched = await repo.get_message("t1", original["id"])
    assert untouched["content"] == "first answer"
    assert untouched["metadata_json"] == {}

    events = await repo.list_events("t1", thread["id"])
    assert "message.regenerated" in [e["event_type"] for e in events]
    regenerated = next(
        e for e in events if e["event_type"] == "message.regenerated"
    )
    assert regenerated["payload"] == {
        "original_message_id": original["id"],
        "new_message_id": new_msg["id"],
    }


@pytest.mark.asyncio
async def test_edit_appends_new_user_and_successor_chain(db):
    thread = await _thread(db)
    repo = ThreadRepository(db)
    user = await _user_turn(db, thread, text="old question")
    original_reply = await _assistant_turn(db, thread, user)

    result = await ThreadMutationService(db).edit(
        "t1", thread["id"], user["id"], "new question", "new question"
    )

    new_user = await repo.get_message("t1", result["new_user_message_id"])
    assert new_user["content"] == "new question"
    assert new_user["parent_message_id"] == user["parent_message_id"]
    assert new_user["metadata_json"].get("edited_from") == user["id"]

    successor = await repo.get_message("t1", result["new_assistant_message_id"])
    assert successor["parent_message_id"] == new_user["id"]
    assert successor["metadata_json"].get("status") == "pending_generation"

    old = await repo.get_message("t1", user["id"])
    assert old["content"] == "old question"
    assert old["metadata_json"] == {}
    untouched_reply = await repo.get_message("t1", original_reply["id"])
    assert untouched_reply["content"] == "reply"

    events = await repo.list_events("t1", thread["id"])
    assert "message.edited" in [e["event_type"] for e in events]


@pytest.mark.asyncio
async def test_audit_shows_both_attempts(db):
    thread = await _thread(db)
    repo = ThreadRepository(db)
    user = await _user_turn(db, thread)
    original = await _assistant_turn(db, thread, user)

    service = ThreadMutationService(db)
    regen = await service.regenerate("t1", thread["id"], original["id"])
    await service.edit("t1", thread["id"], user["id"], "edited text")

    rows = await AuditRepository(db).list_events("t1", limit=50)
    actions = {r["action"] for r in rows}
    assert {"thread.regenerated", "thread.message_edited"} <= actions
    details = next(r for r in rows if r["action"] == "thread.regenerated")["details"]
    assert details["original_message_id"] == original["id"]
    assert details["new_message_id"] == regen["new_message_id"]


@pytest.mark.asyncio
async def test_invalid_targets_rejected(db):
    thread = await _thread(db)
    repo = ThreadRepository(db)
    user = await _user_turn(db, thread)
    assistant = await _assistant_turn(db, thread, user)

    service = ThreadMutationService(db)

    with pytest.raises(InvalidTargetError):
        await service.regenerate("t1", thread["id"], user["id"])  # user, not assistant

    with pytest.raises(InvalidTargetError):
        await service.edit("t1", thread["id"], assistant["id"], "text")  # not user

    with pytest.raises(InvalidTargetError):
        await service.edit("t1", thread["id"], user["id"], "   ")  # empty content

    with pytest.raises(MessageNotFoundError):
        await service.regenerate("t1", thread["id"], str(uuid4()))

    with pytest.raises(ThreadNotFoundError):
        await service.regenerate("t1", str(uuid4()), user["id"])


@pytest.mark.asyncio
async def test_cross_tenant_access_rejected(db):
    thread_a = await _thread(db, "tenant-a")
    repo = ThreadRepository(db)
    user = await _user_turn(db, thread_a)
    assistant = await _assistant_turn(db, thread_a, user)

    service = ThreadMutationService(db)
    with pytest.raises(ThreadNotFoundError):
        await service.regenerate("tenant-b", thread_a["id"], assistant["id"])
    with pytest.raises(MessageNotFoundError):
        await service.edit("tenant-b", thread_a["id"], user["id"], "text")

    rows_after = await ThreadRepository(db).list_events("tenant-a", thread_a["id"])
    assert all(e["event_type"] in {"thread.created", "message.appended"} for e in rows_after)
