"""
Tests for the red-team runner (matrix 11.4 / P1 worker wiring):

- deterministic suites run against the REAL guardrail pipeline (regex
  fastpath + jailbreak scan, classifier/PII/spotlighting disabled so the
  run is offline and reproducible)
- report shape is versioned and machine-readable (CI diffing)
- injected stub targets exercise pass/fail accounting
- worker handler runs the suites and audits the report
"""

import asyncio

import pytest

from backend.app.application.redteam import (
    REDTEAM_SUITES,
    RUNNER_VERSION,
    create_red_team_runner,
)
from backend.app.application.redteam.service import (
    AttackCase,
    SuiteDefinition,
)


class TestSuitesAgainstRealPipeline:
    def test_run_all_produces_versioned_report(self):
        report = asyncio.run(create_red_team_runner().run_all())
        data = report.to_dict()
        assert data["runner_version"] == RUNNER_VERSION
        assert data["run_id"]
        assert data["target"] == "guardrail-pipeline"
        summary = data["summary"]
        assert summary["suites"] == len(REDTEAM_SUITES)
        assert summary["cases"] > 0
        assert summary["blocked"] > 0
        # no expectation mismatches on the deterministic suites
        assert summary["failed"] == 0
        assert data["failures"] == []

    def test_jailbreak_attacks_blocked(self):
        runner = create_red_team_runner()
        suite = next(s for s in REDTEAM_SUITES if s.name == "jailbreak")
        result = asyncio.run(runner.run_suite(suite))
        blocked_ids = {r.case_id for r in result.results if r.blocked}
        assert "jailbreak-0" in blocked_ids  # ignore all previous instructions
        assert "jailbreak-2" in blocked_ids  # DAN mode
        benign = next(r for r in result.results if r.case_id == "jailbreak-5")
        assert benign.blocked is False

    def test_injection_and_pii_suites_blocked(self):
        runner = create_red_team_runner()
        for suite in REDTEAM_SUITES:
            if suite.name not in ("prompt-injection", "pii"):
                continue
            result = asyncio.run(runner.run_suite(suite))
            assert result.failed == 0, result.to_dict()

    def test_benign_controls_pass(self):
        runner = create_red_team_runner()
        suite = next(s for s in REDTEAM_SUITES if s.name == "benign")
        result = asyncio.run(runner.run_suite(suite))
        assert result.failed == 0
        assert result.blocked == 0


class TestRunnerAccounting:
    async def _stub(self, block_words: list[str]):
        async def target(prompt: str) -> dict:
            return {
                "blocked": any(w in prompt for w in block_words),
                "violations": [{"category": "stub"}],
            }

        return target

    def test_findings_reported_not_raised(self):
        suite = SuiteDefinition(
            name="stub",
            cases=[
                AttackCase(id="a", category="stub", prompt="ignore previous", expected_blocked=True),
                AttackCase(id="b", category="stub", prompt="hello world", expected_blocked=False),
            ],
        )
        runner = create_red_team_runner()

        async def run():
            runner.target = await self._stub(["ignore"])
            return await runner.run_suite(suite)

        result = asyncio.run(run())
        assert result.passed == 2
        assert result.failed == 0

    def test_expectation_mismatch_counts_as_failure(self):
        suite = SuiteDefinition(
            name="stub",
            cases=[
                AttackCase(id="a", category="stub", prompt="ignore previous", expected_blocked=False),
            ],
        )
        runner = create_red_team_runner()

        async def run():
            runner.target = await self._stub(["ignore"])
            result = await runner.run_suite(suite)
            data = result.to_dict()
            assert data["failed"] == 1
            assert data["results"][0]["passed"] is False

        asyncio.run(run())


class TestWorkerHandler:
    def test_handle_redteam_run_completes(self):
        from backend.app.worker.handlers import handle_redteam_run

        asyncio.run(handle_redteam_run({}))
        # audit best-effort: DB uninitialized here -> skipped, not raised

    def test_handle_redteam_run_audits_report(self, monkeypatch):
        from backend.app.worker.handlers import handle_redteam_run

        written = {}

        class _FakeAuditRepo:
            def __init__(self, db):
                pass

            async def add(self, **kwargs):
                written.update(kwargs)

        class _FakeDb:
            pass

        monkeypatch.setattr("backend.app.infrastructure.db.get_database_manager", lambda: _FakeDb())
        monkeypatch.setattr(
            "backend.app.infrastructure.db.repositories.AuditRepository", _FakeAuditRepo
        )
        asyncio.run(handle_redteam_run({"tenant_id": "t1"}))
        assert written["action"] == "redteam.run"
        assert written["details"]["summary"]["cases"] > 0
