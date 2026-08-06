"""StreamBuffer unit tests (Phase 4); in-memory fallback — no Redis."""
import pytest

from backend.app.infrastructure.stream.buffer import StreamBuffer


@pytest.mark.asyncio
async def test_append_replay_ordering():
    buf = StreamBuffer()
    a = await buf.append("t1", "s1", "delta", {"content": "one"})
    b = await buf.append("t1", "s1", "delta", {"content": "two"})
    c = await buf.append("t1", "s1", "delta", {"content": "three"})
    assert a < b < c

    records = await buf.replay("t1", "s1", after_event_id=0)
    assert [r["data"]["content"] for r in records] == ["one", "two", "three"]
    assert [r["id"] for r in records] == [a, b, c]

    tail = await buf.replay("t1", "s1", after_event_id=b)
    assert [r["data"]["content"] for r in tail] == ["three"]


@pytest.mark.asyncio
async def test_terminal_marker_and_replay_after_id():
    buf = StreamBuffer()
    a = await buf.append("t1", "s2", "delta", {"content": "x"})
    await buf.mark_terminal("t1", "s2", "result", {"response": "x"})
    terminal = await buf.get_terminal("t1", "s2")
    assert terminal["type"] == "result"
    assert terminal["data"] == {"response": "x"}
    assert terminal["id"] > a


@pytest.mark.asyncio
async def test_no_terminal_while_running():
    buf = StreamBuffer()
    await buf.append("t1", "s3", "delta", {"content": "x"})
    assert await buf.get_terminal("t1", "s3") is None


@pytest.mark.asyncio
async def test_tenant_isolation():
    buf = StreamBuffer()
    await buf.append("tA", "s1", "delta", {"content": "a"})
    await buf.append("tB", "s1", "delta", {"content": "b"})
    assert [r["data"]["content"] for r in await buf.replay("tA", "s1")] == ["a"]
    assert [r["data"]["content"] for r in await buf.replay("tB", "s1")] == ["b"]


@pytest.mark.asyncio
async def test_clear_removes_entries():
    buf = StreamBuffer()
    await buf.append("t1", "s4", "delta", {"content": "x"})
    await buf.mark_terminal("t1", "s4", "result", {"ok": True})
    assert await buf.get_terminal("t1", "s4") is not None
    await buf.clear("t1", "s4")
    assert await buf.replay("t1", "s4") == []
    assert await buf.get_terminal("t1", "s4") is None