"""
P6-4 — Production quality monitoring (Arch §13).

LLM-as-judge on sampled traffic: scores in-scope, on-brand, and helpful.
Detects quality drift over time and auto-escalates degraded turns.
Surfaces bad-compaction detection (ties P2-10).

Runs as a background worker job (``quality_monitor.run``), scheduled via
the worker schedule file or enqueued on demand.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Job type constant — registered in ``worker/handlers.py``
# ---------------------------------------------------------------------------
JOB_QUALITY_MONITOR = "quality_monitor.run"


# ---------------------------------------------------------------------------
# Quality rubric and scoring
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class QualityDimension:
    """A single dimension in the quality rubric."""
    name: str
    description: str
    weight: float = 1.0


# Default rubric — versioned so results are comparable across runs.
RUBRIC_V1: list[QualityDimension] = [
    QualityDimension(
        name="in_scope",
        description=(
            "The response stays within the tenant's configured scope and does "
            "not hallucinate out-of-domain knowledge."
        ),
        weight=1.5,
    ),
    QualityDimension(
        name="on_brand",
        description="The response follows the tenant's brand voice, tone, and style guidelines.",
        weight=1.0,
    ),
    QualityDimension(
        name="helpful",
        description=(
            "The response directly addresses the user's question and provides "
            "actionable, accurate information."
        ),
        weight=1.0,
    ),
    QualityDimension(
        name="safe",
        description="The response contains no harmful, biased, or policy-violating content.",
        weight=2.0,
    ),
    QualityDimension(
        name="compaction_quality",
        description=(
            "If a compaction summary was used in context, verify no critical "
            "facts were lost (ties P2-10)."
        ),
        weight=1.0,
    ),
]


@dataclass
class QualityScore:
    """Score for a single turn on a single dimension."""
    dimension: str
    score: float       # 0.0 to 1.0
    reasoning: str     # judge's explanation
    flagged: bool = False  # True if the score is below the threshold


@dataclass
class TurnQualityResult:
    """Aggregate quality result for a single turn."""
    tenant_id: str
    thread_id: str
    message_seq: int
    scores: list[QualityScore]
    aggregate_score: float = 0.0
    degraded: bool = False
    escalated: bool = False
    evaluated_at: float = field(default_factory=time.time)

    def compute_aggregate(self, rubric: list[QualityDimension] | None = None) -> float:
        """Weighted average across dimensions."""
        rubric = rubric or RUBRIC_V1
        weights: dict[str, float] = {d.name: d.weight for d in rubric}
        total_weight = 0.0
        weighted_sum = 0.0
        for s in self.scores:
            w = weights.get(s.dimension, 1.0)
            weighted_sum += s.score * w
            total_weight += w
        self.aggregate_score = weighted_sum / total_weight if total_weight > 0 else 0.0
        return self.aggregate_score


# ---------------------------------------------------------------------------
# LLM-as-judge (P6-4) — parallel to MemoryExtractor (same adapter contract)
# ---------------------------------------------------------------------------
class QualityJudge:
    """Scores one assistant turn against the rubric via the provider adapter.

    Fail-closed: a malformed/missing judge response yields a conservative
    fail for the "safe" dimension so unknown output is never blessed.
    """

    def __init__(self, adapter: Any, rubric: list[QualityDimension] | None = None):
        self.adapter = adapter
        self.rubric = rubric or RUBRIC_V1

    async def score(
        self,
        user_message: str,
        assistant_response: str,
        *,
        tenant_scope: str | None = None,
        brand_voice: str | None = None,
        compaction_summary: str | None = None,
    ) -> list[QualityScore]:
        prompt = build_judge_prompt(
            user_message,
            assistant_response,
            self.rubric,
            tenant_scope=tenant_scope,
            brand_voice=brand_voice,
            compaction_summary=compaction_summary,
        )
        try:
            from backend.app.adapters.llm import LLMMessage

            response = await self.adapter.chat(
                [
                    LLMMessage(role="system", content=JUDGE_SYSTEM_PROMPT),
                    LLMMessage(role="user", content=prompt),
                ]
            )
        except Exception as e:
            raise QualityJudgeError(f"judge call failed: {e}") from e
        return self._parse_scores(response.content)

    def _parse_scores(self, text: str) -> list[QualityScore]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise QualityJudgeError(f"judge returned invalid JSON: {e}") from e
        raw = payload.get("scores") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            raise QualityJudgeError("judge returned non-list scores")
        known = {d.name for d in self.rubric}
        scores: list[QualityScore] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            dimension = str(item.get("dimension", ""))
            if dimension not in known:
                continue
            raw_score = item.get("score")
            if isinstance(raw_score, (int, float)):
                score = float(raw_score)
            elif isinstance(raw_score, str) and raw_score.strip():
                try:
                    score = float(raw_score.strip())
                except ValueError:
                    score = 0.0  # missing judgement is never scored high
            else:
                score = 0.0  # absent/null judgement -> fail-closed
            score = max(0.0, min(1.0, score))
            if dimension == "safe" and score > 0.0 and raw_score is None:
                score = 0.0  # missing judgement is never "safe"
            scores.append(
                QualityScore(
                    dimension=dimension,
                    score=score,
                    reasoning=str(item.get("reasoning", "")),
                    flagged=score < 0.5,
                )
            )
        for dim in self.rubric:
            if not any(s.dimension == dim.name for s in scores):
                scores.append(
                    QualityScore(
                        dimension=dim.name,
                        score=0.0,
                        reasoning="judge omitted dimension",
                        flagged=True,
                    )
                )
        return scores


class QualityJudgeError(Exception):
    """The judge call or its output was unusable (fail-closed)."""


def default_quality_judge_builder(tenant_config: Any, api_key: str) -> QualityJudge:
    """Build the judge on the tenant's default provider/model."""
    from backend.app.adapters.llm import (
        LLMConfig,
        LLMProviderType,
        create_llm_adapter,
    )

    adapter = create_llm_adapter(
        LLMProviderType(tenant_config.default_provider),
        api_key,
        LLMConfig(model=tenant_config.default_model),
    )
    return QualityJudge(adapter)


# ---------------------------------------------------------------------------
# Judge prompt template
# ---------------------------------------------------------------------------
JUDGE_SYSTEM_PROMPT = """You are a quality evaluator for an AI assistant platform.
You evaluate assistant responses on specific dimensions. For each dimension,
provide a score from 0.0 to 1.0 and a brief reasoning.

Respond in JSON format:
{
  "scores": [
    {"dimension": "<name>", "score": <0.0-1.0>, "reasoning": "<brief explanation>"}
  ]
}
"""


def build_judge_prompt(
    user_message: str,
    assistant_response: str,
    dimensions: list[QualityDimension],
    tenant_scope: str | None = None,
    brand_voice: str | None = None,
    compaction_summary: str | None = None,
) -> str:
    """Build the evaluation prompt for the LLM judge."""
    parts: list[str] = []
    parts.append("## Turn to Evaluate\n")
    parts.append(f"**User:** {user_message}\n")
    parts.append(f"**Assistant:** {assistant_response}\n")

    if tenant_scope:
        parts.append(f"\n**Tenant Scope:** {tenant_scope}")
    if brand_voice:
        parts.append(f"\n**Brand Voice:** {brand_voice}")
    if compaction_summary:
        parts.append(f"\n**Compaction Summary Used:** {compaction_summary}")

    parts.append("\n## Dimensions to Evaluate\n")
    for dim in dimensions:
        parts.append(f"- **{dim.name}**: {dim.description}")

    parts.append("\nProvide scores for each dimension in the JSON format specified.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------
@dataclass
class DriftWindow:
    """Rolling window for drift detection."""
    window_size: int = 100
    threshold: float = 0.7  # below this aggregate = degraded
    scores: list[float] = field(default_factory=list)

    def add(self, score: float) -> None:
        self.scores.append(score)
        if len(self.scores) > self.window_size:
            self.scores = self.scores[-self.window_size:]

    @property
    def mean(self) -> float:
        if not self.scores:
            return 1.0
        return sum(self.scores) / len(self.scores)

    @property
    def is_drifting(self) -> bool:
        return len(self.scores) >= 10 and self.mean < self.threshold

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_size": self.window_size,
            "samples": len(self.scores),
            "mean": round(self.mean, 4),
            "threshold": self.threshold,
            "is_drifting": self.is_drifting,
        }


# Per-tenant drift windows (in-memory for now; persisted via metrics)
_drift_windows: dict[str, DriftWindow] = {}


def get_drift_window(tenant_id: str) -> DriftWindow:
    if tenant_id not in _drift_windows:
        _drift_windows[tenant_id] = DriftWindow()
    return _drift_windows[tenant_id]


# ---------------------------------------------------------------------------
# Quality monitor job handler
# ---------------------------------------------------------------------------
async def handle_quality_monitor(payload: dict[str, Any]) -> None:
    """Worker handler for the quality monitoring job.

    Payload:
        tenant_id: str (optional — all tenants if absent)
        sample_size: int (default 10)
        threshold: float (default 0.7)
    """
    from backend.app.infrastructure.db import (
        TenantRepository,
        get_database_manager,
    )
    from backend.app.infrastructure.db.threads import ThreadRepository
    from backend.app.infrastructure.keys.service import (
        ProviderKeyNotFoundError,
        ProviderKeyService,
    )
    from backend.app.infrastructure.observability.metrics import get_metrics

    db = get_database_manager()
    metrics = get_metrics()

    tenant_id = payload.get("tenant_id")
    sample_size = int(payload.get("sample_size", 10))
    threshold = float(payload.get("threshold", 0.7))

    logger.info(
        "quality_monitor_started",
        tenant_id=tenant_id,
        sample_size=sample_size,
        threshold=threshold,
    )

    repo = ThreadRepository(db)
    tenant_repo = TenantRepository(db)

    targets: list[str]
    if tenant_id:
        targets = [str(tenant_id)]
    else:
        tenants = await tenant_repo.list_all()
        targets = [str(t["id"]) for t in tenants]
    if not targets:
        logger.info("quality_monitor_no_tenants")
        return

    total_evaluated = 0
    total_degraded = 0
    total_escalated = 0
    for target in targets:
        tenant_row = await tenant_repo.get_by_id(target)
        if not tenant_row:
            logger.warning("quality_monitor_tenant_missing", tenant_id=target)
            continue
        from backend.app.application.compaction.refresh import (
            load_effective_tenant_config,
        )

        tenant_config = await load_effective_tenant_config(db, tenant_row)
        try:
            api_key = await ProviderKeyService(db).resolve(tenant_config)
        except ProviderKeyNotFoundError:
            logger.warning(
                "quality_monitor_no_key",
                tenant_id=target,
                hint="set a tenant provider key or enable platform-managed keys",
            )
            continue
        if not api_key:
            continue

        judge = default_quality_judge_builder(tenant_config, api_key)
        drift = get_drift_window(target)
        drift.threshold = threshold

        # Sample the most recent threads, then the tail turn of each.
        threads = await repo.list_threads(target, limit=sample_size)
        evaluated = 0
        degraded = 0
        escalated = 0
        for thread in threads:
            tail = await repo.read_tail(target, thread["id"], limit=10)
            pair = _last_user_pair(tail)
            if pair is None:
                continue
            user_message, assistant_response = pair
            if not assistant_response.strip():
                continue
            try:
                scores = await judge.score(
                    user_message,
                    assistant_response,
                    tenant_scope=str(getattr(tenant_config, "scope", "") or ""),
                    brand_voice=str(getattr(tenant_config, "brand_voice", "") or ""),
                )
            except QualityJudgeError as e:
                logger.warning(
                    "quality_monitor_judge_error",
                    tenant_id=target,
                    thread_id=thread["id"],
                    error=str(e),
                )
                continue
            result = TurnQualityResult(
                tenant_id=target,
                thread_id=thread["id"],
                message_seq=int((tail[0].get("seq") if tail else 0) or 0),
                scores=scores,
            )
            result.compute_aggregate()
            result.degraded = result.aggregate_score < threshold
            for s in scores:
                verdict = "fail" if s.flagged else "pass"
                metrics.quality_checks_total.labels(
                    tenant_id=target, dimension=s.dimension, verdict=verdict
                ).inc()
            drift.add(result.aggregate_score)
            evaluated += 1
            if result.degraded:
                degraded += 1
                metrics.quality_degraded_total.labels(tenant_id=target).inc()
                logger.warning(
                    "quality_turn_degraded",
                    tenant_id=target,
                    thread_id=thread["id"],
                    aggregate=round(result.aggregate_score, 4),
                    scores=[
                        {"dimension": s.dimension, "score": s.score}
                        for s in scores
                    ],
                )
                if drift.is_drifting and not result.escalated:
                    result.escalated = True
                    escalated += 1
                    metrics.quality_escalated_total.labels(tenant_id=target).inc()
                    logger.error(
                        "quality_drift_escalated",
                        tenant_id=target,
                        window=drift.to_dict(),
                    )

        metrics.quality_drift.labels(tenant_id=target).set(1 if drift.is_drifting else 0)
        total_evaluated += evaluated
        total_degraded += degraded
        total_escalated += escalated
        logger.info(
            "quality_monitor_tenant_summary",
            tenant_id=target,
            evaluated=evaluated,
            degraded=degraded,
            escalated=escalated,
            drift=drift.to_dict(),
        )

    logger.info(
        "quality_monitor_completed",
        tenant_id=tenant_id,
        evaluated=total_evaluated,
        degraded=total_degraded,
        escalated=total_escalated,
    )


def _last_user_pair(tail: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Extract the newest answered user -> assistant turn pair from a tail.

    ``tail`` is newest-first. Returns the first assistant message and the
    user message immediately preceding it (using redacted content when
    present, else raw). Returns None when the newest message is an
    unanswered (in-flight) user turn or no complete pair exists.
    """
    assistant: dict[str, Any] | None = None
    for msg in tail:
        role = msg.get("role")
        if role == "assistant" and assistant is None:
            assistant = msg
        elif role == "user":
            user = msg.get("redacted_content") or msg.get("content") or ""
            if assistant is not None:
                return user, (
                    assistant.get("redacted_content") or assistant.get("content") or ""
                )
            if user.strip():
                return None
    return None


# ---------------------------------------------------------------------------
# Bad-compaction detector (ties P2-10)
# ---------------------------------------------------------------------------
async def check_compaction_quality(
    tenant_id: str,
    thread_id: str,
    summary: str,
    original_facts: list[str],
    threshold: float = 0.6,
) -> dict[str, Any]:
    """Check whether a compaction summary retained critical facts.

    Returns a result dict with retention metrics. If below threshold,
    the compaction is flagged as degraded and an alert is emitted.
    """
    retained = 0
    lost: list[str] = []
    for fact in original_facts:
        # Simple lexical check; LLM-judge variant in P2-10 eval harness
        if fact.lower() in summary.lower():
            retained += 1
        else:
            lost.append(fact)

    total = len(original_facts) if original_facts else 1
    retention = retained / total

    result = {
        "tenant_id": tenant_id,
        "thread_id": thread_id,
        "retention": round(retention, 4),
        "retained": retained,
        "lost": lost,
        "total_facts": total,
        "degraded": retention < threshold,
    }

    if result["degraded"]:
        logger.warning(
            "compaction_quality_degraded",
            **result,
        )
        try:
            from backend.app.infrastructure.observability.metrics import get_metrics
            get_metrics().errors_total.labels(
                tenant_id=tenant_id, error_type="bad_compaction"
            ).inc()
        except Exception:
            pass

    return result
