"""
P6-5 — Config canary rollback drill (Arch §13).

The drill exercises the full rollback path in one run:

    publish v1 (full) -> publish v2 (canary 10%) -> traffic split
    -> canary slice regresses (error rate) -> canary.evaluate
    -> auto_rollback re-promotes v1, v2 marked regressed
    -> traffic resolves to v1 everywhere

Also proves the guard rails: a healthy canary stays live, a canary below
min_samples is never judged, and a regressed version cannot be re-promoted
(the promotion gate blocks it).
"""

import asyncio
from uuid import uuid4

import pytest

from backend.app.governance.promotion import (
    VALIDATION_REGRESSED,
    VALIDATION_VALIDATED,
    canary_bucket,
    promotion_gate,
)
from backend.app.infrastructure.db import (
    TenantConfigVersionRepository,
    TenantRepository,
)
from backend.app.infrastructure.db.manager import DatabaseConfig, DatabaseManager
from backend.app.infrastructure.db.models import Base


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(
        DatabaseConfig(database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/drill.db")
    )

    async def _init():
        await manager.initialize()
        async with manager._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    yield manager
    asyncio.run(manager.close())


def _seed_tenant(db):
    async def _run():
        return await TenantRepository(db).create(
            {
                "id": str(uuid4()),
                "slug": "drill-co",
                "name": "Drill Co",
                "allowed_topics": ["general"],
                "blocked_topics": [],
                "escalation_threshold": 0.7,
                "knowledge_allowlist": [],
                "default_provider": "openai",
                "default_model": "gpt-4",
                "features": {},
                "guardrail_config": {},
                "guardrail_thresholds": {},
            }
        )

    return asyncio.run(_run())


def _publish_validated(repo, tenant_id, config, canary_percent=None):
    version = asyncio.run(repo.create_draft(tenant_id, config))
    asyncio.run(
        repo.set_validation(
            tenant_id, version["version"], VALIDATION_VALIDATED, by="drill"
        )
    )
    asyncio.run(repo.approve(tenant_id, version["version"], approved_by="drill"))
    asyncio.run(
        repo.promote(
            tenant_id, version["version"], promoted_by="drill",
            canary_percent=canary_percent,
        )
    )
    return version["version"]


def _bucket_keys(percent: int, count: int) -> list[str]:
    """Request keys that deterministically resolve into the canary slice."""
    keys: list[str] = []
    i = 0
    while len(keys) < count:
        key = f"user-{i}"
        if canary_bucket(key, percent):
            keys.append(key)
        i += 1
    return keys


def _baseline_keys(percent: int, count: int) -> list[str]:
    """Request keys that deterministically resolve into the baseline slice."""
    keys: list[str] = []
    i = 0
    while len(keys) < count:
        key = f"base-{i}"
        if not canary_bucket(key, percent):
            keys.append(key)
        i += 1
    return keys


class TestCanaryRollbackDrill:
    def _clean_rollouts(self):
        from backend.app.worker import canary_monitor

        canary_monitor._active_rollouts.clear()

    def test_regression_triggers_auto_rollback(self, db, monkeypatch):
        from backend.app.worker import canary_monitor

        self._clean_rollouts()
        # The worker handler resolves the DB through the global manager;
        # point it at this test's manager for the drill.
        monkeypatch.setattr(
            "backend.app.infrastructure.db.get_database_manager",
            lambda: db,
        )
        tenant = _seed_tenant(db)
        repo = TenantConfigVersionRepository(db)

        v1 = _publish_validated(repo, tenant["id"], {"settings": {"v": 1}})
        v2 = _publish_validated(
            repo, tenant["id"], {"settings": {"v": 2}}, canary_percent=10
        )

        # Canary traffic is split deterministically.
        canary_keys = _bucket_keys(10, 6)
        baseline_keys = _baseline_keys(10, 6)
        assert asyncio.run(repo.get_effective(tenant["id"], canary_keys[0]))["version"] == v2
        assert asyncio.run(repo.get_effective(tenant["id"], baseline_keys[0]))["version"] == v1

        rollout = canary_monitor.start_canary(
            tenant["id"],
            canary_version=v2,
            baseline_version=v1,
            canary_percent=10,
            min_samples=5,
        )

        # Baseline slice: 6 healthy requests (0% errors).
        for key in baseline_keys:
            canary_monitor.record_outcome(tenant["id"], key)
        # Canary slice: 6 requests, 3 errors (50% error rate).
        for i, key in enumerate(canary_keys):
            canary_monitor.record_outcome(
                tenant["id"], key, error=i % 2 == 0
            )

        # DRILL: evaluate -> the regression must trigger auto-rollback.
        asyncio.run(canary_monitor.handle_canary_evaluate({}))

        v2_row = asyncio.run(repo.get(tenant["id"], v2))
        assert v2_row["status"] == repo.STATUS_SUPERSEDED
        assert v2_row["validation_status"] == VALIDATION_REGRESSED
        assert "regression" in (v2_row["eval_details"] or {})
        v1_row = asyncio.run(repo.get(tenant["id"], v1))
        assert v1_row["status"] == repo.STATUS_PUBLISHED

        # Rollout ended; traffic resolves to the baseline everywhere.
        assert canary_monitor.get_active_canary(tenant["id"]) is None
        for key in canary_keys + baseline_keys:
            effective = asyncio.run(repo.get_effective(tenant["id"], key))
            assert effective["version"] == v1, key

        # A regressed version can never be re-promoted (promotion gate).
        allowed, reason = promotion_gate(
            v2_row["validation_status"], v2_row.get("eval_status")
        )
        assert allowed is False
        assert reason is not None

    def test_healthy_canary_stays_live(self, db):
        from backend.app.worker import canary_monitor

        self._clean_rollouts()
        tenant = _seed_tenant(db)
        repo = TenantConfigVersionRepository(db)

        v1 = _publish_validated(repo, tenant["id"], {"settings": {"v": 1}})
        v2 = _publish_validated(
            repo, tenant["id"], {"settings": {"v": 2}}, canary_percent=10
        )
        rollout = canary_monitor.start_canary(
            tenant["id"], v2, v1, canary_percent=10, min_samples=5
        )
        for key in _bucket_keys(10, 6) + _baseline_keys(10, 6):
            canary_monitor.record_outcome(tenant["id"], key)

        asyncio.run(canary_monitor.handle_canary_evaluate({}))

        assert rollout.rolled_back is False
        assert canary_monitor.get_active_canary(tenant["id"]) is not None
        v2_row = asyncio.run(repo.get(tenant["id"], v2))
        assert v2_row["status"] == repo.STATUS_PUBLISHED

    def test_below_min_samples_is_never_judged(self, db):
        from backend.app.worker import canary_monitor

        self._clean_rollouts()
        tenant = _seed_tenant(db)
        repo = TenantConfigVersionRepository(db)

        v1 = _publish_validated(repo, tenant["id"], {"settings": {"v": 1}})
        v2 = _publish_validated(
            repo, tenant["id"], {"settings": {"v": 2}}, canary_percent=10
        )
        rollout = canary_monitor.start_canary(
            tenant["id"], v2, v1, canary_percent=10, min_samples=5
        )
        # Only 4 requests on the canary slice: below min_samples.
        canary_keys = _bucket_keys(10, 4)
        for i, key in enumerate(canary_keys):
            canary_monitor.record_outcome(tenant["id"], key, error=True)
        rollout.record_request(True, error=True)

        asyncio.run(canary_monitor.handle_canary_evaluate({}))

        assert rollout.rolled_back is False
        assert canary_monitor.get_active_canary(tenant["id"]) is not None
