"""
P2-1 SessionContextLoader tests:

- block order/placement (Arch 8.1: system -> summary -> memory -> tail ->
  knowledge -> current)
- budget overshoot trimming (oldest turns first, lowest-score docs first;
  mandatory blocks fail closed with ContextBudgetExceeded)
- redaction guarantee (P0-5): no fallback path from raw to model context
- token estimator math (4 chars/token default, per-model overrides)
- tool-result clearing hook (P2-7 placeholder semantics)
- system prefix stability across turns (P2-6 prompt-cache precondition)
"""

import pytest

from backend.app.context import (
    ContextBudgetExceeded,
    ContextTurn,
    RedactionViolation,
    SUMMARY_BLOCK_HEADER,
    TOOL_RESULT_PLACEHOLDER,
    SessionContextLoader,
    TokenEstimator,
)
from backend.app.gateway.catalog import ModelCatalog, ModelSpec


def _loader(**kwargs):
    return SessionContextLoader(output_reserve_tokens=kwargs.pop("reserve", 0), **kwargs)


def _turn(role, content, seq, message_id=None, **kwargs):
    return ContextTurn(role=role, content=content, seq=seq, message_id=message_id, **kwargs)


# ---- estimator -----------------------------------------------------------


def test_estimate_default_four_chars_per_token():
    estimator = TokenEstimator()
    assert estimator.estimate("", "openai", "gpt-4o") == 0
    assert estimator.estimate("abcd", "openai", "gpt-4o") == 1
    assert estimator.estimate("abcdefghij", "openai", "gpt-4o") == 3


def test_estimate_per_model_override():
    estimator = TokenEstimator(
        chars_per_token={"openai": 2.0, "openai:gpt-4o": 1.0}
    )
    assert estimator.estimate("abcd", "openai", "gpt-4o") == 4
    assert estimator.estimate("abcd", "openai", "other") == 2
    assert estimator.estimate("abcd", "anthropic", "x") == 1  # default 4.0
    estimator.register_override("anthropic:claude-3-5-sonnet", 2.0)
    assert estimator.estimate("abcd", "anthropic", "claude-3-5-sonnet") == 2


def test_estimate_messages_counts_parts_not_raw_text():
    import json
    import math

    estimator = TokenEstimator()
    tool_payload = {"tool": "x" * 8}
    messages = [
        {"role": "user", "content": "abcd"},          # 1 token
        {"role": "assistant", "content": "efgh"},     # 1 token
        {"role": "assistant", "content": tool_payload},  # serialized size
    ]
    serialized = json.dumps(tool_payload, ensure_ascii=False, sort_keys=True)
    assert estimator.estimate_messages(messages) == 2 + math.ceil(len(serialized) / 4)
    assert estimator.estimate_messages([]) == 0


# ---- block order and placement ------------------------------------------


def test_assembled_block_order():
    loader = _loader()
    ctx = loader.assemble(
        provider="openai",
        model="gpt-4o",
        system_prompt="SYSTEM PREFIX",
        current_message="how are you",
        history_turns=[
            _turn("user", "hello", 1, message_id="m1"),
            _turn("assistant", "hi there", 2, message_id="m2"),
        ],
        summary="compact summary",
        memory_facts=["prefers email"],
        retrieved_docs=[{"content": "DOC", "score": 0.9}],
    )
    roles = [m["role"] for m in ctx.messages]
    assert roles == ["system", "system", "system", "user", "assistant", "system", "user"]
    assert ctx.messages[0]["content"] == "SYSTEM PREFIX"
    assert ctx.messages[1]["content"].startswith(SUMMARY_BLOCK_HEADER)
    assert "compact summary" in ctx.messages[1]["content"]
    assert "prefers email" in ctx.messages[2]["content"]
    assert ctx.messages[3]["content"] == "hello"
    assert ctx.messages[4]["content"] == "hi there"
    assert ctx.messages[5]["content"].startswith("[Source 1] (Score: 0.90)")
    assert ctx.messages[6] == {"role": "user", "content": "how are you"}


def test_omitted_when_absent():
    loader = _loader()
    ctx = loader.assemble(
        provider="openai",
        model="gpt-4o",
        system_prompt="S",
        current_message="hi",
    )
    assert [m["role"] for m in ctx.messages] == ["system", "user"]
    assert ctx.omitted_blocks == []


def test_tail_renders_chronologically():
    loader = _loader()
    ctx = loader.assemble(
        provider="openai",
        model="gpt-4o",
        system_prompt="S",
        current_message="m",
        history_turns=[
            _turn("user", "u1", 1),
            _turn("assistant", "a2", 2),
            _turn("user", "u3", 3),
        ],
    )
    tail = [m for m in ctx.messages[:-1] if m["role"] in ("user", "assistant")]
    assert tail == [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
    ]


def test_tail_without_seqs_keeps_input_order():
    loader = _loader()
    ctx = loader.assemble(
        provider="openai",
        model="gpt-4o",
        system_prompt="S",
        current_message="m",
        history_turns=[
            ContextTurn(role="user", content="u1"),
            ContextTurn(role="assistant", content="a1"),
        ],
    )
    tail = [m for m in ctx.messages[:-1] if m["role"] in ("user", "assistant")]
    assert tail == [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]


# ---- budget math and trimming -------------------------------------------


def test_budget_trims_oldest_turns_first():
    loader = _loader()
    turns = [_turn("user", "x" * 60, seq, message_id=f"m{seq}") for seq in range(1, 5)]
    ctx = loader.assemble(
        provider="openai",
        model="gpt-4o",
        system_prompt="S",  # 1 token
        current_message="hi",  # 1 token
        history_turns=turns,
        context_window_override=41,  # budget 41: 1 + tail + 1
    )
    # Each turn is 15 tokens; budget left for tail is 39 -> two newest fit.
    tail = [m for m in ctx.messages[:-1] if m["role"] in ("user", "assistant")]
    assert [m["content"] for m in tail] == ["x" * 60, "x" * 60]
    assert sorted(ctx.omitted_turns) == ["m1", "m2"]


def test_knowledge_sorted_and_low_score_trimmed():
    loader = _loader()
    ctx = loader.assemble(
        provider="openai",
        model="gpt-4o",
        system_prompt="S",  # 1 token; budget 159 after it
        current_message="hi",
        history_turns=[],
        retrieved_docs=[
            {"content": "y" * 400, "score": 0.5},  # 100 tokens
            {"content": "y" * 400, "score": 0.9},  # 100 tokens
        ],
        context_window_override=160,
    )
    knowledge = [m for m in ctx.messages if "[Source" in m["content"]]
    assert len(knowledge) == 1
    assert knowledge[0]["content"].startswith("[Source 1] (Score: 0.90)")
    assert ctx.omitted_docs == [1]


def test_current_message_fail_closed_when_over_budget():
    loader = _loader()
    with pytest.raises(ContextBudgetExceeded, match="compaction required"):
        loader.assemble(
            provider="openai",
            model="gpt-4o",
            system_prompt="S",
            current_message="z" * 200,
            context_window_override=40,
        )


def test_system_prefix_fail_closed_when_over_budget():
    loader = _loader()
    with pytest.raises(ContextBudgetExceeded, match="System prompt"):
        loader.assemble(
            provider="openai",
            model="gpt-4o",
            system_prompt="z" * 100,
            current_message="hi",
            context_window_override=10,
        )


def test_reserve_exceeding_window_fail_closed():
    loader = SessionContextLoader(output_reserve_tokens=100)
    with pytest.raises(ContextBudgetExceeded, match="output reserve"):
        loader.assemble(
            provider="openai",
            model="gpt-4o",
            system_prompt="S",
            current_message="hi",
            context_window_override=10,
        )


def test_summary_omitted_when_budget_tight():
    loader = _loader()
    ctx = loader.assemble(
        provider="openai",
        model="gpt-4o",
        system_prompt="S",  # 1 token; 49 left
        current_message="hi",
        summary="w" * 400,  # 100 tokens: does not fit
        context_window_override=50,
    )
    assert ctx.omitted_blocks == ["summary"]
    assert not any(SUMMARY_BLOCK_HEADER in m["content"] for m in ctx.messages)


def test_token_accounting_sums_to_total():
    loader = _loader()
    ctx = loader.assemble(
        provider="openai",
        model="gpt-4o",
        system_prompt="S",
        current_message="hello world",
        history_turns=[_turn("user", "some prior turn", 1)],
        retrieved_docs=[{"content": "D", "score": 0.7}],
    )
    assert ctx.total_tokens == sum(ctx.token_counts.values())
    assert ctx.total_tokens > 0


def test_default_output_reserve_uses_settings():
    loader = SessionContextLoader()
    ctx = loader.assemble(
        provider="unknown",
        model="model",
        system_prompt="S",
        current_message="hi",
        context_window_override=5000,
    )
    # Default window is 128_000 but the override wins; reserve comes from
    # settings (CONTEXT_OUTPUT_RESERVE_TOKENS = 4096).
    assert ctx.budget_tokens == 5000 - 4096


def test_context_window_comes_from_gateway_catalog():
    catalog = ModelCatalog(
        specs=[ModelSpec("openai", "gpt-4o", context_window=10_000)]
    )
    loader = SessionContextLoader(model_catalog=catalog, output_reserve_tokens=0)
    ctx = loader.assemble(
        provider="openai",
        model="gpt-4o",
        system_prompt="S",
        current_message="hi",
    )
    assert ctx.context_window == 10_000
    assert ctx.budget_tokens == 10_000


# ---- redaction guarantee (P0-5) ------------------------------------------


def test_redaction_violation_fail_closed():
    loader = _loader()
    leaky = _turn("user", "", 1, message_id="m1", raw_content="SECRET RAW")
    with pytest.raises(RedactionViolation, match="m1"):
        loader.assemble(
            provider="openai",
            model="gpt-4o",
            system_prompt="S",
            current_message="hi",
            history_turns=[leaky],
        )


def test_empty_turns_and_unknown_roles_skipped():
    loader = _loader()
    ctx = loader.assemble(
        provider="openai",
        model="gpt-4o",
        system_prompt="S",
        current_message="hi",
        history_turns=[
            _turn("user", "", 1),
            _turn("system", "not a turn", 2),
            _turn("user", "kept", 3),
        ],
    )
    assert all(m["content"] != "not a turn" for m in ctx.messages)
    assert ctx.messages[-2] == {"role": "user", "content": "kept"}


def test_redacted_classmethod_marks_contract():
    turn = ContextTurn.redacted(role="user", content="clean", seq=1)
    assert turn.raw_content is None
    assert turn.content == "clean"


# ---- tool-result clearing hook (P2-7 placeholder) ------------------------


def test_tool_payload_cleared_to_placeholder():
    loader = _loader()
    tooled = _turn("assistant", "tool payload", 1, has_tool_payload=True)
    ctx = loader.assemble(
        provider="openai",
        model="gpt-4o",
        system_prompt="S",
        current_message="hi",
        history_turns=[tooled],
        clear_tool_payloads=True,
    )
    tail = [m for m in ctx.messages if m["role"] == "assistant"]
    assert tail[0]["content"] == TOOL_RESULT_PLACEHOLDER


def test_tool_payload_kept_when_not_clearing():
    loader = _loader()
    tooled = _turn("assistant", "tool payload", 1, has_tool_payload=True)
    ctx = loader.assemble(
        provider="openai",
        model="gpt-4o",
        system_prompt="S",
        current_message="hi",
        history_turns=[tooled],
    )
    tail = [m for m in ctx.messages if m["role"] == "assistant"]
    assert tail[0]["content"] == "tool payload"


# ---- prompt-cache stability precondition (P2-6) --------------------------


def test_system_prefix_stable_across_turns():
    loader = _loader()
    first = loader.assemble(
        provider="openai",
        model="gpt-4o",
        system_prompt="STABLE PREFIX",
        current_message="turn one",
        history_turns=[_turn("user", "older", 1)],
    )
    second = loader.assemble(
        provider="openai",
        model="gpt-4o",
        system_prompt="STABLE PREFIX",
        current_message="turn two",
        history_turns=[_turn("user", "older", 1), _turn("assistant", "newer", 2)],
    )
    # P2-6: the system message carries cache_control metadata on the stable
    # prefix; byte-identical across turns.
    assert first.messages[0] == {
        "role": "system",
        "content": "STABLE PREFIX",
        "metadata": {"cache_control": {"type": "ephemeral"}},
    }
    assert second.messages[0] == first.messages[0]
