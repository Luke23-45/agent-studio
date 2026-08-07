"""
P5-2 — config promotion pipeline gates (Arch 12, P0-11).

Pure helpers for the versioned-config promotion pipeline:

    edit -> validate -> eval suite (P6-6 slot) -> canary % (P6-5 slot)
        -> promote -> auto-rollback on regression

Stays in-process and dependency-free so the pipeline mechanics are testable
without Redis/Postgres. Deterministic canary bucketing means a given request
key (end user / session) is always served the same version within a rollout,
so a canary can be correlated and rolled back cleanly.
"""

from __future__ import annotations

import hashlib

# Validation-status values recorded on a tenant_config_versions row.
VALIDATION_PENDING = "pending"
VALIDATION_VALIDATED = "validated"
VALIDATION_FAILED = "failed"
VALIDATION_REGRESSED = "regressed"

# Eval-suite (P6-6 slot) statuses. ``None``/"" means no suite recorded for
# this version; the suite is optional until Phase 6 lands.
EVAL_PENDING = "pending"
EVAL_RUNNING = "running"
EVAL_PASSED = "passed"
EVAL_FAILED = "failed"

MIN_CANARY_PERCENT = 0
MAX_CANARY_PERCENT = 100


def promotion_gate(validation_status: str | None, eval_status: str | None = None) -> tuple[bool, str | None]:
    """Block a promotion whose validation or eval gate did not pass.

    Returns ``(allowed, reason)``. A version may only be promoted once its
    validation status is ``validated``/``""`` (never ``failed``/``regressed``)
    and, when an eval suite has a recorded result, it must be ``passed``.
    """
    if validation_status in (VALIDATION_FAILED, VALIDATION_REGRESSED):
        return False, f"failed validation: {validation_status}"
    if eval_status == EVAL_FAILED:
        return False, "eval suite not passed"
    return True, None


def validate_canary_percent(canary_percent: int | None) -> bool:
    """A canary must be None or an integer in [0, 100]."""
    if canary_percent is None:
        return True
    return isinstance(canary_percent, int) and (
        MIN_CANARY_PERCENT <= canary_percent <= MAX_CANARY_PERCENT
    )


def canary_bucket(request_key: str | None, canary_percent: int | None) -> bool:
    """Deterministically route a request key to the canary rollout.

    100/None -> always the canary (full rollout); <=0 -> never. Otherwise a
    stable hash of the request key lands in the first ``canary_percent`` of a
    100-bucket space. The same key always resolves the same way for the same
    rollout, so a user's config version cannot flop between turns.
    """
    if canary_percent is None or canary_percent >= MAX_CANARY_PERCENT:
        return True
    if canary_percent <= MIN_CANARY_PERCENT or not request_key:
        return False
    digest = hashlib.sha256(request_key.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:2], "big") % 100
    return bucket < canary_percent