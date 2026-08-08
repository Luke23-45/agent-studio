"""
P9-4 — Prompt resolution service (versioned prompts + A/B).

``resolve_prompt_variant`` picks the prompt version for a session:
enabled variants of a prompt name split traffic by ``target_percentage``,
and the pick is DETERMINISTIC per seed (tenant_id + session_id) so a
session keeps the same variant across turns -- the precondition for
prompt caching (P2-6) and for clean A/B measurement.
"""

from __future__ import annotations

import hashlib
import structlog
from typing import Any

logger = structlog.get_logger(__name__)


def _bucket(seed: str) -> int:
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % 100


def resolve_prompt_variant(rows: list[dict[str, Any]], *, seed: str) -> dict[str, Any] | None:
    """Deterministic A/B pick among enabled variants.

    ``rows`` must be the enabled variants (repo returns them ordered by
    version). Each row's ``target_percentage`` is a band in 0..100;
    variants whose bands do not cover the bucket simply never win. An
    empty result or a bucket outside every band means "no override" --
    the caller falls back to the default prompt.
    """
    if not rows:
        return None
    bucket = _bucket(seed)
    cumulative = 0
    for row in sorted(rows, key=lambda r: r.get("version", 0)):
        share = int(row.get("target_percentage") or 0)
        if share <= 0:
            continue
        if bucket < cumulative + share:
            return row
        cumulative += share
    return None
