"""
Compaction quality evals (Arch 8.2, P2-10).

Round-trip fact retention: facts answerable from the pre-compaction head
must remain answerable from the post-compaction summary block. This module
provides:

- scorers (lexical exact-fact and LLM judge) over a rendered summary,
- the dataset harness (``CompactionQualityEvaluator``) that summarizes
  labeled cases and scores retention per case,
- a tuning harness (prompt x keep-tokens matrix) for per-tenant prompt and
  keep/buffer configuration,
- probe extraction from turns (LLM-generated, hallucination-guarded),
- the operator surface (``CompactionQualityMonitor``): on-demand context-rot
  checks of a live thread checkpoint with a retention threshold; the
  compaction-time auto check (``CompactionService.quality_check``) reports
  the same signal on every checkpoint swap (P6-4 wires the alert transport).

Scoring is exact-fact retention by default: a probe passes when its answer
string (normalized) appears in the summary. The LLM judge is the
paraphrase-tolerant option for harness runs; both fail closed on
uncertainty (a fact is judged failed rather than assumed retained).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from backend.app.adapters.llm import BaseLLMAdapter, LLMMessage
from backend.app.application.compaction.service import (
    SummarySchema,
)
from backend.app.context import TokenEstimator

logger = structlog.get_logger(__name__)

DEFAULT_QUALITY_THRESHOLD = 0.6

_PROBES_PROMPT = """Extract verifiable factual claims from the conversation below.

Return a JSON array of objects, each with:
- "id": a short unique slug (e.g. "f1")
- "question": a question whose answer is stated in the conversation
- "answer": the exact answer string, stated as it appears in the conversation

Rules:
- Extract 3 to 8 facts; prefer specific, checkable facts (numbers, names,
  decisions, commitments).
- Never invent facts; every answer must appear verbatim in the conversation.
- Output JSON only — no markdown, no code fences.

Conversation:
{conversation}
"""

_JUDGE_PROMPT = """You are a fact-retention judge for conversation summaries.

Summary:
{summary}

Question: {question}
Expected answer: {answer}

Does the summary contain the answer to the question? Reply with exactly
"yes" or "no"."""


class ProbeExtractionError(Exception):
    """Probe generation failed (malformed model output or provider error)."""


@dataclass
class FactProbe:
    """One round-trip fact: answerable before, must be answerable after."""

    id: str
    question: str
    answer: str


@dataclass
class FactVerdict:
    probe_id: str
    passed: bool
    reason: str | None = None


class LexicalFactScorer:
    """Exact-fact retention: normalized answer string must appear."""

    @staticmethod
    def _normalize(text: str) -> str:
        """Lowercase, collapse whitespace, strip punctuation."""
        import re

        normalized = re.sub(r"\s+", " ", text.lower())
        return normalized.strip(" \t\n.,!?;:'\"-")

    @classmethod
    def _contains(cls, corpus: str, answer: str) -> bool:
        return cls._normalize(answer) != "" and cls._normalize(answer) in cls._normalize(corpus)

    async def score(
        self, summary_text: str, probes: Sequence[FactProbe]
    ) -> list[FactVerdict]:
        if not (summary_text or "").strip():
            return [
                FactVerdict(p.id, False, "summary empty") for p in probes
            ]
        verdicts = []
        for probe in probes:
            if self._contains(summary_text, probe.answer):
                verdicts.append(FactVerdict(probe.id, True))
            else:
                verdicts.append(
                    FactVerdict(probe.id, False, "answer absent from summary")
                )
        return verdicts


class LLMJudgeScorer:
    """Paraphrase-tolerant retention judge (one LLM call per probe).

    Fail closed: a judge error or an unrecognized verdict counts as failed.
    """

    def __init__(self, adapter: BaseLLMAdapter, prompt: str | None = None):
        self.adapter = adapter
        self.prompt = prompt or _JUDGE_PROMPT

    async def score(
        self, summary_text: str, probes: Sequence[FactProbe]
    ) -> list[FactVerdict]:
        if not (summary_text or "").strip():
            return [
                FactVerdict(p.id, False, "summary empty") for p in probes
            ]
        return [await self._judge(probe, summary_text) for probe in probes]

    async def _judge(self, probe: FactProbe, summary_text: str) -> FactVerdict:
        prompt = self.prompt.format(
            summary=summary_text,
            question=probe.question,
            answer=probe.answer,
        )
        try:
            response = await self.adapter.chat(
                [LLMMessage(role="user", content=prompt)]
            )
            verdict = self._parse_verdict(response.content)
        except Exception:
            verdict = None
        if verdict is None:
            return FactVerdict(
                probe.id, False, "judge inconclusive (fail closed)"
            )
        if verdict:
            return FactVerdict(probe.id, True)
        return FactVerdict(probe.id, False, "judge: answer absent")

    @staticmethod
    def _parse_verdict(text: str | None) -> bool | None:
        """First token yes/no (case- and punctuation-insensitive)."""
        cleaned = (text or "").strip().lower()
        if not cleaned:
            return None
        first = cleaned.split()[0].strip(".,!?;:'\"")
        if first == "yes":
            return True
        if first == "no":
            return False
        return None


@dataclass
class EvalCase:
    """One labeled dataset entry (evals/datasets/compaction/*.jsonl)."""

    name: str
    turns: list[dict[str, Any]]  # [{role, redacted_content}, ...] head turns
    probes: list[FactProbe]
    keep_tokens: int | None = None  # None = compact the whole case (no split)
    buffer_tokens: int | None = None  # recorded in reports; trigger knob only


@dataclass
class EvalCaseResult:
    name: str
    retention_ratio: float
    passed: list[str]  # probe ids
    failed: list[dict[str, Any]]  # {"id", "question", "answer", "reason"}
    skipped: list[str]  # probe ids not answerable before compaction
    degraded: bool  # no usable summary was produced
    summary: str | None


@dataclass
class TuningResult:
    """One prompt x keep-tokens configuration over a dataset."""

    prompt_label: str
    keep_tokens: int | None
    case_results: list[EvalCaseResult] = field(default_factory=list)

    @property
    def avg_retention(self) -> float:
        totals = [c.retention_ratio for c in self.case_results]
        return sum(totals) / len(totals) if totals else 0.0


class CompactionQualityEvaluator:
    """Dataset harness: summarize -> render -> score probes per case."""

    def __init__(
        self,
        generator: Any,
        scorer: Any | None = None,
        estimator: TokenEstimator | None = None,
    ):
        self.generator = generator  # LLMSummaryGenerator or duck-type
        self.scorer = scorer or LexicalFactScorer()
        self.estimator = estimator or TokenEstimator()

    async def evaluate_case(self, case: EvalCase) -> EvalCaseResult:
        head, skipped = self._head_and_skipped(case)
        scored_probes = [p for p in case.probes if p.id not in skipped]
        try:
            payload = await self.generator.summarize(head)
            errors = SummarySchema.validate(payload)
            if errors:
                return EvalCaseResult(
                    case.name, 0.0, [], [], skipped, True, None
                )
            rendered = SummarySchema.render(payload)
            verdicts = await self.scorer.score(rendered, scored_probes)
            passed = [v.probe_id for v in verdicts if v.passed]
            failed = [
                {
                    "id": v.probe_id,
                    "question": p.question,
                    "answer": p.answer,
                    "reason": v.reason,
                }
                for v, p in zip(verdicts, scored_probes)
                if not v.passed
            ]
            ratio = len(passed) / len(verdicts) if verdicts else 0.0
            return EvalCaseResult(
                case.name, ratio, passed, failed, skipped, False, rendered
            )
        except Exception as e:
            logger.warning(
                "compaction_eval_case_failed",
                case=case.name,
                error=str(e),
            )
            return EvalCaseResult(case.name, 0.0, [], [], skipped, True, None)

    async def evaluate_dataset(
        self, cases: Sequence[EvalCase]
    ) -> list[EvalCaseResult]:
        return [await self.evaluate_case(case) for case in cases]

    def _head_and_skipped(
        self, case: EvalCase
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Apply the keep-tokens split; skip probes outside the compacted head.

        A probe whose answer lies in the kept-live tail (or is ungrounded)
        is skipped: the summary never needs it, so its absence is not a
        retention failure. Dataset correctness (probes answerable in the
        full turns) is enforced at load time.
        """
        head = case.turns
        if case.keep_tokens:
            head = _split_head(case.turns, case.keep_tokens, self.estimator)
        corpus = "\n".join(t.get("redacted_content") or "" for t in head)
        skipped = [
            p.id
            for p in case.probes
            if not LexicalFactScorer._contains(corpus, p.answer)
        ]
        return list(head), skipped


def _split_head(
    turns: Sequence[dict[str, Any]], keep_tokens: int, estimator: TokenEstimator
) -> list[dict[str, Any]]:
    """Newest ~keep_tokens stay live; return the older head (mirrors
    ``CompactionService._split`` token walk)."""
    acc = 0
    boundary = 0
    for i in range(len(turns) - 1, -1, -1):
        tokens = estimator.estimate(turns[i].get("redacted_content") or "")
        if acc + tokens > keep_tokens:
            boundary = i + 1
            break
        acc += tokens
    return list(turns[:boundary])


def aggregate_results(
    results: Sequence[EvalCaseResult],
) -> dict[str, Any]:
    """Aggregate retention stats across cases (per-fact, not per-case)."""
    if not results:
        return {
            "case_count": 0,
            "avg_retention": 0.0,
            "min_retention": 0.0,
            "facts_passed": 0,
            "facts_total": 0,
            "degraded_cases": 0,
        }
    total = sum(len(r.passed) + len(r.failed) for r in results)
    passed = sum(len(r.passed) for r in results)
    return {
        "case_count": len(results),
        "avg_retention": round(passed / total, 3) if total else 0.0,
        "min_retention": round(min(r.retention_ratio for r in results), 3),
        "facts_passed": passed,
        "facts_total": total,
        "degraded_cases": sum(1 for r in results if r.degraded),
    }


async def run_tuning_harness(
    cases: Sequence[EvalCase],
    adapter: BaseLLMAdapter,
    prompts: Sequence[str],
    keep_tokens_options: Sequence[int | None] = (None,),
) -> list[TuningResult]:
    """Prompt x keep-tokens matrix over the dataset (per-tenant tuning)."""
    rows: list[TuningResult] = []
    for prompt_index, prompt in enumerate(prompts):
        for keep in keep_tokens_options:
            generator = _make_generator(adapter, prompt)
            evaluator = CompactionQualityEvaluator(generator)
            results = await evaluator.evaluate_dataset(cases)
            label = (
                f"prompt-{prompt_index}" if prompt_index else "prompt-base"
            )
            rows.append(
                TuningResult(
                    prompt_label=label,
                    keep_tokens=keep,
                    case_results=list(results),
                )
            )
    return rows


def _make_generator(adapter: BaseLLMAdapter, prompt: str) -> Any:
    from backend.app.application.compaction.service import (
        LLMSummaryGenerator,
    )

    return LLMSummaryGenerator(adapter=adapter, prompt=prompt)


# ---- dataset loading ------------------------------------------------------


def load_dataset(path: str) -> list[EvalCase]:
    """Load a JSONL dataset (one EvalCase per line); raises on invalid."""
    cases: list[EvalCase] = []
    errors: list[str] = []
    with open(path, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"line {line_no}: invalid JSON ({e})")
                continue
            case_errors = _validate_case(raw)
            if case_errors:
                errors.extend(f"line {line_no}: {e}" for e in case_errors)
                continue
            probes = [
                FactProbe(
                    id=str(item["id"]),
                    question=str(item["question"]),
                    answer=str(item["answer"]),
                )
                for item in raw["probes"]
            ]
            cases.append(
                EvalCase(
                    name=str(raw["name"]),
                    turns=[dict(t) for t in raw["turns"]],
                    probes=probes,
                    keep_tokens=raw.get("keep_tokens"),
                    buffer_tokens=raw.get("buffer_tokens"),
                )
            )
    if errors:
        raise ValueError("dataset invalid:\n" + "\n".join(errors))
    if not cases:
        raise ValueError("dataset empty")
    return cases


def _validate_case(raw: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ["entry must be a JSON object"]
    if not isinstance(raw.get("name"), str) or not raw["name"].strip():
        errors.append("missing 'name'")
    turns = raw.get("turns")
    if not isinstance(turns, list) or not turns:
        errors.append("'turns' must be a non-empty array")
    else:
        for index, turn in enumerate(turns):
            if not isinstance(turn, dict) or not isinstance(
                turn.get("redacted_content"), str
            ):
                errors.append(f"turns[{index}] must be an object with 'redacted_content'")
    probes = raw.get("probes")
    if not isinstance(probes, list) or not probes:
        errors.append("'probes' must be a non-empty array")
    else:
        ids = set()
        corpus = "\n".join(
            str(t.get("redacted_content") or "") for t in (turns or [])
        )
        for index, probe in enumerate(probes):
            if not isinstance(probe, dict):
                errors.append(f"probes[{index}] must be an object")
                continue
            for key in ("id", "question", "answer"):
                if not isinstance(probe.get(key), str) or not probe[key].strip():
                    errors.append(f"probes[{index}] missing non-empty '{key}'")
            probe_id = probe.get("id")
            if probe_id in ids:
                errors.append(f"duplicate probe id '{probe_id}'")
            ids.add(probe_id)
            answer = probe.get("answer")
            if isinstance(answer, str) and not LexicalFactScorer._contains(
                corpus, answer
            ):
                errors.append(
                    f"probes[{index}] answer not verbatim in the turns "
                    f"(ungrounded: '{answer}')"
                )
    keep = raw.get("keep_tokens")
    if keep is not None and (not isinstance(keep, int) or keep <= 0):
        errors.append("'keep_tokens' must be a positive integer")
    return errors


# ---- probe extraction ------------------------------------------------------


async def generate_probes_from_turns(
    turns: Sequence[dict[str, Any]],
    adapter: BaseLLMAdapter,
    max_probes: int = 8,
) -> list[FactProbe]:
    """LLM-extract verifiable probes from turns (hallucination-guarded).

    Every returned answer appears verbatim (normalized) in the turns; the
    model cannot invent facts into the check. Raises ProbeExtractionError
    on provider failure or unusable output (never returns partial garbage).
    """
    conversation = "\n".join(
        f"{turn.get('role', 'user')}: {turn['redacted_content']}"
        for turn in turns
        if turn.get("redacted_content")
    )
    if not conversation:
        return []
    try:
        response = await adapter.chat(
            [LLMMessage(role="user", content=_PROBES_PROMPT.format(conversation=conversation))]
        )
        payload = _parse_json_payload(response.content)
    except ProbeExtractionError:
        raise
    except Exception as e:
        raise ProbeExtractionError(f"probe extraction failed: {e}") from e
    if not isinstance(payload, list):
        raise ProbeExtractionError(
            "probe extraction returned a non-array payload"
        )
    corpus = conversation
    probes: list[FactProbe] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            continue
        probe_id = item.get("id") or f"f{index + 1}"
        question = item.get("question")
        answer = item.get("answer")
        if not isinstance(probe_id, str) or not probe_id.strip():
            continue
        if not isinstance(question, str) or not question.strip():
            continue
        if not isinstance(answer, str) or not answer.strip():
            continue
        if not LexicalFactScorer._contains(corpus, answer):
            continue  # hallucination guard: drop ungrounded answers
        probes.append(
            FactProbe(id=probe_id, question=question, answer=answer)
        )
        if len(probes) >= max_probes:
            break
    return probes


def _parse_json_payload(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ProbeExtractionError(
            f"probe extraction returned invalid JSON: {e}"
        ) from e


# ---- operator surface (context-rot detection) ------------------------------


@dataclass
class CheckpointEvalResult:
    tenant_id: str
    thread_id: str
    retention_ratio: float
    passed: list[str]
    failed: list[dict[str, Any]]
    degraded: bool
    low_quality: bool


class CompactionQualityMonitor:
    """On-demand context-rot check of a live thread's summary checkpoint.

    Scores the current summary block against operator-supplied probes (or
    probes generated from a retained head snapshot). Below the threshold the
    result is flagged ``low_quality`` and a warning is logged — the signal
    P6-4's alert transport consumes. Never raises: a failed check degrades
    to low_quality (fail closed).
    """

    def __init__(
        self,
        threads: Any,
        generator: Any,
        scorer: Any | None = None,
        threshold: float = DEFAULT_QUALITY_THRESHOLD,
    ):
        self.threads = threads
        self.generator = generator
        self.scorer = scorer or LexicalFactScorer()
        self.threshold = threshold

    async def evaluate_thread(
        self,
        tenant_id: str,
        thread_id: str,
        probes: Sequence[FactProbe],
    ) -> CheckpointEvalResult:
        row = await self.threads.get_thread(tenant_id, thread_id)
        block = (row or {}).get("summary_block") or {}
        content = (block.get("content") or "").strip()
        degraded = not content
        if degraded:
            verdicts = [
                FactVerdict(p.id, False, "no summary checkpoint") for p in probes
            ]
        else:
            try:
                verdicts = await self.scorer.score(content, probes)
            except Exception as e:
                logger.warning(
                    "compaction_quality_check_failed",
                    tenant_id=tenant_id,
                    thread_id=thread_id,
                    error=str(e),
                )
                verdicts = [
                    FactVerdict(p.id, False, "check failed (fail closed)")
                    for p in probes
                ]
        passed = [v.probe_id for v in verdicts if v.passed]
        failed = [
            {
                "id": v.probe_id,
                "question": p.question,
                "answer": p.answer,
                "reason": v.reason,
            }
            for v, p in zip(verdicts, probes)
            if not v.passed
        ]
        ratio = len(passed) / len(verdicts) if verdicts else 0.0
        low_quality = ratio < self.threshold
        if low_quality:
            logger.warning(
                "compaction_quality_low",
                tenant_id=tenant_id,
                thread_id=thread_id,
                retention_ratio=round(ratio, 3),
                probed=len(verdicts),
                threshold=self.threshold,
                degraded=degraded,
                failed=[v.probe_id for v in verdicts if not v.passed],
            )
        else:
            logger.info(
                "compaction_quality_ok",
                tenant_id=tenant_id,
                thread_id=thread_id,
                retention_ratio=round(ratio, 3),
                probed=len(verdicts),
            )
        return CheckpointEvalResult(
            tenant_id=tenant_id,
            thread_id=thread_id,
            retention_ratio=ratio,
            passed=passed,
            failed=failed,
            degraded=degraded,
            low_quality=low_quality,
        )
