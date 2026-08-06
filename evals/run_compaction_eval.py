"""P2-10 compaction quality harness (round-trip fact retention).

Summarizes each dataset case with the configured provider, scores fact
retention (lexical exact-fact by default; --judge uses the LLM as a
paraphrase-tolerant judge), prints a per-case report, and exits non-zero
when the aggregate retention is below the threshold (CI-able).

Usage:
  python evals/run_compaction_eval.py \
      [--dataset evals/datasets/compaction/baseline.jsonl] \
      [--provider openai] [--model gpt-4o-mini] [--api-key KEY] \
      [--prompt-file prompts/summary.md] [--keep-tokens 8000] \
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
from backend.app.application.compaction import (  # noqa: E402
    CompactionQualityEvaluator,
    LLMJudgeScorer,
    LLMSummaryGenerator,
    LexicalFactScorer,
    aggregate_results,
    load_dataset,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        default=str(
            Path(__file__).resolve().parent / "datasets" / "compaction" / "baseline.jsonl"
        ),
    )
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--prompt-file", default=None)
    parser.add_argument("--keep-tokens", type=int, default=None)
    parser.add_argument("--threshold", type=float, default=0.6)
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    cases = load_dataset(args.dataset)
    api_key = args.api_key
    if not api_key:
        from backend.app.settings.env import settings

        api_key = getattr(settings, f"{args.provider.upper()}_API_KEY", None) or ""
    adapter = create_llm_adapter(
        LLMProviderType(args.provider),
        api_key,
        LLMConfig(model=args.model, temperature=0.0),
    )
    prompt = None
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    generator = LLMSummaryGenerator(adapter=adapter, prompt=prompt)
    scorer = LLMJudgeScorer(adapter) if args.judge else LexicalFactScorer()
    evaluator = CompactionQualityEvaluator(generator, scorer=scorer)
    if args.keep_tokens is not None:
        for case in cases:  # CLI tuning knob overrides the dataset value
            case.keep_tokens = args.keep_tokens
    results = await evaluator.evaluate_dataset(cases)
    aggregate = aggregate_results(results)

    print(f"=== compaction quality: {Path(args.dataset).name} ===")
    print(
        f"scorer: {'llm-judge' if args.judge else 'lexical'} | "
        f"model: {args.provider}/{args.model}"
    )
    for result in results:
        state = "DEGRADED" if result.degraded else "ok"
        print(
            f"  {result.name:<22} retention {result.retention_ratio:.2f} "
            f"[{len(result.passed)}/{len(result.passed) + len(result.failed)}] "
            f"skip {len(result.skipped)} {state}"
        )
        if args.verbose:
            for failed in result.failed:
                print(f"    FAIL {failed['id']}: {failed['question']} ({failed['reason']})")
            for probe_id in result.skipped:
                print(f"    SKIP {probe_id}: not answerable pre-compaction")
    print(
        f"aggregate retention: {aggregate['avg_retention']:.2f} "
        f"({aggregate['facts_passed']}/{aggregate['facts_total']} facts) | "
        f"min {aggregate['min_retention']:.2f} | "
        f"degraded {aggregate['degraded_cases']}/{aggregate['case_count']}"
    )
    return 0 if aggregate["avg_retention"] >= args.threshold else 1


def main() -> None:
    args = _parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
