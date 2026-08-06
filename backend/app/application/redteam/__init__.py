"""Red-team runner (matrix item 11.4): attack suites against the real
guardrail pipeline with a versioned, machine-readable report."""

from .service import (
    AttackCase,
    CaseResult,
    REDTEAM_SUITES,
    RUNNER_VERSION,
    RedTeamReport,
    RedTeamRunner,
    RedTeamTarget,
    SuiteDefinition,
    SuiteResult,
    create_red_team_runner,
)

__all__ = [
    "AttackCase",
    "CaseResult",
    "REDTEAM_SUITES",
    "RUNNER_VERSION",
    "RedTeamReport",
    "RedTeamRunner",
    "RedTeamTarget",
    "SuiteDefinition",
    "SuiteResult",
    "create_red_team_runner",
]
