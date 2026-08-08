"""
P8-2 schema discipline tests: the tenant-leading composite indexes that
make the turn log partition/shard-ready are present in the created schema
(migration 0010 ships the same DDL for existing deployments).
"""

from sqlalchemy import inspect

from backend.app.infrastructure.db import init_database
from backend.app.infrastructure.db.models import Base


async def _indexes(manager, table: str) -> set[str]:
    async with manager._engine.connect() as conn:
        names = await conn.run_sync(
            lambda c: {ix["name"] for ix in inspect(c).get_indexes(table)}
        )
        return names


async def _unique_constraints(manager, table: str) -> set[str]:
    async with manager._engine.connect() as conn:
        names = await conn.run_sync(
            lambda c: {uc["name"] for uc in inspect(c).get_unique_constraints(table)}
        )
        return names


async def test_turn_log_has_tenant_leading_composites(tmp_path):
    manager = init_database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db")
    await manager.initialize()
    async with manager._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    assert "ix_messages_tenant_thread_seq" in await _indexes(manager, "messages")
    assert "ix_thread_events_tenant_thread_seq" in await _indexes(manager, "thread_events")

    # The pre-existing serving indexes and uniqueness keys remain intact.
    for table, expected in (
        ("messages", {"ix_messages_tenant_created", "uq_messages_thread_seq"}),
        ("thread_events", {"ix_thread_events_tenant_created", "uq_thread_events_thread_seq"}),
    ):
        assert expected <= (
            await _indexes(manager, table) | await _unique_constraints(manager, table)
        )

    await manager.close()
