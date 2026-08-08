"""
P8-3 read-replica routing tests:

- unconfigured router routes every read to the primary
- configured router round-robins across healthy replicas
- read-your-writes window pins freshly written keys to the primary
- replica outage marks it unavailable and falls back to the primary
- reads never hard-fail on replica failure; health reports honestly
- repository integration: history reads go to replicas, writes to primary
"""

import asyncio

import pytest
from sqlalchemy.exc import DBAPIError

from backend.app.infrastructure.db import (
    ConversationRepository,
    ThreadRepository,
    init_database,
)
from backend.app.infrastructure.db.models import Base
from backend.app.infrastructure.db.replicas import ReplicaRouter
from backend.app.infrastructure.patterns import HealthStatus


@pytest.fixture
async def db(tmp_path):
    manager = init_database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db")
    await manager.initialize()
    async with manager._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield manager
    await manager.close()


def _router(tmp_path, urls, **kwargs) -> ReplicaRouter:
    return ReplicaRouter(
        [f"sqlite+aiosqlite:///{tmp_path.as_posix()}/{u}" for u in urls],
        **kwargs,
    )


# ---- unconfigured: primary only ----------------------------------------


@pytest.mark.asyncio
async def test_unconfigured_routes_to_primary(db):
    router = ReplicaRouter([])
    router.bind_primary(db)
    assert not router.configured
    # No replicas -> replica never eligible; every read goes to the primary.
    assert not router.replica_eligible("t1", "thread-1")

    async with router.get_read_session("t1", "thread-1") as session:
        from sqlalchemy import text

        result = await session.execute(text("SELECT 1"))
        assert result.scalar() == 1

    await router.close()


# ---- round-robin selection ----------------------------------------------


@pytest.mark.asyncio
async def test_round_robin_across_healthy_replicas(db, tmp_path):
    router = _router(tmp_path, ["r1.db", "r2.db"])
    router.bind_primary(db)
    await router.initialize()

    assert len(router._replicas) == 2
    assert not router._unavailable
    await router.close()


@pytest.mark.asyncio
async def test_next_replica_alternates(db, tmp_path):
    router = _router(tmp_path, ["r1.db", "r2.db"])
    await router.initialize()

    first, _ = router._next_replica()
    second, _ = router._next_replica()
    third, _ = router._next_replica()
    assert first is not second
    assert third is first  # wraps around

    await router.close()


# ---- read-your-writes window --------------------------------------------


@pytest.mark.asyncio
async def test_ryw_window_pins_written_keys_to_primary(db, tmp_path):
    router = _router(tmp_path, ["r1.db"], window_seconds=5.0)
    await router.initialize()

    assert router.replica_eligible("t1", "thread-a")
    router.mark_write("t1", "thread-a")
    assert not router.replica_eligible("t1", "thread-a")
    # The tenant key was marked: sibling keys sharing it are pinned too
    # (repository marks the tenant key on thread creation so listings
    # always see fresh threads).
    assert not router.replica_eligible("t1", "thread-b")
    # Unmarked keys (other tenants, other threads) stay eligible.
    assert router.replica_eligible("t2", "thread-b")

    await router.close()


@pytest.mark.asyncio
async def test_ryw_window_expires(db, tmp_path):
    router = _router(tmp_path, ["r1.db"], window_seconds=0.05)
    await router.initialize()

    router.mark_write("t1", "thread-1")
    assert not router.replica_eligible("t1", "thread-1")
    await asyncio.sleep(0.08)
    assert router.replica_eligible("t1", "thread-1")

    await router.close()


@pytest.mark.asyncio
async def test_mark_write_ignores_empty_keys(db, tmp_path):
    router = _router(tmp_path, ["r1.db"], window_seconds=5.0)
    await router.initialize()

    router.mark_write("t1", "", None)
    # Empty keys are skipped; "t1" was stored so its reads stay pinned,
    # while untouched tenants remain eligible.
    assert not router.replica_eligible("t1", "whatever")
    assert router.replica_eligible("t2", "whatever")

    await router.close()


# ---- failure semantics ---------------------------------------------------


@pytest.mark.asyncio
async def test_unhealthy_replica_falls_back_to_primary(db, tmp_path):
    # Path in a directory that does not exist -> ping fails at initialize.
    router = ReplicaRouter(
        [f"sqlite+aiosqlite:///{tmp_path.as_posix()}/no_such_dir/r.db"]
    )
    router.bind_primary(db)
    await router.initialize()

    assert router._unavailable
    assert not router.replica_eligible("t1", "thread-1")

    # Reads never hard-fail: they land on the primary.
    conv = await ConversationRepository(db).get_or_create("t1", "s-1")
    thread = await ThreadRepository(db).create_thread(
        "t1", conversation_id=conv["id"]
    )
    repo = ThreadRepository(db, read_router=router)
    page = await repo.list_messages("t1", thread["id"])
    assert page["messages"] == []

    await router.close()


@pytest.mark.asyncio
async def test_dbapi_error_marks_replica_unavailable(db, tmp_path):
    router = _router(tmp_path, ["r1.db"])
    await router.initialize()
    engine = router._replicas[0]
    assert id(engine) not in router._unavailable

    with pytest.raises(DBAPIError):
        async with router.get_read_session("t1", "thread-1"):
            raise DBAPIError("boom", None, None)

    assert id(engine) in router._unavailable

    # After the failure the router serves the primary.
    router.bind_primary(db)
    async with router.get_read_session("t1", "thread-1") as session:
        from sqlalchemy import text

        result = await session.execute(text("SELECT 1"))
        assert result.scalar() == 1

    await router.close()


# ---- health reporting ----------------------------------------------------


@pytest.mark.asyncio
async def test_health_reports_degraded_when_all_replicas_down(db, tmp_path):
    router = ReplicaRouter(
        [f"sqlite+aiosqlite:///{tmp_path.as_posix()}/no_such_dir/r.db"]
    )
    await router.initialize()

    health = await router.health_check()
    assert health.status == HealthStatus.DEGRADED

    await router.close()


@pytest.mark.asyncio
async def test_health_reports_healthy_when_one_replica_up(db, tmp_path):
    router = _router(tmp_path, ["r1.db", "no_such_dir/r2.db"])
    await router.initialize()

    health = await router.health_check()
    assert health.status == HealthStatus.HEALTHY
    assert health.metadata["healthy"] == 1

    await router.close()


# ---- repository integration (L1 shape: replicas share the primary file) -


@pytest.mark.asyncio
async def test_history_reads_served_from_replica_data(db, tmp_path):
    # Replica points at the same sqlite file: it serves the primary's rows.
    router = ReplicaRouter(
        [f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db"]
    )
    router.bind_primary(db)
    await router.initialize()

    conv = await ConversationRepository(db).get_or_create("t1", "s-1")
    repo = ThreadRepository(db, read_router=router)
    thread = await repo.create_thread("t1", conversation_id=conv["id"])
    await repo.append_message(
        "t1",
        thread["id"],
        role="user",
        content="hello",
        redacted_content="hello",
        conversation_id=conv["id"],
    )

    # Freshly written keys pin to the primary (RYW).
    assert not router.replica_eligible("t1", thread["id"])
    page = await repo.list_messages("t1", thread["id"])
    assert len(page["messages"]) == 1

    # Once the window expires, the replica serves the same rows.
    router.window_seconds = 0.0
    router._last_write.clear()
    assert router.replica_eligible("t1", thread["id"])
    page = await repo.list_messages("t1", thread["id"])
    assert [m["content"] for m in page["messages"]] == ["hello"]

    await router.close()
