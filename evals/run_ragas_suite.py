"""P6-6 RAGAS-style RAG quality harness (faithfulness, answer relevance,
context precision, context recall).

Scores each dataset case with the configured scorer — deterministic
lexical by default (no model calls, runs keyless in CI); ``--judge``
swaps in an LLM judge for paraphrase tolerance. Prints a per-case report
and exits non-zero when any metric's aggregate is below the threshold
(CI-able gate).

Usage:
  python evals/run_ragas_suite.py \
      [--dataset evals/datasets/ragas/baseline.jsonl] \
      [--provider openai] [--model gpt-4o-mini] [--api-key KEY] \
      [--threshold 0.6] [--judge] [--verbose]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from backend.app.adapters.llm import (  # noqa: E402
    LLMConfig,
    LLMProviderType,
    create_llm_adapter,
)
from backend.app.application.evals.ragas_suite import (  # noqa: E402
    METRICS,
    LLMRagasJudge,
    LexicalRagasScorer,
    RagasSuiteRunner,
    aggregate_results,
    load_dataset,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        default=str(
            Path(__file__).resolve().parent / "datasets" / "ragas" / "baseline.jsonl"
        ),
    )
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--threshold", type=float, default=0.6)
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    cases = load_dataset(args.dataset)
    if args.judge:
        api_key = args.api_key
        if not api_key:
            from backend.app.settings.env import settings

            api_key = getattr(settings, f"{args.provider.upper()}_API_KEY", None) or ""
        adapter = create_llm_adapter(
            LLMProviderType(args.provider),
            api_key,
            LLMConfig(model=args.model, temperature=0.0),
        )
        scorer = LLMRagasJudge(adapter)
        scorer_label = f"llm-judge ({args.provider}/{args.model})"
    else:
        scorer = LexicalRagasScorer()
        scorer_label = "lexical (keyless)"
    runner = RagasSuiteRunner(scorer=scorer)
    results = await runner.evaluate_dataset(cases)
    aggregate = aggregate_results(results)

    print(f"=== ragas suite: {Path(args.dataset).name} ===")
    print(f"scorer: {scorer_label}")
    for result in results:
        state = "DEGRADED" if result.degraded else "ok"
        scores = " ".join(
            f"{metric}={result.metrics[metric]:.2f}" for metric in METRICS
        )
        print(f"  {result.name:<22} {scores} {state}")
        if args.verbose:
            for verdict in result.verdicts:
                if not verdict.passed and verdict.reason:
                    print(
                        f"    FAIL {verdict.metric}: {verdict.reason}"
                    )
    metrics = aggregate["metrics"]
    print(
        f"aggregate: "
        + " ".join(f"{m}={metrics[m]:.2f}" for m in METRICS)
        + f" | degraded {aggregate['degraded_cases']}/{aggregate['case_count']}"
    )
    failed = [
        metric
        for metric in METRICS
        if metrics[metric] < args.threshold
    ]
    if failed:
        print(
            f"GATE FAILED: {', '.join(failed)} below threshold {args.threshold}"
        )
        return 1
    return 0


def main() -> None:
    args = _parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
