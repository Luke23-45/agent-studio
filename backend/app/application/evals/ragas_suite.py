"""
P6-6 — RAGAS-style RAG quality suite (Arch §13, EU-AI-Act documented
adversarial testing).

Implements the four core RAGAS metrics without a third-party dependency:

- faithfulness: are the claims in the generated answer supported by the
  retrieved contexts?
- answer_relevance: does the answer address the question?
- context_precision: how many retrieved contexts are relevant (rank-weighted)?
- context_recall: is the ground truth answerable from the contexts?

Every metric has a deterministic lexical scorer (no model calls — runs
keyless in CI) and an optional LLM judge for paraphrase tolerance. Both
variants fail closed: a judge error or an unrecognized verdict counts as
failed, never as passed. Dataset correctness (ground-truth claims must be
answerable from the contexts) is enforced at load time, mirroring the
compaction eval harness (``application/compaction/eval.py``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from backend.app.adapters.llm import BaseLLMAdapter, LLMMessage

logger = structlog.get_logger(__name__)

DEFAULT_RAGAS_THRESHOLD = 0.6

METRICS = ("faithfulness", "answer_relevance", "context_precision", "context_recall")

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_SIGNIFICANT_TOKEN = re.compile(r"[a-z0-9]{4,}")
_STOPWORDS = {
    "with", "from", "this", "that", "what", "when", "where", "which",
    "there", "their", "about", "would", "could", "should", "have", "has",
    "been", "were", "will", "than", "then", "them", "they", "your", "you",
    "into", "over", "more", "most", "some", "such", "only", "also",
}

_JUDGE_SUPPORT_PROMPT = """Context:
{context}

Claim:
{claim}

Is the claim supported by the context? Reply with exactly "yes" or "no"."""

_JUDGE_RELEVANCE_PROMPT = """Question: {question}
Context:
{context}

Is this context relevant to answering the question? Reply with exactly "yes" or "no"."""

_JUDGE_RECALL_PROMPT = """Contexts:
{contexts}

Statement: {statement}

Is the statement answerable from the contexts? Reply with exactly "yes" or "no"."""

_JUDGE_ANSWER_PROMPT = """Question: {question}
Answer: {answer}

Does the answer actually address the question? Reply with exactly "yes" or "no"."""


class RagasDatasetError(ValueError):
    """Dataset failed schema validation (never load partial data)."""


@dataclass
class RagCase:
    """One labeled RAG evaluation entry (evals/datasets/ragas/*.jsonl)."""

    name: str
    question: str
    answer: str
    contexts: list[str] = field(default_factory=list)
    ground_truths: list[str] = field(default_factory=list)


@dataclass
class RagVerdict:
    metric: str
    passed: bool
    reason: str | None = None


@dataclass
class RagCaseResult:
    name: str
    question: str
    metrics: dict[str, float]
    verdicts: list[RagVerdict]
    degraded: bool = False

    @property
    def passed_all(self) -> bool:
        return not self.degraded and all(v.passed for v in self.verdicts)


def _significant_tokens(text: str) -> set[str]:
    """Lowercased content words (>=4 chars) minus a small stopword set."""
    return {
        token
        for token in _SIGNIFICANT_TOKEN.findall(text.lower())
        if token not in _STOPWORDS
    }


def _sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text.strip())]
    return [p for p in parts if p]


def _supported_by(tokens: set[str], corpus_tokens: set[str]) -> bool:
    """All significant tokens of a claim are present in the corpus."""
    return bool(tokens) and tokens <= corpus_tokens


class LexicalRagasScorer:
    """Deterministic, model-free scoring (runs keyless in CI)."""

    @staticmethod
    def faithfulness(answer: str, contexts: Sequence[str]) -> list[RagVerdict]:
        corpus = set[str]()
        for ctx in contexts:
            corpus |= _significant_tokens(ctx)
        verdicts = []
        claims = _sentences(answer)
        if not claims:
            return [RagVerdict("faithfulness", False, "answer has no claims")]
        for claim in claims:
            tokens = _significant_tokens(claim)
            if _supported_by(tokens, corpus):
                verdicts.append(RagVerdict("faithfulness", True))
            else:
                verdicts.append(
                    RagVerdict(
                        "faithfulness", False,
                        f"unsupported claim: {claim[:80]}",
                    )
                )
        return verdicts

    @staticmethod
    def answer_relevance(question: str, answer: str) -> list[RagVerdict]:
        question_tokens = _significant_tokens(question)
        answer_tokens = _significant_tokens(answer)
        if not question_tokens:
            return [RagVerdict("answer_relevance", True)]
        verdicts = []
        for token in sorted(question_tokens):
            verdicts.append(
                RagVerdict(
                    "answer_relevance",
                    token in answer_tokens,
                    None if token in answer_tokens else f"question token absent: {token}",
                )
            )
        return verdicts

    @staticmethod
    def context_precision(question: str, contexts: Sequence[str]) -> list[RagVerdict]:
        question_tokens = _significant_tokens(question)
        relevant = [
            index
            for index, ctx in enumerate(contexts)
            if question_tokens and _significant_tokens(ctx) & question_tokens
        ]
        # RAGAS context precision: rank-weighted share of relevant contexts.
        total = len(contexts)
        if total == 0:
            return [RagVerdict("context_precision", False, "no contexts retrieved")]
        if not question_tokens:
            return [RagVerdict("context_precision", False, "question has no tokens")]
        score = 0.0
        for rank in relevant:
            relevant_before = sum(1 for r in relevant if r <= rank)
            score += relevant_before / (rank + 1)
        denominator = len(relevant) if relevant else total
        precision = score / denominator if relevant else 0.0
        verdicts = [
            RagVerdict(
                "context_precision",
                bool(precision >= 0.5),
                f"rank-weighted precision {precision:.3f}",
            )
        ]
        return verdicts

    @staticmethod
    def context_recall(ground_truths: Sequence[str], contexts: Sequence[str]) -> list[RagVerdict]:
        corpus = set[str]()
        for ctx in contexts:
            corpus |= _significant_tokens(ctx)
        verdicts = []
        if not ground_truths:
            return [RagVerdict("context_recall", False, "no ground truths")]
        for truth in ground_truths:
            for claim in _sentences(truth):
                tokens = _significant_tokens(claim)
                verdicts.append(
                    RagVerdict(
                        "context_recall",
                        _supported_by(tokens, corpus),
                        None if _supported_by(tokens, corpus)
                        else f"ground truth not answerable: {claim[:80]}",
                    )
                )
        return verdicts

    def score(self, case: RagCase) -> list[RagVerdict]:
        verdicts: list[RagVerdict] = []
        verdicts.extend(self.faithfulness(case.answer, case.contexts))
        verdicts.extend(self.answer_relevance(case.question, case.answer))
        verdicts.extend(self.context_precision(case.question, case.contexts))
        verdicts.extend(self.context_recall(case.ground_truths, case.contexts))
        return verdicts


class LLMRagasJudge:
    """Paraphrase-tolerant judge (one LLM call per claim/context/truth).

    Fail closed: a provider error or an unrecognized verdict counts as a
    failed verdict, never as passed.
    """

    def __init__(self, adapter: BaseLLMAdapter):
        self.adapter = adapter

    async def score(self, case: RagCase) -> list[RagVerdict]:
        verdicts: list[RagVerdict] = []
        for claim in _sentences(case.answer):
            verdicts.append(
                await self._judge(
                    "faithfulness",
                    _JUDGE_SUPPORT_PROMPT.format(
                        context="\n".join(case.contexts), claim=claim
                    ),
                    f"claim: {claim[:80]}",
                )
            )
        if not _sentences(case.answer):
            verdicts.append(
                RagVerdict("faithfulness", False, "answer has no claims")
            )
        for index, ctx in enumerate(case.contexts):
            verdicts.append(
                await self._judge(
                    "context_precision",
                    _JUDGE_RELEVANCE_PROMPT.format(question=case.question, context=ctx),
                    f"context[{index}] relevance",
                )
            )
        if not case.contexts:
            verdicts.append(
                RagVerdict("context_precision", False, "no contexts retrieved")
            )
        for truth in case.ground_truths:
            verdicts.append(
                await self._judge(
                    "context_recall",
                    _JUDGE_RECALL_PROMPT.format(
                        contexts="\n".join(case.contexts), statement=truth
                    ),
                    f"ground truth: {truth[:80]}",
                )
            )
        if not case.ground_truths:
            verdicts.append(
                RagVerdict("context_recall", False, "no ground truths")
            )
        verdicts.append(
            await self._judge(
                "answer_relevance",
                _JUDGE_ANSWER_PROMPT.format(
                    question=case.question, answer=case.answer
                ),
                "answer relevance",
            )
        )
        return verdicts

    async def _judge(self, metric: str, prompt: str, label: str) -> RagVerdict:
        try:
            response = await self.adapter.chat([LLMMessage(role="user", content=prompt)])
            verdict = self._parse_verdict(response.content)
        except Exception:
            verdict = None
        if verdict is None:
            return RagVerdict(metric, False, f"judge inconclusive: {label}")
        if verdict:
            return RagVerdict(metric, True)
        return RagVerdict(metric, False, label)

    @staticmethod
    def _parse_verdict(text: str | None) -> bool | None:
        cleaned = (text or "").strip().lower()
        if not cleaned:
            return None
        first = cleaned.split()[0].strip(".,!?;:'\"")
        if first == "yes":
            return True
        if first == "no":
            return False
        return None


class RagasSuiteRunner:
    """Dataset harness: score every case, aggregate per metric."""

    def __init__(self, scorer: Any | None = None):
        self.scorer = scorer or LexicalRagasScorer()

    async def evaluate_case(self, case: RagCase) -> RagCaseResult:
        try:
            verdicts = (
                await self.scorer.score(case)
                if isinstance(self.scorer, LLMRagasJudge)
                else self.scorer.score(case)
            )
            metrics = _metric_ratios(verdicts)
            return RagCaseResult(
                name=case.name,
                question=case.question,
                metrics=metrics,
                verdicts=verdicts,
            )
        except Exception as e:
            logger.warning("ragas_case_failed", case=case.name, error=str(e))
            return RagCaseResult(
                name=case.name,
                question=case.question,
                metrics={m: 0.0 for m in METRICS},
                verdicts=[],
                degraded=True,
            )

    async def evaluate_dataset(
        self, cases: Sequence[RagCase]
    ) -> list[RagCaseResult]:
        return [await self.evaluate_case(case) for case in cases]


def _metric_ratios(verdicts: Sequence[RagVerdict]) -> dict[str, float]:
    ratios = {metric: 0.0 for metric in METRICS}
    for metric in METRICS:
        group = [v for v in verdicts if v.metric == metric]
        if group:
            ratios[metric] = sum(1 for v in group if v.passed) / len(group)
    return ratios


def aggregate_results(results: Sequence[RagCaseResult]) -> dict[str, Any]:
    """Aggregate per-metric means + pass rates across cases."""
    if not results:
        return {
            "case_count": 0,
            "metrics": {metric: 0.0 for metric in METRICS},
            "degraded_cases": 0,
        }
    means = {metric: 0.0 for metric in METRICS}
    for metric in METRICS:
        values = [r.metrics[metric] for r in results]
        means[metric] = round(sum(values) / len(values), 3)
    return {
        "case_count": len(results),
        "metrics": means,
        "degraded_cases": sum(1 for r in results if r.degraded),
    }


# ---- dataset loading ------------------------------------------------------

REQUIRED_RAGAS_KEYS = ("name", "question", "answer", "contexts", "ground_truths")


def load_dataset(path: str) -> list[RagCase]:
    """Load a JSONL dataset (one RagCase per line); raises on any invalid."""
    cases: list[RagCase] = []
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
            cases.append(
                RagCase(
                    name=str(raw["name"]),
                    question=str(raw["question"]),
                    answer=str(raw["answer"]),
                    contexts=[str(c) for c in raw["contexts"]],
                    ground_truths=[str(g) for g in raw["ground_truths"]],
                )
            )
    if errors:
        raise RagasDatasetError("dataset invalid:\n" + "\n".join(errors))
    if not cases:
        raise RagasDatasetError("dataset empty")
    return cases


def _validate_case(raw: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ["entry must be a JSON object"]
    for key in REQUIRED_RAGAS_KEYS:
        if key not in raw:
            errors.append(f"missing '{key}'")
    if not isinstance(raw.get("name"), str) or not raw["name"].strip():
        errors.append("'name' must be a non-empty string")
    for key in ("question", "answer"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            errors.append(f"'{key}' must be a non-empty string")
    contexts = raw.get("contexts")
    if not isinstance(contexts, list) or not contexts:
        errors.append("'contexts' must be a non-empty array")
    elif not all(isinstance(c, str) and c.strip() for c in contexts):
        errors.append("'contexts' must be non-empty strings")
    truths = raw.get("ground_truths")
    if not isinstance(truths, list) or not truths:
        errors.append("'ground_truths' must be a non-empty array")
    elif not all(isinstance(g, str) and g.strip() for g in truths):
        errors.append("'ground_truths' must be non-empty strings")
    # Grounding enforcement: every ground-truth claim must be answerable
    # from the retrieved contexts (dataset self-consistency).
    if isinstance(contexts, list) and isinstance(truths, list):
        corpus = set[str]()
        for ctx in contexts:
            if isinstance(ctx, str):
                corpus |= _significant_tokens(ctx)
        for truth in truths:
            if not isinstance(truth, str):
                continue
            for claim in _sentences(truth):
                tokens = _significant_tokens(claim)
                if tokens and not _supported_by(tokens, corpus):
                    errors.append(
                        f"ground truth not answerable from contexts: "
                        f"{claim[:80]}"
                    )
    return errors
