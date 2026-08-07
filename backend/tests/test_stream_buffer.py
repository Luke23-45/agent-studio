"""StreamBuffer unit tests (Phase 4); in-memory fallback — no Redis."""
import pytest

from backend.app.infrastructure.stream.buffer import StreamBuffer


@pytest.mark.asyncio
async def test_append_replay_ordering():
    buf = StreamBuffer()
    a = await buf.append("t1", "th-1", "s1", "delta", {"content": "one"})
    b = await buf.append("t1", "th-1", "s1", "delta", {"content": "two"})
    c = await buf.append("t1", "th-1", "s1", "delta", {"content": "three"})
    assert a < b < c

    records = await buf.replay("t1", "th-1", "s1", after_event_id=0)
    assert [r["data"]["content"] for r in records] == ["one", "two", "three"]
    assert [r["id"] for r in records] == [a, b, c]

    tail = await buf.replay("t1", "th-1", "s1", after_event_id=b)
    assert [r["data"]["content"] for r in tail] == ["three"]


@pytest.mark.asyncio
async def test_terminal_marker_and_replay_after_id():
    buf = StreamBuffer()
    a = await buf.append("t1", "th-1", "s2", "delta", {"content": "x"})
    await buf.mark_terminal("t1", "th-1", "s2", "result", {"response": "x"})
    terminal = await buf.get_terminal("t1", "th-1", "s2")
    assert terminal["type"] == "result"
    assert terminal["data"] == {"response": "x"}
    assert terminal["id"] > a


@pytest.mark.asyncio
async def test_no_terminal_while_running():
    buf = StreamBuffer()
    await buf.append("t1", "th-1", "s3", "delta", {"content": "x"})
    assert await buf.get_terminal("t1", "th-1", "s3") is None


@pytest.mark.asyncio
async def test_tenant_isolation():
    buf = StreamBuffer()
    await buf.append("tA", "th-1", "s1", "delta", {"content": "a"})
    await buf.append("tB", "th-1", "s1", "delta", {"content": "b"})
    assert [r["data"]["content"] for r in await buf.replay("tA", "th-1", "s1")] == ["a"]
    assert [r["data"]["content"] for r in await buf.replay("tB", "th-1", "s1")] == ["b"]


@pytest.mark.asyncio
async def test_thread_isolation_shares_stream_ids():
    """P4-2: buffer keys are thread-scoped (``{tenant}:{thread}:{stream}``), so
    the same request-id/stream-id can replay under two different threads without
    cross-talk (idempotency stays per request within a thread)."""
    buf = StreamBuffer()
    await buf.append("t1", "th-A", "req-1", "delta", {"content": "for A"})
    await buf.append("t1", "th-B", "req-1", "delta", {"content": "for B"})
    await buf.mark_terminal("t1", "th-B", "req-1", "result", {"response": "done"})

    assert [r["data"]["content"] for r in await buf.replay("t1", "th-A", "req-1")] == ["for A"]
    assert await buf.get_terminal("t1", "th-A", "req-1") is None
    assert await buf.get_terminal("t1", "th-B", "req-1") is not None


@pytest.mark.asyncio
async def test_clear_removes_entries():
    buf = StreamBuffer()
    await buf.append("t1", "th-1", "s4", "delta", {"content": "x"})
    await buf.mark_terminal("t1", "th-1", "s4", "result", {"ok": True})
    assert await buf.get_terminal("t1", "th-1", "s4") is not None
    await buf.clear("t1", "th-1", "s4")
    assert await buf.replay("t1", "th-1", "s4") == []
    assert await buf.get_terminal("t1", "th-1", "s4") is None


@pytest.mark.asyncio
async def test_idempotent_replay_is_the_same_call():
    """P4-5: a retry reusing ``Idempotency-Key`` replays the same buffered
    turn — the route uses ``get_terminal``/``replay`` off the same request-id
    stream (thread-scoped), so the bytes are byte-identical and never
    re-generated."""
    buf = StreamBuffer()
    ids = []
    for chunk in ("one", "two"):
        ids.append(await buf.append("t1", "th-1", "req-1", "delta", {"content": chunk}))
    await buf.mark_terminal("t1", "th-1", "req-1", "result", {"response": "helloworld"})

    # A client that acked the last delta still legitimately receives the
    # terminal (its id is newer than any delta ack) — never re-generated.
    run1 = await buf.replay("t1", "th-1", "req-1", after_event_id=ids[-1])
    assert [r["type"] for r in run1] == ["result"]

    # A fresh connection replays the identical ordered bytes, then the
    # terminal in the same position it was produced.
    run2 = await buf.replay("t1", "th-1", "req-1", after_event_id=0)
    assert [r["data"]["content"] for r in run2[:-1]] == ["one", "two"]
    assert run2[-1]["type"] == "result"

    # A strict resume after the terminal yields nothing new.
    run3 = await buf.replay("t1", "th-1", "req-1", after_event_id=run2[-1]["id"])
    assert run3 == []