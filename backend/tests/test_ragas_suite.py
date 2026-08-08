"""
P6-6 — RAGAS-style suite unit tests (Arch §13).

Dataset validation (schema + grounding), lexical scorer math, aggregate
math, fail-closed LLM judge behavior, and per-case degradation on scorer
failure — the CI gate is exercised end-to-end by evals.yml, the scorer
semantics live here.
"""

import asyncio
from pathlib import Path

import pytest

from backend.app.application.evals.ragas_suite import (
    LLMRagasJudge,
    LexicalRagasScorer,
    RagasDatasetError,
    RagasSuiteRunner,
    RagCase,
    aggregate_results,
    load_dataset,
)


class _FakeAdapter:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def chat(self, messages):
        self.calls += 1
        if not self.replies:
            raise RuntimeError("provider down")
        return type("R", (), {"content": self.replies.pop(0)})()


def _case(**overrides):
    data = dict(
        name="c1",
        question="What is the refund window for annual plans?",
        answer="The refund window is thirty days. Plans are cancelled within thirty days.",
        contexts=[
            "Annual plans can be cancelled within thirty days of purchase for a full refund; the refund window is thirty days.",
            "Monthly plans renew automatically on the first of every month.",
        ],
        ground_truths=["Annual plans can be cancelled within thirty days."],
    )
    data.update(overrides)
    return RagCase(**data)


class TestDatasetValidation:
    def test_valid_dataset_loads(self, tmp_path):
        path = Path(tmp_path) / "good.jsonl"
        path.write_text(
            '{"name": "c", "question": "q", "answer": "a.", "contexts": ["ctx with words"], '
            '"ground_truths": ["ctx with words"]}\n',
            encoding="utf-8",
        )
        cases = load_dataset(str(path))
        assert len(cases) == 1
        assert cases[0].name == "c"

    def test_missing_fields_rejected(self, tmp_path):
        path = Path(tmp_path) / "bad.jsonl"
        path.write_text('{"name": "c"}\n', encoding="utf-8")
        with pytest.raises(RagasDatasetError) as exc:
            load_dataset(str(path))
        assert "missing 'question'" in str(exc.value)

    def test_empty_contexts_rejected(self, tmp_path):
        path = Path(tmp_path) / "bad.jsonl"
        path.write_text(
            '{"name": "c", "question": "q", "answer": "a.", "contexts": [], '
            '"ground_truths": ["ctx"]}\n',
            encoding="utf-8",
        )
        with pytest.raises(RagasDatasetError) as exc:
            load_dataset(str(path))
        assert "'contexts' must be a non-empty array" in str(exc.value)

    def test_ungrounded_ground_truth_rejected(self, tmp_path):
        path = Path(tmp_path) / "bad.jsonl"
        path.write_text(
            '{"name": "c", "question": "q", "answer": "a.", '
            '"contexts": ["the refund window is thirty days"], '
            '"ground_truths": ["orbital telemetry calibrates"]}\n',
            encoding="utf-8",
        )
        with pytest.raises(RagasDatasetError) as exc:
            load_dataset(str(path))
        assert "ground truth not answerable" in str(exc.value)

    def test_invalid_json_line_rejected(self, tmp_path):
        path = Path(tmp_path) / "bad.jsonl"
        path.write_text("{not json}\n", encoding="utf-8")
        with pytest.raises(RagasDatasetError) as exc:
            load_dataset(str(path))
        assert "invalid JSON" in str(exc.value)


class TestLexicalScoring:
    def test_faithfulness_supported_and_unsupported(self):
        case = _case(
            answer="The refund window is thirty days. Orbital telemetry calibrates nightly."
        )
        verdicts = LexicalRagasScorer.faithfulness(case.answer, case.contexts)
        claims = [v.passed for v in verdicts if v.metric == "faithfulness"]
        assert claims == [True, False]

    def test_faithfulness_empty_answer_fails_closed(self):
        verdicts = LexicalRagasScorer.faithfulness("", ["anything"])
        assert len(verdicts) == 1
        assert verdicts[0].passed is False

    def test_answer_relevance_tracks_question_tokens(self):
        verdicts = LexicalRagasScorer.answer_relevance(
            "What is the refund window?", "The refund window is thirty days."
        )
        assert all(v.passed for v in verdicts if v.metric == "answer_relevance")

        missing = LexicalRagasScorer.answer_relevance(
            "What is the refund window?", "Nothing about windows here."
        )
        assert not all(v.passed for v in missing if v.metric == "answer_relevance")

    def test_context_precision_rank_weighted(self):
        # Relevant contexts at ranks 1 and 3 (1-based): precision@1=1, precision@3=2/3.
        case = _case(
            contexts=[
                "The refund window is thirty days.",
                "Monthly renewals are automatic.",
                "The refund window applies to annual plans as well.",
            ]
        )
        verdicts = LexicalRagasScorer.context_precision(case.question, case.contexts)
        assert len(verdicts) == 1
        assert verdicts[0].passed is True
        # Rank-weighted: (1/1 + 2/3) / 2 = 0.833 >= 0.5.
        assert "0.833" in verdicts[0].reason

    def test_context_precision_no_contexts_fails(self):
        verdicts = LexicalRagasScorer.context_precision("q", [])
        assert verdicts[0].passed is False
        assert "no contexts" in verdicts[0].reason

    def test_context_recall_grounded_truth(self):
        verdicts = LexicalRagasScorer.context_recall(
            ["Annual plans can be cancelled within thirty days.",
             "Completely unrelated orbital claim."],
            ["Annual plans can be cancelled within thirty days of purchase."],
        )
        passed = [v.passed for v in verdicts if v.metric == "context_recall"]
        assert passed == [True, False]

    def test_full_case_score(self):
        case = _case()
        verdicts = LexicalRagasScorer().score(case)
        from backend.app.application.evals.ragas_suite import METRICS

        for metric in METRICS:
            assert any(v.metric == metric for v in verdicts)


class TestRunnerAndAggregate:
    def test_evaluate_case_metrics(self):
        result = asyncio.run(RagasSuiteRunner().evaluate_case(_case()))
        assert result.degraded is False
        for metric, value in result.metrics.items():
            assert 0.0 <= value <= 1.0

    def test_runner_degrades_on_scorer_failure(self):
        class _Boom:
            def score(self, case):
                raise RuntimeError("boom")

        result = asyncio.run(RagasSuiteRunner(scorer=_Boom()).evaluate_case(_case()))
        assert result.degraded is True
        assert all(v == 0.0 for v in result.metrics.values())

    def test_aggregate_means(self):
        results = asyncio.run(
            RagasSuiteRunner().evaluate_dataset([_case(), _case(name="c2")])
        )
        agg = aggregate_results(results)
        assert agg["case_count"] == 2
        for metric, value in agg["metrics"].items():
            assert 0.0 <= value <= 1.0


class TestLLMJudgeFailClosed:
    def test_judge_parses_yes_no(self):
        case = _case()
        judge = LLMRagasJudge(_FakeAdapter(["yes"] * 20))
        verdicts = asyncio.run(judge.score(case))
        assert len(verdicts) >= 4
        assert all(v.passed for v in verdicts)

    def test_judge_fails_closed_on_garbage(self):
        judge = LLMRagasJudge(_FakeAdapter(["maybe later"] * 20))
        verdicts = asyncio.run(judge.score(_case()))
        assert all(not v.passed for v in verdicts)

    def test_judge_fails_closed_on_provider_error(self):
        judge = LLMRagasJudge(_FakeAdapter([]))
        verdicts = asyncio.run(judge.score(_case()))
        assert all(not v.passed for v in verdicts)
        assert all("judge inconclusive" in (v.reason or "") for v in verdicts)
