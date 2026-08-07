"""
P4-2 — Stream buffer Postgres overflow tier.

The hot tier (Redis list, or process-memory fallback here) is capped per
stream via ``overflow_max_chunks``; the oldest chunks past the cap are
persisted to the durable overflow tier instead of dropped, and ``replay``
merges both by event id so a reconnect replays the identical ordered
bytes — including after a process restart that landed on a fresh buffer
with only the overflow tier available.
"""

import pytest

from backend.app.infrastructure.db.manager import DatabaseConfig, DatabaseManager
from backend.app.infrastructure.db.models import Base
from backend.app.infrastructure.stream.buffer import StreamBuffer
from backend.app.infrastructure.stream.overflow import StreamBufferOverflowRepository


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(
        DatabaseConfig(database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/overflow.db")
    )
    await manager.initialize()
    async with manager._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield manager
    await manager.close()


def _buf(db, cap):
    """StreamBuffer without touching Redis: overflow repo wired directly,
    redis_available stays False, so streams use the process-memory fallback."""
    buf = StreamBuffer(db=db, overflow_max_chunks=cap)
    buf._overflow = StreamBufferOverflowRepository(db)
    return buf


@pytest.mark.asyncio
async def test_cap_spills_oldest_to_overflow_and_replay_merges(db):
    """Ingress past ``overflow_max_chunks`` keeps only the newest in the hot
    tier; the oldest overflow to Postgres and replay returns all chunks."""
    buf = _buf(db, cap=3)
    ids = {}
    for content in ("one", "two", "three", "four", "five"):
        ids[content] = await buf.append(
            "t1", "th-1", "s-over", "delta", {"content": content}
        )

    # Hot tier bounded at 3; oldest two overflowed.
    hot = await buf.replay("t1", "th-1", "s-over", after_event_id=0)
    assert len(hot) == 5
    assert [r["data"]["content"] for r in hot] == ["one", "two", "three", "four", "five"]
    assert [r["id"] for r in hot] == [ids[c] for c in ("one", "two", "three", "four", "five")]

    # The overflow tier holds exactly the oldest two.
    overflowed = await buf._overflow.read_chunks(
        tenant_id="t1", thread_id="th-1", stream_id="s-over"
    )
    assert [r["data"]["content"] for r in overflowed] == ["one", "two"]


@pytest.mark.asyncio
async def test_no_cap_unbounded_hot_tier(db):
    """overflow_max_chunks=0 keeps the pre-P4-2 behavior: no overflow."""
    buf = _buf(db, cap=0)
    for content in ("one", "two", "three"):
        await buf.append("t", "th-1", "s-nocap", "delta", {"content": content})
    records = await buf.replay("t", "th-1", "s-nocap", after_event_id=0)
    assert [r["data"]["content"] for r in records] == ["one", "two", "three"]
    assert len(buf._memory["neryva:stream:buffer:t:th-1:s-nocap"]["entries"]) == 3


@pytest.mark.asyncio
async def test_overflow_survives_buffer_restart(db):
    """A fresh buffer against the same DB (simulating a process restart that
    had no hot tier) replays the overflowed chunks held durably."""
    buf1 = _buf(db, cap=2)
    await buf1.append("t", "th-1", "s-re", "delta", {"content": "first"})
    await buf1.append("t", "th-1", "s-re", "delta", {"content": "second"})
    await buf1.append("t", "th-1", "s-re", "delta", {"content": "third"})  # first overflows

    buf2 = _buf(db, cap=2)  # fresh, no hot tier — Redis/memory empty
    records = await buf2.replay("t", "th-1", "s-re", after_event_id=0)
    contents = [r["data"]["content"] for r in records]
    assert "first" in contents  # resurrected from the durable overflow tier


@pytest.mark.asyncio
async def test_overflow_write_idempotent_by_seq(db):
    """Overflow persists each event id once (crash-safe re-flow)."""
    repo = StreamBufferOverflowRepository(db)
    chunk = {"id": 7, "type": "delta", "data": {"content": "x"}}
    n1 = await repo.write_chunks(tenant_id="t", thread_id="th-1", stream_id="s-id", chunks=[chunk])
    n2 = await repo.write_chunks(tenant_id="t", thread_id="th-1", stream_id="s-id", chunks=[chunk])
    assert n1 == 1 and n2 == 1
    rows = await repo.read_chunks(tenant_id="t", thread_id="th-1", stream_id="s-id")
    assert [r["id"] for r in rows] == [7]
    assert rows[0]["data"] == {"content": "x"}


@pytest.mark.asyncio
async def test_clear_repo_removes_overflow_rows(db):
    repo = StreamBufferOverflowRepository(db)
    await repo.write_chunks(
        tenant_id="t", thread_id="th-1", stream_id="s-cl",
        chunks=[
            {"id": 1, "type": "delta", "data": {}},
            {"id": 2, "type": "delta", "data": {}},
        ],
    )
    deleted = await repo.clear(tenant_id="t", thread_id="th-1", stream_id="s-cl")
    assert deleted == 2
    assert await repo.read_chunks(tenant_id="t", thread_id="th-1", stream_id="s-cl") == []


@pytest.mark.asyncio
async def test_clear_stream_removes_hot_and_overflow(db):
    buf = _buf(db, cap=2)
    for content in ("first", "second", "third"):  # "first" overflows
        await buf.append("t", "th-1", "s-clear", "delta", {"content": content})
    overflowed = await buf._overflow.read_chunks(
        tenant_id="t", thread_id="th-1", stream_id="s-clear"
    )
    assert len(overflowed) == 1

    assert await buf.clear("t", "th-1", "s-clear") is True
    assert await buf.replay("t", "th-1", "s-clear") == []
    assert await buf._overflow.read_chunks(
        tenant_id="t", thread_id="th-1", stream_id="s-clear"
    ) == []