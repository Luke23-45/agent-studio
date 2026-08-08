"""
P8-1 write-minimization discipline tests:

- last-active telemetry buffers on the hot tier (Redis / in-memory
  fallback) and NEVER writes the Postgres primary per event
- flush applies batched deltas to the primary in one transaction
- SQL-side increments survive concurrent flushers; revoked keys are skipped
- a batcher with no primary attached degrades to a no-op flush, never an error
"""

from uuid import uuid4

import pytest

from backend.app.infrastructure.db import ApiKeyRepository, init_database
from backend.app.infrastructure.db.models import Base
from backend.app.infrastructure.patterns.last_active import LastActiveBatcher


@pytest.fixture
async def db(tmp_path):
    manager = init_database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db")
    await manager.initialize()
    async with manager._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield manager
    await manager.close()


class _NoPrimary:
    """Stub primary: ANY database access fails the test immediately."""

    def get_session(self):
        raise AssertionError("hot-tier touch must never open a primary session")


async def _key(db) -> dict:
    return await ApiKeyRepository(db).create(
        name="t", key_hash=f"hash-{uuid4()}", prefix="nv", role="operator"
    )


@pytest.mark.asyncio
async def test_touch_buffers_on_hot_tier_never_writes_primary():
    batcher = LastActiveBatcher(db=_NoPrimary())
    await batcher.touch("key-1")
    await batcher.touch("key-1")
    await batcher.touch("key-2")
    assert await batcher.pending_count() == 2
    assert batcher._memory_counts["key-1"] == 2
    assert batcher._memory_counts["key-2"] == 1


@pytest.mark.asyncio
async def test_flush_applies_batched_deltas_in_one_transaction(db):
    key = await _key(db)
    batcher = LastActiveBatcher(db=db)
    await batcher.touch(key["id"])
    await batcher.touch(key["id"])
    await batcher.touch(key["id"])
    assert await batcher.pending_count() == 1

    applied = await batcher.flush()
    assert applied == 1
    row = await ApiKeyRepository(db).get_by_id(key["id"])
    assert row["usage_count"] == 3
    assert row["last_used_at"] is not None

    # Idempotent drain: nothing pending -> nothing applied.
    assert await batcher.flush() == 0


@pytest.mark.asyncio
async def test_flush_skips_revoked_keys(db):
    key = await _key(db)
    await ApiKeyRepository(db).revoke(key["id"])
    batcher = LastActiveBatcher(db=db)
    await batcher.touch(key["id"])
    await batcher.flush()
    row = await ApiKeyRepository(db).get_by_id(key["id"])
    assert row["usage_count"] == 0
    assert row["last_used_at"] is None


@pytest.mark.asyncio
async def test_concurrent_flushers_accumulate_in_sql(db):
    key = await _key(db)
    first = LastActiveBatcher(db=db)
    second = LastActiveBatcher(db=db)
    for _ in range(2):
        await first.touch(key["id"])
    for _ in range(3):
        await second.touch(key["id"])
    await first.flush()
    await second.flush()
    row = await ApiKeyRepository(db).get_by_id(key["id"])
    assert row["usage_count"] == 5


@pytest.mark.asyncio
async def test_flush_without_primary_is_a_noop():
    batcher = LastActiveBatcher()
    await batcher.touch("key-1")
    assert await batcher.flush() == 0
