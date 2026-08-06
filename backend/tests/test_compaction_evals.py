"""
P2-10 compaction quality eval tests (Arch 8.2):

- lexical exact-fact scorer (normalization, empty-summary, pass/fail)
- LLM judge scorer (yes/no parsing, fail-closed on errors)
- dataset loader + validation (malformed entries rejected)
- round-trip harness: perfect/lossy/degraded summaries, skipped probes,
  keep-tokens split
- tuning harness (prompt x keep-tokens matrix)
- probe extraction (hallucination guard, schema tolerance)
- operator monitor: threshold flagging, degraded checkpoints, logging
- CompactionService integration: quality_check config flag (off by
  default; on -> compaction_quality_low warning, swap still commits)
- LLMSummaryGenerator prompt override
"""

import json
from pathlib import Path

import pytest

from backend.app.adapters.llm import LLMConfig, LLMProviderType, LLMResponse
from backend.app.application.compaction import (
    CompactionConfig,
    CompactionQualityEvaluator,
    CompactionQualityMonitor,
    CompactionService,
    EvalCase,
    FactProbe,
    LexicalFactScorer,
    LLMJudgeScorer,
    LLMSummaryGenerator,
    ProbeExtractionError,
    SummarySchema,
    aggregate_results,
    generate_probes_from_turns,
    load_dataset,
    run_tuning_harness,
)

TURNS = [
    {"role": "user", "redacted_content": "I want a refund of 120 dollars."},
    {"role": "agent", "redacted_content": "The refund of 120 dollars is issued "
     "within 5 to 7 business days. Ticket TKT-8823."},
    {"role": "user", "redacted_content": "When does it arrive?"},
    {"role": "agent", "redacted_content": "It arrives by Friday."},
]

PROBES = [
    FactProbe("f1", "What amount?", "120 dollars"),
    FactProbe("f2", "How fast?", "5 to 7 business days"),
    FactProbe("f3", "Ticket?", "TKT-8823"),
    FactProbe("f4", "Delivery day?", "Friday"),
]

GOOD_PAYLOAD = {
    "objective": "Resolve refund",
    "key_facts": ["120 dollars", "5 to 7 business days", "TKT-8823", "Friday"],
    "decisions": ["refund issued"],
    "pending_work": [],
    "next_moves": [],
}

LOSSY_PAYLOAD = {
    "objective": "Resolve refund",
    "key_facts": ["120 dollars", "5 to 7 business days"],
    "decisions": ["refund issued"],
    "pending_work": [],
    "next_moves": [],
}


class _ScriptedAdapter:
    """Chat adapter with a response queue for summarizer/judge/probes."""

    provider_type = LLMProviderType.OPENAI

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.prompts = []
        self.config = LLMConfig(model="gpt-4o")

    async def chat(self, messages):
        self.calls += 1
        self.prompts.append(messages[0].content if messages else "")
        idx = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[idx]


def _payload_response(payload):
    return LLMResponse(content=json.dumps(payload), model="gpt-4")


def _generator(adapter):
    return LLMSummaryGenerator(adapter=adapter)


def _case(**overrides):
    base = {
        "name": "c1",
        "turns": list(TURNS),
        "probes": list(PROBES),
        "keep_tokens": None,
    }
    base.update(overrides)
    return EvalCase(**base)


# ---- lexical scorer -------------------------------------------------------


@pytest.mark.asyncio
async def test_lexical_scorer_pass_and_fail():
    verdicts = await LexicalFactScorer().score(SummarySchema.render(GOOD_PAYLOAD), PROBES)
    assert [v.passed for v in verdicts] == [True, True, True, True]


@pytest.mark.asyncio
async def test_lexical_scorer_lossy_summary_fails_missing_facts():
    verdicts = await LexicalFactScorer().score(SummarySchema.render(LOSSY_PAYLOAD), PROBES)
    passed = [v.probe_id for v in verdicts if v.passed]
    failed = [v.probe_id for v in verdicts if not v.passed]
    assert passed == ["f1", "f2"]
    assert failed == ["f3", "f4"]
    assert all(v.reason for v in verdicts if not v.passed)


@pytest.mark.asyncio
async def test_lexical_scorer_normalization():
    scorer = LexicalFactScorer()
    summary = "The refund of 120  Dollars  is issued. (Ticket: tkt-8823)"
    verdicts = await scorer.score(summary, [FactProbe("x", "q", "120 dollars")])
    assert verdicts[0].passed
    verdicts = await scorer.score(summary, [FactProbe("x", "q", "TKT-8823")])
    assert verdicts[0].passed
    verdicts = await scorer.score(summary, [FactProbe("x", "q", " Friday ")])
    assert not verdicts[0].passed


@pytest.mark.asyncio
async def test_lexical_scorer_empty_summary_fails_all():
    verdicts = await LexicalFactScorer().score("   ", PROBES)
    assert all(not v.passed for v in verdicts)
    assert all(v.reason == "summary empty" for v in verdicts)


# ---- LLM judge scorer ------------------------------------------------------


@pytest.mark.asyncio
async def test_judge_scorer_parses_yes_no():
    adapter = _ScriptedAdapter(
        [LLMResponse(content="yes", model="gpt-4o"),
         LLMResponse(content="No.", model="gpt-4o")]
    )
    verdicts = await LLMJudgeScorer(adapter).score("summary text", PROBES[:2])
    assert verdicts[0].passed
    assert not verdicts[1].passed


@pytest.mark.asyncio
async def test_judge_scorer_fails_closed_on_error_and_garbage():
    async def broken(messages):
        raise RuntimeError("provider down")

    adapter = _ScriptedAdapter([])
    adapter.chat = broken
    verdicts = await LLMJudgeScorer(adapter).score("text", PROBES[:1])
    assert not verdicts[0].passed
    assert "fail closed" in verdicts[0].reason

    adapter = _ScriptedAdapter([LLMResponse(content="maybe?", model="gpt-4o")])
    verdicts = await LLMJudgeScorer(adapter).score("text", PROBES[:1])
    assert not verdicts[0].passed
    assert "inconclusive" in verdicts[0].reason


@pytest.mark.asyncio
async def test_judge_scorer_empty_summary():
    verdicts = await LLMJudgeScorer(_ScriptedAdapter([])).score("", PROBES[:1])
    assert not verdicts[0].passed


def test_parse_verdict():
    assert LLMJudgeScorer._parse_verdict("Yes, it does.") is True
    assert LLMJudgeScorer._parse_verdict("no") is False
    assert LLMJudgeScorer._parse_verdict("unclear") is None
    assert LLMJudgeScorer._parse_verdict("") is None


# ---- dataset loading --------------------------------------------------------


def test_load_dataset(tmp_path):
    path = tmp_path / "d.jsonl"
    path.write_text(
        "\n".join(json.dumps({
            "name": "refund",
            "turns": TURNS,
            "probes": [{"id": "f1", "question": "q", "answer": "120 dollars"}],
        }) for _ in range(2)),
        encoding="utf-8",
    )
    cases = load_dataset(str(path))
    assert len(cases) == 2
    assert cases[0].name == "refund"
    assert cases[0].probes[0].answer == "120 dollars"


def test_load_dataset_rejects_invalid(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text(
        '{"name": "x", "turns": "nope"}\n'
        '{"name": "y", "turns": [{"redacted_content": "a"}], "probes": []}\n'
        "{not json}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc:
        load_dataset(str(path))
    message = str(exc.value)
    assert "'turns' must be a non-empty array" in message
    assert "'probes' must be a non-empty array" in message
    assert "line 3: invalid JSON" in message


def test_load_dataset_rejects_duplicate_probe_ids(tmp_path):
    path = tmp_path / "dup.jsonl"
    path.write_text(json.dumps({
        "name": "x",
        "turns": TURNS,
        "probes": [
            {"id": "f1", "question": "q", "answer": "120 dollars"},
            {"id": "f1", "question": "q2", "answer": "Friday"},
        ],
    }), encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_dataset(str(path))
    assert "duplicate probe id 'f1'" in str(exc.value)


def test_baseline_dataset_loads_and_is_grounded():
    path = (
        Path(__file__).resolve().parents[2]
        / "evals" / "datasets" / "compaction" / "baseline.jsonl"
    )
    cases = load_dataset(str(path))
    assert len(cases) == 5
    for case in cases:
        corpus = "\n".join(t["redacted_content"] for t in case.turns)
        for probe in case.probes:
            assert LexicalFactScorer._contains(corpus, probe.answer), (
                f"{case.name}: probe {probe.id} answer '{probe.answer}' "
                "not verbatim in turns"
            )


# ---- round-trip harness ------------------------------------------------------


@pytest.mark.asyncio
async def test_round_trip_perfect_summary():
    adapter = _ScriptedAdapter([_payload_response(GOOD_PAYLOAD)])
    evaluator = CompactionQualityEvaluator(_generator(adapter))
    result = await evaluator.evaluate_case(_case())
    assert result.retention_ratio == 1.0
    assert result.passed == ["f1", "f2", "f3", "f4"]
    assert result.failed == []
    assert not result.degraded
    assert result.summary


@pytest.mark.asyncio
async def test_round_trip_lossy_summary():
    adapter = _ScriptedAdapter([_payload_response(LOSSY_PAYLOAD)])
    evaluator = CompactionQualityEvaluator(_generator(adapter))
    result = await evaluator.evaluate_case(_case())
    assert result.retention_ratio == 0.5
    assert result.passed == ["f1", "f2"]
    assert [f["id"] for f in result.failed] == ["f3", "f4"]
    assert [f["reason"] for f in result.failed if f["reason"]] == ["answer absent from summary"] * 2


@pytest.mark.asyncio
async def test_round_trip_degraded_summary():
    adapter = _ScriptedAdapter([LLMResponse(content="", model="gpt-4")])
    evaluator = CompactionQualityEvaluator(_generator(adapter))
    result = await evaluator.evaluate_case(_case())
    assert result.degraded
    assert result.retention_ratio == 0.0
    assert result.summary is None


@pytest.mark.asyncio
async def test_round_trip_invalid_payload_degraded():
    adapter = _ScriptedAdapter(
        [_payload_response({"objective": "x", "key_facts": "not a list"})]
    )
    evaluator = CompactionQualityEvaluator(_generator(adapter))
    result = await evaluator.evaluate_case(_case())
    assert result.degraded
    assert result.retention_ratio == 0.0


@pytest.mark.asyncio
async def test_probe_not_answerable_before_is_skipped():
    case = _case(probes=list(PROBES) + [FactProbe("f9", "?", "never mentioned")])
    adapter = _ScriptedAdapter([_payload_response(GOOD_PAYLOAD)])
    evaluator = CompactionQualityEvaluator(_generator(adapter))
    result = await evaluator.evaluate_case(case)
    assert result.skipped == ["f9"]
    assert result.retention_ratio == 1.0


@pytest.mark.asyncio
async def test_keep_tokens_split_excludes_tail_facts_from_scoring():
    # keep_tokens small: turn 4 falls into the kept-live tail, so f4 is
    # skipped (the summary never needs it), not failed
    case = _case(keep_tokens=10)
    adapter = _ScriptedAdapter([_payload_response(GOOD_PAYLOAD)])
    evaluator = CompactionQualityEvaluator(_generator(adapter))
    result = await evaluator.evaluate_case(case)
    assert result.skipped == ["f4"]
    assert result.passed == ["f1", "f2", "f3"]
    assert result.retention_ratio == 1.0


@pytest.mark.asyncio
async def test_generator_error_degrades_case():
    async def broken(messages):
        raise RuntimeError("boom")

    adapter = _ScriptedAdapter([])
    adapter.chat = broken
    evaluator = CompactionQualityEvaluator(_generator(adapter))
    result = await evaluator.evaluate_case(_case())
    assert result.degraded
    assert result.retention_ratio == 0.0


def test_aggregate_stats():
    from backend.app.application.compaction import EvalCaseResult

    results = [
        EvalCaseResult("a", 1.0, ["f1"], [], [], False, "s"),
        EvalCaseResult("b", 0.5, ["f1"], [{"id": "f2"}], [], False, "s"),
        EvalCaseResult("c", 0.0, [], [], [], True, None),
    ]
    stats = aggregate_results(results)
    assert stats["facts_total"] == 3
    assert stats["facts_passed"] == 2
    assert stats["avg_retention"] == round(2 / 3, 3)
    assert stats["min_retention"] == 0.0
    assert stats["degraded_cases"] == 1


# ---- tuning harness -----------------------------------------------------------


@pytest.mark.asyncio
async def test_tuning_harness_compares_prompts_and_keep():
    adapter = _ScriptedAdapter(
        [
            _payload_response(GOOD_PAYLOAD),
            _payload_response(LOSSY_PAYLOAD),
            _payload_response(GOOD_PAYLOAD),
            _payload_response(LOSSY_PAYLOAD),
        ]
    )
    rows = await run_tuning_harness(
        [_case()],
        adapter,
        prompts=["{conversation}", "Terse version: {conversation}"],
        keep_tokens_options=[None, 10],
    )
    assert len(rows) == 4
    # prompt-major ordering: (p0,None) (p0,10) (p1,None) (p1,10)
    assert rows[0].prompt_label == "prompt-base"
    assert rows[0].keep_tokens is None
    assert rows[0].avg_retention == 1.0
    assert rows[1].keep_tokens == 10
    assert rows[2].prompt_label == "prompt-1"
    assert rows[2].keep_tokens is None
    assert rows[2].avg_retention == 1.0


@pytest.mark.asyncio
async def test_generator_prompt_override_is_used():
    custom = "CUSTOM SUMMARIZER PROMPT\n{conversation}"
    adapter = _ScriptedAdapter([_payload_response(GOOD_PAYLOAD)])
    generator = LLMSummaryGenerator(adapter=adapter, prompt=custom)
    await generator.summarize(TURNS)
    assert adapter.calls == 1
    assert "CUSTOM SUMMARIZER PROMPT" in adapter.prompts[0]
    assert "120 dollars" in adapter.prompts[0]

    adapter2 = _ScriptedAdapter([_payload_response(GOOD_PAYLOAD)])
    generator2 = LLMSummaryGenerator(adapter=adapter2)
    await generator2.summarize(TURNS)
    assert "You are a conversation summarizer" in adapter2.prompts[0]


# ---- probe extraction -----------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_extraction_grounded_only():
    payload = [
        {"id": "f1", "question": "amount?", "answer": "120 dollars"},
        {"id": "f2", "question": "invented?", "answer": "completely made up claim"},
    ]
    adapter = _ScriptedAdapter([_payload_response(payload)])
    probes = await generate_probes_from_turns(TURNS, adapter)
    assert [p.id for p in probes] == ["f1"]


@pytest.mark.asyncio
async def test_probe_extraction_tolerates_loose_schema():
    payload = [
        {"id": "f1", "question": "amount?", "answer": "120 dollars"},
        {"id": "", "question": "no id", "answer": "Friday"},
        {"id": "f3", "question": "?", "answer": "TKT-8823"},
        "not an object",
        {"id": "f5", "question": "", "answer": "x"},
        {"id": "f6", "question": "?", "answer": "Friday"},
    ]
    adapter = _ScriptedAdapter([_payload_response(payload)])
    probes = await generate_probes_from_turns(TURNS, adapter)
    # empty id gets auto-assigned; blank questions dropped; non-objects dropped
    assert [p.id for p in probes] == ["f1", "f2", "f3", "f6"]
    assert len(probes) == 4


@pytest.mark.asyncio
async def test_probe_extraction_fails_closed():
    adapter = _ScriptedAdapter([LLMResponse(content="not json", model="gpt-4")])
    with pytest.raises(ProbeExtractionError):
        await generate_probes_from_turns(TURNS, adapter)

    async def broken(messages):
        raise RuntimeError("provider down")

    adapter.chat = broken
    with pytest.raises(ProbeExtractionError):
        await generate_probes_from_turns(TURNS, adapter)


@pytest.mark.asyncio
async def test_probe_extraction_respects_max():
    payload = [
        {"id": f"f{i}", "question": "?", "answer": "120 dollars"}
        for i in range(10)
    ]
    adapter = _ScriptedAdapter([_payload_response(payload)])
    probes = await generate_probes_from_turns(TURNS, adapter, max_probes=3)
    assert len(probes) == 3


# ---- operator monitor -----------------------------------------------------------


class _FakeThreads:
    def __init__(self, row):
        self.row = row

    async def get_thread(self, tenant_id, thread_id):
        return self.row


@pytest.mark.asyncio
async def test_monitor_flags_low_quality(capsys):
    summary = SummarySchema.render(LOSSY_PAYLOAD)
    threads = _FakeThreads({"summary_block": {"content": summary}})
    monitor = CompactionQualityMonitor(
        threads=threads, generator=None, threshold=0.6
    )
    result = await monitor.evaluate_thread("t1", "th1", PROBES)
    assert result.retention_ratio == 0.5
    assert result.low_quality is True
    assert result.passed == ["f1", "f2"]
    assert [f["id"] for f in result.failed] == ["f3", "f4"]
    assert not result.degraded
    assert "compaction_quality_low" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_monitor_ok_above_threshold(capsys):
    threads = _FakeThreads(
        {"summary_block": {"content": SummarySchema.render(GOOD_PAYLOAD)}}
    )
    monitor = CompactionQualityMonitor(threads=threads, generator=None)
    result = await monitor.evaluate_thread("t1", "th1", PROBES)
    assert not result.low_quality
    assert result.retention_ratio == 1.0
    assert "compaction_quality_ok" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_monitor_degraded_without_summary(capsys):
    threads = _FakeThreads({})
    monitor = CompactionQualityMonitor(threads=threads, generator=None)
    result = await monitor.evaluate_thread("t1", "th1", PROBES)
    assert result.degraded is True
    assert result.low_quality is True
    assert result.retention_ratio == 0.0
    assert "compaction_quality_low" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_monitor_scorer_failure_fails_closed():
    async def broken(summary, probes):
        raise RuntimeError("judge down")

    threads = _FakeThreads({"summary_block": {"content": "some summary"}})
    monitor = CompactionQualityMonitor(
        threads=threads, generator=None, scorer=broken
    )
    result = await monitor.evaluate_thread("t1", "th1", PROBES)
    assert result.low_quality is True
    assert result.retention_ratio == 0.0
    assert all("fail closed" in f["reason"] for f in result.failed)


# ---- CompactionService integration ------------------------------------------------


def _service_threads(summary_block=None):
    calls = {"set_summary": 0}

    async def set_summary(tenant_id, thread_id, *, summary_block=None,
                          summary_position=None, boundary_message_id=None,
                          request_id=None, degraded=False):
        calls["set_summary"] += 1
        return {"summary_version": 1, "summary_position": 1, "event": {}}

    async def get_thread(tenant_id, thread_id):
        return {"summary_block": summary_block}

    async def list_messages(tenant_id, thread_id, *, after_seq=None, limit=200):
        return {"messages": [], "has_more": False}

    return SimpleNamespaceStub(set_summary=set_summary, get_thread=get_thread,
                               list_messages=list_messages), calls


class SimpleNamespaceStub:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _compaction_service(adapter, config=None):
    return CompactionService(
        threads=_service_threads()[0],
        generator=LLMSummaryGenerator(adapter=adapter),
        config=config or CompactionConfig(quality_check=False),
    )


@pytest.mark.asyncio
async def test_quality_check_off_by_default_no_probe_calls():
    adapter = _ScriptedAdapter([_payload_response(LOSSY_PAYLOAD)])
    service = _compaction_service(adapter)
    result = await service.compact("t1", "th1")
    assert result["compacted"] is False  # empty thread snapshot -> noop
    assert adapter.calls == 0


def _long_turns():
    """Turns padded past keep_tokens so a real head exists for compaction."""
    return [
        {
            "id": f"m{i}",
            "seq": i + 1,
            "role": "agent",
            "content": "",
            "redacted_content": turn["redacted_content"] + " " + "x" * 12000,
        }
        for i, turn in enumerate(TURNS)
    ]


@pytest.mark.asyncio
async def test_quality_check_on_reports_low_and_still_commits(capsys):
    # snapshot has turns; quality_check enabled; summary drops the probed
    # fact (the refund amount) -> low-ratio warning, commit still happens
    threads, calls = _service_threads()
    lossy_without_amount = {
        "objective": "Resolve refund",
        "key_facts": ["5 to 7 business days"],
        "decisions": ["refund issued"],
        "pending_work": [],
        "next_moves": [],
    }

    async def list_messages(tenant_id, thread_id, *, after_seq=None, limit=200):
        return {"messages": _long_turns(), "has_more": False}

    threads.list_messages = list_messages
    adapter = _ScriptedAdapter(
        [
            _payload_response(lossy_without_amount),  # summarizer (first)
            _payload_response([{"id": "f1", "question": "amount?",
                                "answer": "120 dollars"}]),  # probes
        ]
    )
    service = CompactionService(
        threads=threads,
        generator=LLMSummaryGenerator(adapter=adapter),
        config=CompactionConfig(quality_check=True, quality_threshold=0.6),
    )
    result = await service.compact("t1", "th1")
    assert result["compacted"] is True
    assert calls["set_summary"] == 1
    # summarizer call (1) + probes call (1)
    assert adapter.calls == 2
    assert "compaction_quality_low" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_quality_check_failure_never_blocks_commit(capsys):
    threads, calls = _service_threads()

    async def list_messages(tenant_id, thread_id, *, after_seq=None, limit=200):
        return {"messages": _long_turns(), "has_more": False}

    threads.list_messages = list_messages
    adapter = _ScriptedAdapter(
        [
            _payload_response(GOOD_PAYLOAD),  # summarizer (first)
            _payload_response([{"id": "f1", "question": "amount?",
                                "answer": "120 dollars"}]),  # probes
        ]
    )

    service = CompactionService(
        threads=threads,
        generator=LLMSummaryGenerator(adapter=adapter),
        config=CompactionConfig(quality_check=True),
    )
    original = service.generator.adapter.chat
    calls_made = {"n": 0}

    async def second_broken(messages):
        calls_made["n"] += 1
        if calls_made["n"] == 2:
            raise RuntimeError("probes provider down")
        return await original(messages)

    service.generator.adapter.chat = second_broken
    result = await service.compact("t1", "th1")
    assert result["compacted"] is True
    assert calls["set_summary"] == 1
    assert "compaction_quality_check_failed" in capsys.readouterr().out
