"""
P4-7 — Async cost ledger / quota reconcile worker path.

The completion path enqueues ``cost_ledger.write`` (never blocks the
request); the worker persists one ``spend_events`` row and upserts the
durable ``quota_state`` rows for every budget level on the request's path.
These tests pin the async contract:

- ``CostLedger.record`` enqueues a job when a queue is configured, and
  falls back to a direct repository write when enqueue fails (never drops).
- ``handle_cost_ledger_write`` writes spend + quota rows for the levels
  present on the record (platform / tenant / surface / end-user).
- Repeated writes accumulate on the same window row (idempotent upsert).
- The handler is registered in ``build_handlers`` for the worker.
"""

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.gateway.ledger import CostLedger, JOB_COST_LEDGER_WRITE, UsageRecord
from backend.app.infrastructure.db import init_database
from backend.app.infrastructure.db.models import Base
from backend.app.infrastructure.db.repositories import (
    QuotaStateRepository,
    SpendEventRepository,
)
from backend.app.worker import handlers as worker_handlers


@pytest.fixture
async def db(tmp_path):
    manager = init_database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/ledger.db")
    await manager.initialize()
    async with manager._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield manager
    await manager.close()


def _record(**overrides) -> UsageRecord:
    base = dict(
        tenant_id="t1",
        provider="openai",
        model="gpt-4o",
        input_tokens=100,
        output_tokens=50,
        reasoning_tokens=5,
        cached_tokens=10,
        usd=0.0042,
        surface_id="surface-1",
        end_user_id="user-1",
        conversation_id="conv-1",
        session_id="sess-1",
        request_id="req-1",
    )
    base.update(overrides)
    return UsageRecord(**base)


async def _count(db, model):
    from sqlalchemy import func, select

    from backend.app.infrastructure.db.models import SpendEventModel

    async with db.get_session() as session:
        return int(
            (await session.execute(select(func.count()).select_from(model))).scalar_one()
        )


async def _windows(db, scope_type):
    from backend.app.infrastructure.db.models import QuotaStateModel

    async with db.get_session() as session:
        rows = list(
            (
                await session.execute(
                    QuotaStateModel.__table__.select().where(
                        QuotaStateModel.scope_type == scope_type
                    )
                )
            ).fetchall()
        )
        return [dict(r._mapping) for r in rows]


@pytest.mark.asyncio
async def test_record_enqueues_cost_ledger_write_job():
    """The async reconcile path: completion enqueues cost_ledger.write."""
    queue = MagicMock()
    queue.enqueue = AsyncMock(return_value=True)
    ledger = CostLedger(queue=queue)
    await ledger.record(_record())

    queue.enqueue.assert_awaited_once()
    job = queue.enqueue.await_args.args[0]
    assert job.type == JOB_COST_LEDGER_WRITE
    assert job.payload["tenant_id"] == "t1"
    assert job.payload["usd"] == 0.0042
    assert job.trace_id == "req-1"


@pytest.mark.asyncio
async def test_record_falls_back_to_direct_db_write(db):
    """Enqueue failure must never drop the spend event (degrade visibly)."""
    queue = MagicMock()
    queue.enqueue = AsyncMock(return_value=False)
    ledger = CostLedger(queue=queue, db=db)
    await ledger.record(_record())

    # Direct fallback wrote the row even though enqueue returned False.
    from backend.app.infrastructure.db.models import SpendEventModel

    assert await _count(db, SpendEventModel) == 1


@pytest.mark.asyncio
async def test_record_raises_when_no_write_path():
    ledger = CostLedger(queue=None, db=None)
    with pytest.raises(RuntimeError):
        await ledger.record(_record())


@pytest.mark.asyncio
async def test_worker_writes_spend_event_and_quota_rows(db, monkeypatch):
    """handle_cost_ledger_write persists the spend row + quota upserts for
    every level present on the record (P3-5/P3-6 async path)."""
    import backend.app.infrastructure.db as db_module

    monkeypatch.setattr(db_module, "get_database_manager", lambda: db)
    await worker_handlers.handle_cost_ledger_write(_record().__dict__)

    from backend.app.infrastructure.db.models import SpendEventModel

    assert await _count(db, SpendEventModel) == 1
    for scope in ("platform", "tenant", "surface", "end_user"):
        windows = await _windows(db, scope)
        assert len(windows) == 1, scope
        assert windows[0]["spent_usd"] == pytest.approx(0.0042), scope


@pytest.mark.asyncio
async def test_worker_skips_levels_absent_on_record(db, monkeypatch):
    """No surface/end-user on the record → only platform + tenant rows."""
    import backend.app.infrastructure.db as db_module

    monkeypatch.setattr(db_module, "get_database_manager", lambda: db)
    await worker_handlers.handle_cost_ledger_write(
        _record(surface_id=None, end_user_id=None).__dict__
    )

    assert len(await _windows(db, "platform")) == 1
    assert len(await _windows(db, "tenant")) == 1
    assert len(await _windows(db, "surface")) == 0
    assert len(await _windows(db, "end_user")) == 0


@pytest.mark.asyncio
async def test_worker_reconcile_accumulates_on_window(db, monkeypatch):
    """Two completions in the same month accumulate on the same quota row."""
    import backend.app.infrastructure.db as db_module

    monkeypatch.setattr(db_module, "get_database_manager", lambda: db)
    await worker_handlers.handle_cost_ledger_write(_record().__dict__)
    await worker_handlers.handle_cost_ledger_write(
        _record(request_id="req-2", usd=0.0010).__dict__
    )

    windows = await _windows(db, "tenant")
    assert len(windows) == 1
    assert windows[0]["spent_usd"] == pytest.approx(0.0052)


@pytest.mark.asyncio
async def test_cost_ledger_sum_usd_month_scoped(db):
    from backend.app.infrastructure.db.models import SpendEventModel

    ledger = CostLedger(queue=None, db=db)
    record = _record()
    await SpendEventRepository(db).add(record.__dict__)
    future = _record(request_id="req-2", usd=0.0010)
    await SpendEventRepository(db).add(future.__dict__)

    now = datetime.datetime.now(datetime.timezone.utc)
    total = await ledger.sum_usd(tenant_id="t1", year=now.year, month=now.month)
    assert total == pytest.approx(0.0052)


@pytest.mark.asyncio
async def test_handler_registered_in_build(monkeypatch):
    manager = MagicMock()
    monkeypatch.setattr(
        "backend.app.worker.handlers.get_queue_manager", lambda: manager
    )
    handlers = worker_handlers.build_handlers()
    assert JOB_COST_LEDGER_WRITE in handlers
    assert callable(handlers[JOB_COST_LEDGER_WRITE])