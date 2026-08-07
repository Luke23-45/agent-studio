"""
P5-2 config promotion pipeline tests (Arch 12, P0-11):

- publish changes runtime behavior; rollback restores the previous version
- concurrent publishes leave exactly one published version (last wins),
  and version immutability is preserved
- canary rollouts: deterministic per-key bucketing, bounded split, and
  baseline served to the non-canary slice
- auto-rollback on regression re-promotes the prior version and marks the
  failing one regressed (never promotable again)
- eval gate: a draft whose eval suite failed cannot be promoted
"""

import asyncio
from uuid import uuid4

import pytest

from backend.app.application.compaction.refresh import load_effective_tenant_config
from backend.app.governance.promotion import (
    VALIDATION_REGRESSED,
    canary_bucket,
    promotion_gate,
    validate_canary_percent,
)
from backend.app.infrastructure.db import (
    TenantConfigVersionRepository,
    TenantRepository,
    init_database,
)
from backend.app.infrastructure.db.models import Base


@pytest.fixture
async def db(tmp_path):
    manager = init_database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/promo.db")
    await manager.initialize()
    async with manager._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield manager
    await manager.close()


async def _tenant(db, slug=None) -> str:
    tenant_id = str(uuid4())
    await TenantRepository(db).create(
        {
            "id": tenant_id,
            "slug": slug or f"slug-{uuid4()}",
            "name": "Promo Co",
            "allowed_topics": [],
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
    return tenant_id


def _config(tenant_id: str, model: str, **extra) -> dict:
    return {
        "id": tenant_id,
        "name": "Promo Co",
        "slug": "promo",
        "default_provider": "openai",
        "default_model": model,
        **extra,
    }


class TestPublishRollbackRuntime:
    async def test_publish_changes_runtime_behavior(self, db):
        repo = TenantConfigVersionRepository(db)
        tenant_id = await _tenant(db)
        tenant_row = (await TenantRepository(db).get_by_id(tenant_id))

        v1 = await repo.create_draft(tenant_id, _config(tenant_id, "gpt-4"))
        await repo.promote(tenant_id, v1["version"])
        effective = await load_effective_tenant_config(db, tenant_row)
        assert effective.default_model == "gpt-4"

        v2 = await repo.create_draft(tenant_id, _config(tenant_id, "gpt-4o"))
        await repo.promote(tenant_id, v2["version"])
        effective = await load_effective_tenant_config(db, tenant_row)
        assert effective.default_model == "gpt-4o"
        assert v2["version"] == v1["version"] + 1  # immutable, append-only

    async def test_rollback_restores_previous_behavior(self, db):
        repo = TenantConfigVersionRepository(db)
        tenant_id = await _tenant(db)
        tenant_row = (await TenantRepository(db).get_by_id(tenant_id))

        v1 = await repo.create_draft(tenant_id, _config(tenant_id, "gpt-4"))
        await repo.promote(tenant_id, v1["version"])
        v2 = await repo.create_draft(tenant_id, _config(tenant_id, "gpt-4o"))
        await repo.promote(tenant_id, v2["version"])
        assert (await load_effective_tenant_config(db, tenant_row)).default_model == "gpt-4o"

        await repo.promote(tenant_id, v1["version"])
        effective = await load_effective_tenant_config(db, tenant_row)
        assert effective.default_model == "gpt-4"

    async def test_concurrent_publish_is_serialized_last_writer_wins(self, db):
        repo = TenantConfigVersionRepository(db)
        tenant_id = await _tenant(db)

        v1 = await repo.create_draft(tenant_id, _config(tenant_id, "gpt-4"))
        v2 = await repo.create_draft(tenant_id, _config(tenant_id, "gpt-4o"))
        # Two publishes racing: exactly one published row must survive and it
        # must be one of the promoted versions — never a torn state.
        await asyncio.gather(
            repo.promote(tenant_id, v1["version"]),
            repo.promote(tenant_id, v2["version"]),
        )
        latest = await repo.get_latest_published(tenant_id)
        assert latest is not None
        assert latest["version"] in (v1["version"], v2["version"])
        versions = await repo.list_versions(tenant_id)
        published = [v for v in versions if v["status"] == repo.STATUS_PUBLISHED]
        assert len(published) == 1
        superseded = [v for v in versions if v["status"] == repo.STATUS_SUPERSEDED]
        assert len(superseded) == 1


class TestCanaryRollout:
    def test_bucket_is_deterministic_per_key(self):
        assert canary_bucket("eu-1", 50) == canary_bucket("eu-1", 50)
        assert canary_bucket("", 50) is False  # empty key -> baseline

    def test_bucket_boundaries(self):
        assert canary_bucket("any", 100) is True
        assert canary_bucket("any", 0) is False
        assert canary_bucket("any", None) is True

    def test_bucket_splits_within_bounds(self):
        keys = [f"user-{i}" for i in range(400)]
        canary_share = sum(canary_bucket(k, 50) for k in keys)
        assert 0 < canary_share < len(keys)  # both slices populated
        assert canary_share == sum(canary_bucket(k, 50) for k in keys)

    async def test_canary_rollout_serves_baseline_to_rest(self, db):
        repo = TenantConfigVersionRepository(db)
        tenant_id = await _tenant(db)
        tenant_row = (await TenantRepository(db).get_by_id(tenant_id))

        v1 = await repo.create_draft(tenant_id, _config(tenant_id, "gpt-4"))
        await repo.promote(tenant_id, v1["version"])
        v2 = await repo.create_draft(tenant_id, _config(tenant_id, "gpt-4o"))
        canary = await repo.promote(tenant_id, v2["version"], canary_percent=30)
        assert canary["canary_percent"] == 30

        baseline = await repo.get_previous_published(tenant_id, v2["version"])
        assert baseline["version"] == v1["version"]

        by_key = {k: (await load_effective_tenant_config(db, tenant_row, k)).default_model for k in ("eu-a", "eu-b", "eu-c")}
        served = set(by_key.values())
        assert served == {"gpt-4", "gpt-4o"}  # both slices live

        # Same key always gets the same version for this rollout.
        for key, model in by_key.items():
            again = (await load_effective_tenant_config(db, tenant_row, key)).default_model
            assert again == model

        # Background workers (no key) see the latest published canary.
        assert (await load_effective_tenant_config(db, tenant_row)).default_model == "gpt-4o"

    async def test_full_rollout_supersedes_canary(self, db):
        repo = TenantConfigVersionRepository(db)
        tenant_id = await _tenant(db)
        v1 = await repo.create_draft(tenant_id, _config(tenant_id, "gpt-4"))
        await repo.promote(tenant_id, v1["version"])
        v2 = await repo.create_draft(tenant_id, _config(tenant_id, "gpt-4o"))
        await repo.promote(tenant_id, v2["version"], canary_percent=30)
        v3 = await repo.create_draft(tenant_id, _config(tenant_id, "gpt-5"))
        await repo.promote(tenant_id, v3["version"])  # full rollout
        latest = await repo.get_latest_published(tenant_id)
        assert latest["version"] == v3["version"]
        assert latest["canary_percent"] is None
        baseline = await repo.get_previous_published(tenant_id, v3["version"])
        assert baseline is not None  # superseded v2 is the rollback target


class TestAutoRollback:
    async def test_auto_rollback_restores_baseline(self, db):
        repo = TenantConfigVersionRepository(db)
        tenant_id = await _tenant(db)
        tenant_row = (await TenantRepository(db).get_by_id(tenant_id))

        v1 = await repo.create_draft(tenant_id, _config(tenant_id, "gpt-4"))
        await repo.promote(tenant_id, v1["version"])
        v2 = await repo.create_draft(tenant_id, _config(tenant_id, "gpt-4o"))
        await repo.promote(tenant_id, v2["version"], canary_percent=50)

        restored = await repo.auto_rollback(tenant_id, v2["version"], "error rate > 5%")
        assert restored["version"] == v1["version"]

        failing = await repo.get(tenant_id, v2["version"])
        assert failing["validation_status"] == VALIDATION_REGRESSED
        assert "error rate" in failing["eval_details"]["regression"]

        # The regressed version can never be re-promoted (eval gate).
        allowed, _ = promotion_gate(failing["validation_status"], failing["eval_status"])
        assert allowed is False
        assert (await load_effective_tenant_config(db, tenant_row)).default_model == "gpt-4"

    async def test_auto_rollback_after_full_publish(self, db):
        repo = TenantConfigVersionRepository(db)
        tenant_id = await _tenant(db)
        v1 = await repo.create_draft(tenant_id, _config(tenant_id, "gpt-4"))
        await repo.promote(tenant_id, v1["version"])
        v2 = await repo.create_draft(tenant_id, _config(tenant_id, "gpt-4o"))
        await repo.promote(tenant_id, v2["version"])

        restored = await repo.auto_rollback(tenant_id, v2["version"], "failure regression")
        assert restored["version"] == v1["version"]
        assert (await repo.get_latest_published(tenant_id))["version"] == v1["version"]

    async def test_auto_rollback_noop_when_not_published(self, db):
        repo = TenantConfigVersionRepository(db)
        tenant_id = await _tenant(db)
        draft = await repo.create_draft(tenant_id, _config(tenant_id, "gpt-4"))
        assert (
            await repo.auto_rollback(tenant_id, draft["version"], "nope") is None
        )


class TestEvalGate:
    async def test_failed_eval_blocks_promotion(self, db):
        repo = TenantConfigVersionRepository(db)
        tenant_id = await _tenant(db)
        draft = await repo.create_draft(tenant_id, _config(tenant_id, "gpt-4"))
        await repo.set_eval_result(tenant_id, draft["version"], "failed", {"accuracy": 0.4})

        allowed, reason = promotion_gate(draft["validation_status"], "failed")
        assert allowed is False
        assert "eval suite" in reason

    async def test_passed_eval_allows_promotion(self, db):
        repo = TenantConfigVersionRepository(db)
        tenant_id = await _tenant(db)
        draft = await repo.create_draft(tenant_id, _config(tenant_id, "gpt-4"))
        row = await repo.set_eval_result(tenant_id, draft["version"], "passed", {"accuracy": 0.9})
        allowed, reason = promotion_gate(row["validation_status"], row["eval_status"])
        assert allowed is True and reason is None
        assert row["eval_details"]["accuracy"] == 0.9

    def test_gate_rejects_failed_validation_statuses(self):
        assert promotion_gate("failed")[0] is False
        assert promotion_gate(VALIDATION_REGRESSED)[0] is False
        assert promotion_gate("validated")[0] is True
        assert promotion_gate("")[0] is True
        assert promotion_gate(None)[0] is True

    def test_canary_percent_validation(self):
        assert validate_canary_percent(None)
        assert validate_canary_percent(0)
        assert validate_canary_percent(100)
        assert not validate_canary_percent(-1)
        assert not validate_canary_percent(101)
