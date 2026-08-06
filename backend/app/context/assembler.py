"""
Session context assembler (Arch 8.1, P2-1).

``SessionContextLoader`` is the single component that constructs the LLM
prompt for a turn. Nothing else in the codebase builds provider messages.

Block order (Arch 8.1):
  system (stable prefix) -> summary block -> memory block -> recent tail
  (newest-first, within budget) -> retrieved knowledge (allowlist-filtered,
  spotlighted) -> current user message.

Redaction guarantee (P0-5): turns carry redacted content only, by
construction. The loader never falls back to raw content: a turn with
empty redacted content and a raw payload raises ``RedactionViolation``
(fail closed) instead of leaking raw text into the prompt.
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from backend.app.context.estimator import TokenEstimator
from backend.app.gateway.catalog import ModelCatalog, get_default_catalog

logger = structlog.get_logger(__name__)

SUMMARY_BLOCK_HEADER = (
    "Summary of the conversation so far (compaction checkpoint; immutable)."
)
MEMORY_BLOCK_HEADER = "End-user memory (opt-in facts about the customer):"
TOOL_RESULT_PLACEHOLDER = "[tool result cleared; re-fetch on demand]"

# P2-6 (Arch 8.2 cache discipline): the stable prefix (system prompt + the
# immutable summary layers) is marked with cache_control where the provider
# supports it (Anthropic); OpenAI/Gemini cache automatically and ignore it.
CACHE_CONTROL_METADATA = {"cache_control": {"type": "ephemeral"}}


class ContextBudgetError(Exception):
    """Base class for context assembly failures."""


class ContextBudgetExceeded(ContextBudgetError):
    """The system prefix or current user message cannot fit in the budget.

    These blocks are mandatory; exceeding the budget is a configuration or
    compaction error (Arch 8.2 provider-overflow trigger), not a trimming
    opportunity.
    """


class RedactionViolation(ContextBudgetError):
    """A turn carried raw content but no redacted content (P0-5 fail-closed)."""


@dataclass(frozen=True)
class ContextTurn:
    """A prior conversation turn for the recent-tail block.

    ``content`` MUST be redacted text (P0-5). ``raw_content`` is never
    rendered; it exists only so the assembler can fail closed instead of
    leaking it when a caller provides an unredacted turn.
    """

    role: str  # "user" | "assistant"
    content: str
    seq: int | None = None
    message_id: str | None = None
    has_tool_payload: bool = False
    raw_content: str | None = None

    @classmethod
    def redacted(cls, *, role: str, content: str, **kwargs: Any) -> "ContextTurn":
        """Construct a turn whose content is redacted by contract."""
        return cls(role=role, content=content, **kwargs)


@dataclass
class AssembledContext:
    """Result of a context assembly: provider messages + budget accounting."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    token_counts: dict[str, int] = field(default_factory=dict)
    omitted_turns: list[str] = field(default_factory=list)
    omitted_docs: list[int] = field(default_factory=list)
    omitted_blocks: list[str] = field(default_factory=list)
    total_tokens: int = 0
    context_window: int = 0
    budget_tokens: int = 0


class SessionContextLoader:
    """Assembles the per-turn provider prompt within the model budget."""

    def __init__(
        self,
        estimator: TokenEstimator | None = None,
        model_catalog: ModelCatalog | None = None,
        output_reserve_tokens: int | None = None,
        cache_markers: bool | None = None,
    ):
        self._estimator = estimator or TokenEstimator()
        # P2-2: model facts (context windows) come from the gateway catalog —
        # the context layer must not carry its own table.
        self._model_catalog = model_catalog or get_default_catalog()
        if output_reserve_tokens is None:
            from backend.app.settings.env import get_settings

            settings = get_settings()
            output_reserve_tokens = settings.CONTEXT_OUTPUT_RESERVE_TOKENS
        self.output_reserve_tokens = output_reserve_tokens
        # P2-6: mark the stable prefix with cache_control metadata unless the
        # operator disables it (provider still caches automatically where
        # supported — the marker only adds an explicit breakpoint).
        if cache_markers is None:
            from backend.app.settings.env import get_settings

            settings = get_settings()
            cache_markers = settings.PROMPT_CACHE_MARKERS_ENABLED
        self.cache_markers = cache_markers

    # -- public API --------------------------------------------------------

    def assemble(
        self,
        *,
        provider: str,
        model: str,
        system_prompt: str,
        current_message: str,
        history_turns: Sequence[ContextTurn] = (),
        summary: str | None = None,
        summary_position: int | None = None,
        summary_layers: Sequence[dict[str, Any]] | None = None,
        memory_facts: Sequence[str] = (),
        retrieved_docs: Sequence[dict[str, Any] | str] = (),
        clear_tool_payloads: bool = False,
        context_window_override: int | None = None,
        cache_markers: bool | None = None,
    ) -> AssembledContext:
        """Build the messages list for one LLM call, in Arch 8.1 block order.

        Mandatory blocks (system prefix, current message) that exceed the
        budget raise ``ContextBudgetExceeded`` (fail closed, compaction
        trigger). Best-effort blocks (summary, memory, tail, knowledge) are
        trimmed oldest/lowest-score first and reported in ``omitted_*``.
        ``summary_position`` (Arch 8.2 checkpoint) replaces every turn with
        ``seq <= position`` by the summary block: the head is never rendered
        twice.

        ``summary_layers`` (P2-6) renders each immutable compaction layer as
        its own system message — earlier layers are byte-identical across
        compactions, so the prompt-cache prefix survives a boundary. When
        absent, ``summary`` is rendered as a single layer (pre-P2-6 blocks).
        ``cache_markers`` overrides the loader default (settings): when on,
        the stable prefix (system + summary layers) carries cache_control
        metadata for providers that support explicit markers.
        """
        window = context_window_override or self._model_catalog.context_window(
            provider, model
        )
        budget = window - self.output_reserve_tokens
        if budget <= 0:
            raise ContextBudgetExceeded(
                f"Context window {window} does not exceed output reserve "
                f"{self.output_reserve_tokens}"
            )

        ctx = AssembledContext(
            context_window=window,
            budget_tokens=budget,
            token_counts={},
        )
        remaining = budget
        markers = self.cache_markers if cache_markers is None else cache_markers
        stable_metadata = CACHE_CONTROL_METADATA if markers else {}

        # 1. System (stable prefix; byte-identical across turns -> P2-6 cache).
        sys_tokens = self._estimator.estimate(system_prompt, provider, model)
        if sys_tokens > remaining:
            raise ContextBudgetExceeded(
                f"System prompt ({sys_tokens} tokens) exceeds context budget "
                f"{remaining}"
            )
        ctx.messages.append(
            {"role": "system", "content": system_prompt, "metadata": stable_metadata}
        )
        ctx.token_counts["system"] = sys_tokens
        remaining -= sys_tokens

        # 2. Summary block (immutable layers, P2-6; set only at compaction
        #    boundaries). Each layer becomes one system message so appended
        #    layers never rewrite earlier bytes. The block is rendered whole
        #    or not at all: a partial layer set would shift the tail prefix
        #    between turns.
        layers = self._normalize_layers(summary_layers, summary)
        if layers:
            summary_messages: list[dict[str, Any]] = []
            summary_tokens = 0
            for idx, layer in enumerate(layers):
                content = layer.get("content") or ""
                text = (
                    f"{SUMMARY_BLOCK_HEADER}\n{content}" if idx == 0 else content
                )
                tokens = self._estimator.estimate(text, provider, model)
                summary_tokens += tokens
                summary_messages.append(
                    {
                        "role": "system",
                        "content": text,
                        "metadata": stable_metadata,
                    }
                )
            if summary_tokens <= remaining:
                ctx.messages.extend(summary_messages)
                ctx.token_counts["summary"] = summary_tokens
                remaining -= summary_tokens
            else:
                ctx.omitted_blocks.append("summary")
                logger.info(
                    "context_summary_omitted",
                    tokens=summary_tokens,
                    remaining=remaining,
                    layers=len(layers),
                )
        # Absent summary -> no summary block (pre-compaction sessions).

        # 3. Memory block (opt-in end-user facts; P2-8).
        if memory_facts:
            memory_text = MEMORY_BLOCK_HEADER + "\n" + "\n".join(
                f"- {fact}" for fact in memory_facts
            )
            memory_tokens = self._estimator.estimate(memory_text, provider, model)
            if memory_tokens <= remaining:
                ctx.messages.append({"role": "system", "content": memory_text})
                ctx.token_counts["memory"] = memory_tokens
                remaining -= memory_tokens
            else:
                ctx.omitted_blocks.append("memory")

        # 4. Recent tail: newest-first walk, oldest-first rendering, never
        #    splitting a turn. Newest turns win the budget. Turns at or
        #    before the compaction position are represented by the summary
        #    block (Arch 8.2), never rendered again.
        tail_messages: list[dict[str, str]] = []
        for turn in self._recent_turns_first(history_turns):
            if (
                summary_position is not None
                and turn.seq is not None
                and turn.seq <= summary_position
            ):
                continue
            content = self._turn_content(turn, clear_tool_payloads)
            if not content:
                continue
            tokens = self._estimator.estimate(content, provider, model)
            if tokens > remaining:
                self._record_omitted_turn(ctx, turn)
                # Older turns cannot fit either: the newest-first walk has
                # already consumed the budget with newer turns.
                continue
            tail_messages.append({"role": turn.role, "content": content})
            ctx.token_counts["tail"] = ctx.token_counts.get("tail", 0) + tokens
            remaining -= tokens
        ctx.messages.extend(reversed(tail_messages))

        # 5. Retrieved knowledge (allowlist-filtered upstream; spotlighted
        #    here, highest score first).
        docs = self._sorted_docs(retrieved_docs)
        if docs:
            knowledge_messages: list[dict[str, str]] = []
            for idx, doc in enumerate(docs):
                content = doc.get("content", "")
                score = doc.get("score", 0.0)
                block = f"[Source {idx + 1}] (Score: {score:.2f})\n{content}"
                tokens = self._estimator.estimate(block, provider, model)
                if tokens > remaining:
                    ctx.omitted_docs.extend(range(idx, len(docs)))
                    break
                knowledge_messages.append({"role": "system", "content": block})
                ctx.token_counts["knowledge"] = (
                    ctx.token_counts.get("knowledge", 0) + tokens
                )
                remaining -= tokens
            ctx.messages.extend(knowledge_messages)

        # 6. Current user message (mandatory; fail closed if it cannot fit).
        current_tokens = self._estimator.estimate(current_message, provider, model)
        if current_tokens > remaining:
            raise ContextBudgetExceeded(
                f"Current message ({current_tokens} tokens) exceeds remaining "
                f"context budget {remaining}; compaction required (Arch 8.2)"
            )
        ctx.messages.append({"role": "user", "content": current_message})
        ctx.token_counts["current"] = current_tokens
        ctx.total_tokens = sum(ctx.token_counts.values())
        return ctx

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _normalize_layers(
        summary_layers: Sequence[dict[str, Any]] | None,
        summary: str | None,
    ) -> list[dict[str, Any]]:
        """Normalize summary input to a layer list (P2-6).

        ``summary_layers`` (stored compaction block) wins; ``summary``
        (legacy string, or in-memory checkpoint after a compact) becomes a
        single layer. Empty layers never produce a block.
        """
        if summary_layers:
            return [dict(layer) for layer in summary_layers]
        if summary:
            return [{"content": summary}]
        return []

    def _recent_turns_first(
        self, turns: Sequence[ContextTurn]
    ) -> Iterable[ContextTurn]:
        """Yield turns newest-first (highest seq last position wins budget).

        Input is expected oldest-first; the input index breaks seq ties so
        later turns (newer) sort ahead of earlier ones.
        """
        eligible = [
            (turn, idx) for idx, turn in enumerate(turns)
            if turn.role in ("user", "assistant")
        ]
        ordered = sorted(
            eligible,
            key=lambda p: (p[0].seq if p[0].seq is not None else 0, p[1]),
            reverse=True,
        )
        return [turn for turn, _ in ordered]

    def _turn_content(self, turn: ContextTurn, clear_tool_payloads: bool) -> str:
        if clear_tool_payloads and turn.has_tool_payload:
            return TOOL_RESULT_PLACEHOLDER
        if turn.content:
            return turn.content
        if turn.raw_content:
            # Fail closed: never leak raw text into the model context (P0-5).
            raise RedactionViolation(
                f"Turn {turn.message_id or turn.seq or '?'} carries raw content "
                "with no redacted content"
            )
        return ""

    @staticmethod
    def _record_omitted_turn(ctx: AssembledContext, turn: ContextTurn) -> None:
        label = turn.message_id or (f"seq:{turn.seq}" if turn.seq is not None else "?")
        ctx.omitted_turns.append(label)

    @staticmethod
    def _sorted_docs(docs: Sequence[dict[str, Any] | str]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for doc in docs:
            if isinstance(doc, str):
                normalized.append({"content": doc, "score": 0.0})
            else:
                normalized.append(dict(doc))
        return sorted(normalized, key=lambda d: d.get("score", 0.0), reverse=True)
