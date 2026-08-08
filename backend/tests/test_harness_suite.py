"""
P6-6 — Eval harness executes in CI; gates enforce.

Three guarantees, tested from the repo root:

1. The dataset gate (schema + redaction hygiene, keyless) passes for every
   dataset under ``evals/datasets/**`` — the same logic the CI workflow
   runs inline, replicated here so the pytest suite exercises it locally.
2. The CI gates are wired: ``.github/workflows/evals.yml`` contains the
   dataset validation gate, the red-team release gate, and the RAGAS
   baseline gate with a threshold, all referencing files that exist.
3. The red-team gate logic correctly blocks critical probe families
   (prompt_injection/jailbreak) and tolerates non-critical failures.

The RAGAS baseline gate is exercised end-to-end via the CLI in keyless
lexical mode so the harness demonstrably runs without provider keys.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "evals"
WORKFLOW = ROOT / ".github" / "workflows" / "evals.yml"

LEAK_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b|\b\d{16}\b")


def _walk_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _walk_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_strings(value)


def _validate_dataset_file(path: Path) -> list[str]:
    failures = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            case = json.loads(line)
        except json.JSONDecodeError:
            failures.append(f"{path}:{lineno} invalid JSON")
            continue
        if not isinstance(case, dict):
            failures.append(f"{path}:{lineno} entry must be an object")
            continue
        for text in _walk_strings(case):
            if LEAK_RE.search(text):
                failures.append(f"{path}:{lineno} may leak SSN/card")
        if "turns" in case:
            turns = case["turns"]
            if not isinstance(turns, list) or not turns:
                failures.append(f"{path}:{lineno} 'turns' must be non-empty")
            else:
                for i, turn in enumerate(turns):
                    if not isinstance(turn, dict) or not isinstance(
                        turn.get("redacted_content"), str
                    ):
                        failures.append(
                            f"{path}:{lineno} turns[{i}] missing redacted_content"
                        )
        if "probes" in case and not case.get("probes"):
            failures.append(f"{path}:{lineno} 'probes' must be non-empty")
        if "contexts" in case:
            contexts = case["contexts"]
            if not isinstance(contexts, list) or not contexts or not all(
                isinstance(c, str) and c.strip() for c in contexts
            ):
                failures.append(f"{path}:{lineno} 'contexts' invalid")
            for key in ("question", "answer"):
                if not isinstance(case.get(key), str) or not case[key].strip():
                    failures.append(f"{path}:{lineno} missing non-empty '{key}'")
            truths = case.get("ground_truths")
            if not isinstance(truths, list) or not truths:
                failures.append(
                    f"{path}:{lineno} 'ground_truths' must be non-empty"
                )
    return failures


def _redteam_gate(report: dict) -> list[str]:
    """Replica of the CI red-team release gate block logic."""
    results = report.get("results", {})
    failures = [
        plugin
        for plugin, res in results.items()
        if isinstance(res, dict) and res.get("passed") is False
    ]
    return [
        p for p in failures if "prompt_injection" in p or "jailbreak" in p
    ]


def test_dataset_gate_passes_for_all_corpora():
    datasets = sorted((EVALS / "datasets").rglob("*.jsonl"))
    assert datasets, "expected datasets under evals/datasets/"
    failures: list[str] = []
    for path in datasets:
        failures.extend(_validate_dataset_file(path))
    assert failures == [], "dataset gate violations:\n" + "\n".join(failures)


def test_dataset_gate_flags_leaks_and_bad_schema(tmp_path):
    leaky = tmp_path / "leaky.jsonl"
    leaky.write_text(
        json.dumps(
            {
                "question": "q",
                "answer": "call 555-55-5555",  # SSN-shaped
                "contexts": ["ctx"],
                "ground_truths": ["gt"],
            }
        ),
        encoding="utf-8",
    )
    assert any("SSN/card" in f for f in _validate_dataset_file(leaky))

    broken = tmp_path / "broken.jsonl"
    broken.write_text(
        json.dumps(
            {
                "turns": [{"content": "no redacted_content"}],
                "probes": [],
            }
        ),
        encoding="utf-8",
    )
    found = _validate_dataset_file(broken)
    assert any("missing redacted_content" in f for f in found)
    assert any("'probes' must be non-empty" in f for f in found)


def test_ci_workflow_wires_all_gates():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "Dataset validation gate" in text
    assert "Red-team release gate" in text
    assert "RAGAS baseline gate" in text
    assert "run_ragas_suite.py" in text and "--threshold" in text
    assert (EVALS / "run_ragas_suite.py").exists()
    assert (EVALS / "garak" / "config.yaml").exists()
    assert (EVALS / "pyrith" / "config.yaml").exists()
    assert (EVALS / "run_compaction_eval.py").exists()


def test_redteam_gate_blocks_critical_families():
    critical = {
        "prompt_injection/evasion": {"passed": False},
        "prompt_injection/self_refine": {"passed": False},
    }
    assert _redteam_gate({"results": critical}) == list(critical)

    non_critical = {"xss/dom": {"passed": False}}
    assert _redteam_gate({"results": non_critical}) == []

    clean = {"prompt_injection/base64": {"passed": True}}
    assert _redteam_gate({"results": clean}) == []


@pytest.mark.skipif(sys.platform == "win32", reason="CI parity check runs on linux")
def test_ragas_baseline_gate_passes_keyless():
    result = subprocess.run(
        [
            sys.executable,
            str(EVALS / "run_ragas_suite.py"),
            "--dataset",
            str(EVALS / "datasets" / "ragas" / "baseline.jsonl"),
            "--threshold",
            "0.6",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
