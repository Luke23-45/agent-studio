"""
Compaction service (Arch 8.2, P2-3).

Rolls the head of a thread's durable log into a structured summary block
(objective, key facts, decisions, pending work, next moves), keeping a
token-bounded live tail. The swap is atomic: snapshot -> summarize against
the snapshot -> schema validation -> single-transaction checkpoint swap;
any failure leaves the prior boundary active.

Triggers (OpenCode-compatible):
- preemptive ~70% of the effective context budget,
- reactive ~95%,
- provider-overflow one-shot recovery (orchestration retries exactly once;
  a second overflow is a hard error that degrades, never auto-falls back).

The summarizer model call has tools disabled and bounded output tokens;
the head is redacted-content only (P0-5) — raw text never reaches the
summarizer. Heads larger than the summarizer window are chunk-and-merged
(P2-4): summarize window-sized chunks, then summarize the summaries.
A per-thread breaker (P2-4) trips after 3 consecutive failures and the
service falls back to lossy truncation with a visible degraded marker.
"""

from __future__ import annotations

import json
import structlog
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from backend.app.adapters.llm import BaseLLMAdapter, LLMConfig, LLMMessage
from backend.app.application.compaction.breaker import (
    CompactionBreaker,
    get_compaction_breaker,
)
from backend.app.context import TokenEstimator
from backend.app.gateway.catalog import ModelCatalog, get_default_catalog

logger = structlog.get_logger(__name__)

SUMMARY_FIELDS: tuple[str, ...] = (
    "objective",
    "key_facts",
    "decisions",
    "pending_work",
    "next_moves",
)


class CompactionError(Exception):
    """Base class for compaction failures."""


class SummaryValidationError(CompactionError):
    """The summarizer output failed schema validation (atomicity preserved)."""


class CompactionAborted(CompactionError):
    """Compaction could not run (head too large, generator failure, ...).

    The prior checkpoint remains active; the session continues un-compacted.
    """


@dataclass
class CompactionConfig:
    preemptive_ratio: float = 0.70
    reactive_ratio: float = 0.95
    keep_tokens: int = 8000
    buffer_tokens: int = 20000
    max_output_tokens: int = 4096
    max_snapshot_messages: int = 1000
    # P2-4 (Neryva addition): breaker + lossy-truncation fallback.
    max_failures: int = 3
    truncation_keep_turns: int = 10
    # P2-4 (Neryva addition): chunk-and-merge round bound.
    max_merge_depth: int = 4
    # P2-6 (Neryva addition): summary layers are appended immutably so the
    # prompt-cache prefix survives compaction boundaries; at this many
    # layers the summary is consolidated into a single rewritten layer
    # (bounded cache invalidation — at most once per N boundaries).
    max_summary_layers: int = 5


DEGRADED_SUMMARY_CONTENT = (
    "Summary unavailable (compaction degraded; older context truncated)."
)


@dataclass
class CompactionDecision:
    """Outcome of a trigger evaluation."""

    action: str | None  # "preemptive" | "reactive" | None
    estimated_tokens: int
    budget_tokens: int

    @property
    def triggered(self) -> bool:
        return self.action is not None


class SummarySchema:
    """Validation + deterministic rendering of the summary payload."""

    @staticmethod
    def validate(payload: Any) -> list[str]:
        """Return a list of schema violations (empty when valid)."""
        errors: list[str] = []
        if not isinstance(payload, dict):
            return ["summary payload must be a JSON object"]
        for field_name in SUMMARY_FIELDS:
            if field_name not in payload:
                errors.append(f"missing field '{field_name}'")
                continue
            value = payload[field_name]
            if field_name == "objective":
                if not isinstance(value, str) or not value.strip():
                    errors.append("'objective' must be a non-empty string")
            else:
                if not isinstance(value, list) or not all(
                    isinstance(v, str) for v in value
                ):
                    errors.append(f"'{field_name}' must be a list of strings")
        return errors

    @staticmethod
    def render(payload: dict[str, Any]) -> str:
        """Deterministic rendering for the model-visible summary block."""
        lines = [f"Objective: {payload.get('objective', '')}"]
        for field_name in SUMMARY_FIELDS[1:]:
            values = payload.get(field_name) or []
            if values:
                label = field_name.replace("_", " ").title()
                lines.append(f"{label}:")
                lines.extend(f"- {v}" for v in values)
        return "\n".join(lines)


_SUMMARY_PROMPT = """You are a conversation summarizer for a customer-support agent.
Summarize the conversation below into a structured JSON payload.

Follow this schema exactly. Output JSON only — no markdown, no code fences:
{{
  "objective": "string",
  "key_facts": ["string"],
  "decisions": ["string"],
  "pending_work": ["string"],
  "next_moves": ["string"]
}}

Rules:
- Use only the conversation content provided; never invent facts.
- Keep facts terse and specific.
- Omit personally identifiable information that is not needed.

Conversation:
{conversation}
"""


class LLMSummaryGenerator:
    """Summarizes a head of turns with an LLM (tools disabled, bounded out).

    The provider adapter is injected so tests can substitute a stub;
    heads larger than the summarizer window are chunk-and-merged (P2-4).
    """

    def __init__(
        self,
        adapter: BaseLLMAdapter,
        config: CompactionConfig | None = None,
        estimator: TokenEstimator | None = None,
        model_catalog: ModelCatalog | None = None,
    ):
        self.adapter = adapter
        self.config = config or CompactionConfig()
        self.estimator = estimator or TokenEstimator()
        self.model_catalog = model_catalog or get_default_catalog()

    def summarizer_window(self) -> int:
        """Tokens available to the summarizer for the head (window - out - reserve)."""
        window = self.model_catalog.context_window(
            self.adapter.provider_type.value,
            self.adapter.config.model,
        )
        return window - self.config.max_output_tokens - 4096

    async def summarize(self, head_turns: Sequence[dict[str, Any]]) -> dict[str, Any]:
        """Summarize redacted turns into a validated summary payload.

        Heads that fit the summarizer window take one call; larger heads
        are chunk-and-merged (P2-4): summarize window-sized chunks, then
        summarize the summaries until a single payload fits.
        """
        turns = [
            dict(turn)
            for turn in head_turns
            if turn.get("redacted_content")
        ]
        if not turns:
            raise CompactionAborted("nothing to summarize (empty head)")
        return await self._merge(turns, depth=0)

    def _chunk_turns(
        self, turns: Sequence[dict[str, Any]], window: int
    ) -> list[list[dict[str, Any]]]:
        """Greedily partition turns into window-fitting chunks (chronological)."""
        chunks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        acc = 0
        for turn in turns:
            tokens = self.estimator.estimate(turn.get("redacted_content") or "")
            if tokens > window:
                raise CompactionAborted(
                    f"single turn ({tokens} tokens) exceeds summarizer context ({window})"
                )
            if current and acc + tokens > window:
                chunks.append(current)
                current = []
                acc = 0
            current.append(turn)
            acc += tokens
        if current:
            chunks.append(current)
        return chunks

    async def _merge(
        self, turns: Sequence[dict[str, Any]], depth: int
    ) -> dict[str, Any]:
        """Recursive summarize halves, then summarize the summaries."""
        window = self.summarizer_window()
        chunks = self._chunk_turns(turns, window)
        if len(chunks) == 1:
            return await self._summarize_turns(chunks[0])
        if depth >= self.config.max_merge_depth:
            raise CompactionAborted(
                "chunk-and-merge did not converge "
                f"(depth {depth}, chunks {len(chunks)})"
            )
        rendered = [
            SummarySchema.render(await self._summarize_turns(chunk))
            for chunk in chunks
        ]
        pseudo_turns = [
            {"role": "summary", "redacted_content": text} for text in rendered
        ]
        return await self._merge(pseudo_turns, depth + 1)

    async def _summarize_turns(
        self, turns: Sequence[dict[str, Any]]
    ) -> dict[str, Any]:
        """One summarizer call over a window-fitting turn list."""
        conversation = "\n".join(
            f"{turn.get('role', 'user')}: {turn['redacted_content']}"
            for turn in turns
            if turn.get("redacted_content")
        )
        prompt = _SUMMARY_PROMPT.format(conversation=conversation)
        config = LLMConfig(
            model=self.adapter.config.model,
            temperature=0.0,
            max_tokens=self.config.max_output_tokens,
        )
        # Tools disabled: the adapter is used bare (no tool wiring) and the
        # prompt demands JSON-only output.
        try:
            response = await self.adapter.chat(
                [LLMMessage(role="user", content=prompt)]
            )
        except Exception as e:
            raise CompactionAborted(f"summarizer call failed: {e}") from e

        payload = self._parse_json(response.content)
        errors = SummarySchema.validate(payload)
        if errors:
            raise SummaryValidationError("; ".join(errors))
        return payload

    @staticmethod
    def _parse_json(text: str) -> Any:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise SummaryValidationError(f"summarizer returned invalid JSON: {e}") from e


class CompactionService:
    """Evaluates triggers and performs atomic checkpoint swaps."""

    def __init__(
        self,
        threads: Any,
        generator: LLMSummaryGenerator,
        config: CompactionConfig | None = None,
        estimator: TokenEstimator | None = None,
        breaker: CompactionBreaker | None = None,
    ):
        self.threads = threads
        self.generator = generator
        self.config = config or CompactionConfig()
        self.estimator = estimator or TokenEstimator()
        # P2-4: shared in-process breaker (per-thread state survives across
        # request-scoped service instances; Redis-shared in P3-4).
        self.breaker = breaker or get_compaction_breaker()

    # -- triggers ----------------------------------------------------------

    def evaluate(
        self,
        *,
        estimated_tokens: int,
        budget_tokens: int,
        has_summary: bool,
    ) -> CompactionDecision:
        """Decide whether to compact this turn.

        Reactive (>= 95%) fires first; preemptive (>= 70%) fires when the
        context is growing. ``has_summary`` distinguishes a refresh from a
        first checkpoint (logged only; both compact).
        """
        if budget_tokens <= 0:
            return CompactionDecision(None, estimated_tokens, budget_tokens)
        ratio = estimated_tokens / budget_tokens
        if ratio >= self.config.reactive_ratio:
            action = "reactive"
        elif ratio >= self.config.preemptive_ratio:
            action = "preemptive"
        else:
            action = None
        if action:
            logger.info(
                "compaction_trigger",
                action=action,
                ratio=round(ratio, 3),
                estimated_tokens=estimated_tokens,
                budget_tokens=budget_tokens,
                has_summary=has_summary,
            )
        return CompactionDecision(action, estimated_tokens, budget_tokens)

    # -- mechanics ---------------------------------------------------------

    async def compact(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Snapshot -> summarize -> validate -> atomic swap.

        Returns ``{"compacted": bool, "summary": str|None, ...checkpoint}``.
        Any failure raises (or returns compacted=False) with the prior
        checkpoint still active — never a partial state.

        P2-6 (cache discipline): the new checkpoint APPENDS an immutable
        summary layer covering only the turns after the previous boundary
        (previous layers stay byte-identical, so the prompt-cache prefix
        survives the swap). At ``max_summary_layers`` layers the summary is
        consolidated into a single rewritten layer — a full rewrite is
        accepted, but bounded to at most one per N boundaries.
        """
        snapshot = await self._snapshot(tenant_id, thread_id)
        if not snapshot:
            return self._noop_result()
        head, _tail = self._split(snapshot)
        if not head:
            return self._noop_result()

        breaker_key = f"{tenant_id}:{thread_id}"
        # P2-4: tripped breaker -> lossy truncation fallback (keep newest K
        # turns live, drop the middle, mark degraded) instead of retrying a
        # failing summarizer; never silent.
        if await self.breaker.is_tripped(breaker_key):
            return await self._truncate_fallback(
                tenant_id, thread_id, snapshot, breaker_key, request_id
            )

        old_layers, prev_position = await self._current_layers(
            tenant_id, thread_id
        )
        # Turns beyond the previous boundary only: nothing new -> noop.
        head_slice = [m for m in head if m.get("seq", 0) > prev_position]
        if not head_slice:
            return self._noop_result()
        position = head_slice[-1]["seq"]
        boundary_message_id = head_slice[-1]["id"]

        try:
            if len(old_layers) >= self.config.max_summary_layers:
                # Consolidate: one layer covering the ENTIRE head (whole-log
                # rewrite, bounded by max_summary_layers; chunk-and-merge
                # inside the generator handles any size).
                payload = await self.generator.summarize(head)
                layers = [
                    {
                        "position": position,
                        "content": SummarySchema.render(payload),
                        "payload": payload,
                    }
                ]
            else:
                # Append: summarize only the growth since the last boundary;
                # earlier layers stay immutable (P2-6 cache discipline).
                payload = await self.generator.summarize(head_slice)
                layers = old_layers + [
                    {
                        "position": position,
                        "content": SummarySchema.render(payload),
                        "payload": payload,
                    }
                ]
            # Re-validate regardless of generator implementation: the swap is
            # only reached with a schema-clean payload (atomicity contract).
            errors = SummarySchema.validate(payload)
            if errors:
                raise SummaryValidationError("; ".join(errors))
            rendered = layers[-1]["content"]
            checkpoint = await self.threads.set_summary(
                tenant_id,
                thread_id,
                summary_block={
                    "content": "\n\n".join(layer["content"] for layer in layers),
                    "payload": payload,
                    "head_until_seq": position,
                    "layers": layers,
                },
                summary_position=position,
                boundary_message_id=boundary_message_id,
                request_id=request_id,
            )
        except Exception as e:
            await self.breaker.record_failure(breaker_key)
            raise
        await self.breaker.record_success(breaker_key)
        logger.info(
            "compaction_committed",
            tenant_id=tenant_id,
            thread_id=thread_id,
            head_messages=len(head),
            summary_version=checkpoint["summary_version"],
            position=position,
            layers=len(layers),
        )
        return {
            "compacted": True,
            "summary": rendered,
            "summary_version": checkpoint["summary_version"],
            "summary_position": checkpoint["summary_position"],
            "event": checkpoint["event"],
            "degraded": False,
            "layers": layers,
        }

    async def _truncate_fallback(
        self,
        tenant_id: str,
        thread_id: str,
        snapshot: Sequence[dict[str, Any]],
        breaker_key: str,
        request_id: str | None,
    ) -> dict[str, Any]:
        """Lossy truncation (P2-4): drop the middle, keep newest K turns.

        Runs while the breaker is tripped; swaps a degraded checkpoint so
        the assembler keeps serving the recent tail and the operators see
        the event marker. Same atomic swap path — never partial.
        """
        keep = self.config.truncation_keep_turns
        if len(snapshot) <= keep:
            return self._noop_result()
        boundary = snapshot[-(keep + 1)]
        checkpoint = await self.threads.set_summary(
            tenant_id,
            thread_id,
            summary_block={
                "content": DEGRADED_SUMMARY_CONTENT,
                "payload": {},
                "head_until_seq": boundary["seq"],
                "degraded": True,
                # P2-6: truncation drops the whole layer stack (lossy).
                "layers": [],
            },
            summary_position=boundary["seq"],
            boundary_message_id=boundary["id"],
            request_id=request_id,
            degraded=True,
        )
        logger.warning(
            "compaction_degraded_truncation",
            tenant_id=tenant_id,
            thread_id=thread_id,
            keep_turns=keep,
            dropped_until_seq=boundary["seq"],
            breaker_key=breaker_key,
        )
        return {
            "compacted": True,
            "summary": DEGRADED_SUMMARY_CONTENT,
            "summary_version": checkpoint["summary_version"],
            "summary_position": checkpoint["summary_position"],
            "event": checkpoint["event"],
            "degraded": True,
        }

    # -- internals ---------------------------------------------------------

    async def _current_layers(
        self, tenant_id: str, thread_id: str
    ) -> tuple[list[dict[str, Any]], int]:
        """Load the checkpoint's immutable layer stack (P2-6).

        Returns ``(layers, prev_position)`` where ``prev_position`` is the
        seq covered by the last layer (0 when no summary exists yet).
        Pre-P2-6 blocks (no ``layers`` key) degrade to a single legacy
        layer so the next compaction appends instead of rewriting.
        """
        row = await self.threads.get_thread(tenant_id, thread_id)
        block = (row or {}).get("summary_block") or {}
        layers = [dict(layer) for layer in (block.get("layers") or [])]
        if not layers and block.get("content"):
            layers = [
                {
                    "position": (row or {}).get("summary_position") or 0,
                    "content": block.get("content"),
                    "payload": block.get("payload") or {},
                }
            ]
        prev_position = layers[-1].get("position") or 0 if layers else 0
        return layers, prev_position

    @staticmethod
    def _noop_result() -> dict[str, Any]:
        return {
            "compacted": False,
            "summary": None,
            "summary_version": None,
            "summary_position": None,
        }

    async def _snapshot(self, tenant_id: str, thread_id: str) -> list[dict[str, Any]]:
        """Ascending-seq snapshot of the thread log (bounded page walk)."""
        messages: list[dict[str, Any]] = []
        after_seq: int | None = None
        while len(messages) < self.config.max_snapshot_messages:
            page = await self.threads.list_messages(
                tenant_id,
                thread_id,
                after_seq=after_seq,
                limit=200,
            )
            if not page["messages"]:
                break
            messages.extend(page["messages"])
            if not page["has_more"]:
                break
            after_seq = page["messages"][-1]["seq"]
        return messages

    def _split(self, messages: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split into (head, tail): the newest ~keep_tokens stay live.

        Walks newest -> oldest accumulating tail tokens; the boundary is the
        first message that would push the tail over ``keep_tokens``. The
        head is everything older (summarized); it may be empty.
        """
        acc = 0
        tail_idx = 0
        for i in range(len(messages) - 1, -1, -1):
            tokens = self.estimator.estimate(
                messages[i].get("redacted_content") or ""
            )
            if acc + tokens > self.config.keep_tokens:
                tail_idx = i + 1
                break
            acc += tokens
        return list(messages[:tail_idx]), list(messages[tail_idx:])


# Type alias for the orchestration hook: given context estimates, return the
# new checkpoint ("summary" + "position") or None when nothing changed.
CompactionHook = Callable[[dict[str, Any]], Any]
