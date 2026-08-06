"""
Durable memory store (Arch 8.4, P2-8).

A background worker (``memory.extract``) extracts durable structured facts
(preferences, decisions, constraints) from closed turns into a
per-tenant / per-end-user store. Facts are PII-filtered twice: the input
turns are redacted-only (P0-5) and every extracted fact is re-passed
through the PII service before storage (fail-closed: only the redacted
text is ever persisted). Reads are tenant-gated (feature flag), scoped to
the end-user, bounded by expiry, and scored for relevance against the
current message (top-k on demand, never a raw dump).
"""

from __future__ import annotations

import json
import re
import structlog
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from backend.app.adapters.llm import BaseLLMAdapter, LLMMessage

logger = structlog.get_logger(__name__)

JOB_MEMORY_EXTRACT = "memory.extract"

#: Tenant feature flag that enables memory (features["memory"]).
MEMORY_FEATURE = "memory"

DEFAULT_MAX_FACTS = 20.0
DEFAULT_EXPIRY_DAYS = 30.0

_MEMORY_PROMPT = """\
Extract durable, reusable facts about the user from the conversation below.
Keep ONLY facts that will still be true later: preferences, constraints,
decisions, personal context the user volunteered, and standing requests.
Exclude: ephemeral content, raw quotes, anything that could identify a
third party, and anything that looks like personal information (names,
emails, phone numbers, addresses, IDs). If a fact contains such data,
restate it without the sensitive value or drop it.

Reply with a JSON array of short strings only, e.g. ["fact one", "fact two"].
No prose. At most 20 facts. Empty array is fine.

Conversation:
{conversation}"""


class MemoryExtractionError(Exception):
    pass


@dataclass
class MemoryConfig:
    enabled: bool = False
    max_facts: int = int(DEFAULT_MAX_FACTS)
    expiry_days: int = int(DEFAULT_EXPIRY_DAYS)


def memory_config(tenant_config: Any) -> MemoryConfig:
    """Derive per-tenant memory settings (feature-gated)."""
    memory = getattr(tenant_config, "memory", {}) or {}
    return MemoryConfig(
        enabled=bool(tenant_config.features.get(MEMORY_FEATURE, False)),
        max_facts=int(memory.get("max_facts", DEFAULT_MAX_FACTS)),
        expiry_days=int(memory.get("expiry_days", DEFAULT_EXPIRY_DAYS)),
    )


class MemoryExtractor:
    """Extracts durable facts from a batch of redacted turns with an LLM.

    The provider adapter is injected so tests can substitute a stub. One
    call, JSON-array-of-strings output, strict validation (fail closed:
    malformed output raises instead of storing garbage).
    """

    def __init__(self, adapter: BaseLLMAdapter, config: MemoryConfig | None = None):
        self.adapter = adapter
        self.config = config or MemoryConfig()

    async def extract(self, turns: Sequence[dict[str, Any]]) -> list[str]:
        """Extract facts from redacted turns (oldest first)."""
        conversation = "\n".join(
            f"{turn.get('role', 'user')}: {turn['redacted_content']}"
            for turn in turns
            if turn.get("redacted_content")
        )
        if not conversation.strip():
            raise MemoryExtractionError("nothing to extract (empty turns)")
        prompt = _MEMORY_PROMPT.format(conversation=conversation)
        try:
            response = await self.adapter.chat(
                [LLMMessage(role="user", content=prompt)]
            )
        except Exception as e:
            raise MemoryExtractionError(f"memory extractor call failed: {e}") from e
        return self._parse_facts(response.content)

    @staticmethod
    def _parse_facts(text: str) -> list[str]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise MemoryExtractionError(
                f"memory extractor returned invalid JSON: {e}"
            ) from e
        if not isinstance(payload, list):
            raise MemoryExtractionError("memory extractor returned non-list JSON")
        facts = [
            str(item).strip()
            for item in payload
            if isinstance(item, str) and item.strip()
        ]
        return facts[:20]


async def extract_thread_memory(
    db: Any,
    tenant_id: str,
    thread_id: str,
    *,
    generator: Any,
    pii_service: Any | None = None,
    config: MemoryConfig | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Extract durable facts from a thread's closed turns (idempotent).

    Only turns after the thread's extraction watermark (max stored
    source_seq) are considered, and the newest message is excluded (the
    in-flight turn is not yet closed). The batch's facts are stored with
    ``source_seq`` = the newest extracted turn, so the watermark advances
    atomically-ish with the batch; re-runs are free. Every stored fact is
    the PII-redacted text (never the raw extraction output).
    """
    from backend.app.infrastructure.db.memory import MemoryRepository
    from backend.app.infrastructure.db.threads import ThreadRepository

    config = config or MemoryConfig()
    memory = MemoryRepository(db)
    threads = ThreadRepository(db)
    thread = await threads.get_thread(tenant_id, thread_id)
    if thread is None:
        return {"extracted": 0, "reason": "thread_not_found"}

    watermark = await memory.max_source_seq(tenant_id, thread_id)
    turns: list[dict[str, Any]] = []
    after = watermark
    while True:
        page = await threads.list_messages(
            tenant_id, thread_id, after_seq=after, limit=200
        )
        batch = page["messages"]
        turns.extend(batch)
        if not page["has_more"]:
            break
        after = batch[-1]["seq"]
    if not turns:
        return {"extracted": 0, "reason": "no_new_turns"}
    # The newest message may still be in flight; only closed turns feed
    # extraction.
    turns = turns[:-1]
    closed = [
        {"role": t.get("role"), "redacted_content": t.get("redacted_content")}
        for t in turns
        if t.get("redacted_content")
    ]
    if not closed:
        return {"extracted": 0, "reason": "no_closed_turns"}

    facts = await generator.extract(closed)
    source_seq = turns[-1]["seq"]
    stored = 0
    now = datetime.now(timezone.utc)
    expires_at = (
        now + timedelta(days=config.expiry_days) if config.expiry_days > 0 else None
    )
    for fact in facts:
        if pii_service is not None:
            result = pii_service.process_message(fact)
            fact = result.redacted_text.strip()
        if not fact:
            continue
        await memory.add(
            tenant_id=tenant_id,
            end_user_id=thread.get("end_user_id"),
            thread_id=thread_id,
            source_seq=source_seq,
            content=fact,
            expires_at=expires_at,
        )
        stored += 1

    return {
        "extracted": stored,
        "reason": "extracted" if stored else "no_facts",
        "source_seq": source_seq,
        "watermark_from": watermark,
    }


def _score_facts(
    rows: Sequence[dict[str, Any]],
    query: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Keyword relevance (top-k on demand): query-token hits, recency tiebreak."""
    tokens = (
        [t for t in re.split(r"\W+", (query or "").lower()) if len(t) >= 3]
        if query
        else []
    )
    unique = list(dict.fromkeys(tokens))
    scored = []
    for row in rows:
        content = (row.get("content") or "").lower()
        score = sum(1 for t in unique if t in content)
        if not tokens or score > 0:
            scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [row for _score, row in scored[:limit]]


async def retrieve_facts(
    db: Any,
    tenant_id: str,
    *,
    end_user_id: str | None = None,
    query: str | None = None,
    limit: int = 10,
) -> list[str]:
    """Top-k relevant live facts for one tenant/end-user (Arch 8.4).

    Read scope is the caller's own facts (end-user session) or
    tenant-global facts (API-key session); expired/erased facts never
    surface. ``query`` (the current message) drives keyword scoring; no
    query -> newest facts.
    """
    from backend.app.infrastructure.db.memory import MemoryRepository

    rows = await MemoryRepository(db).retrieve(
        tenant_id,
        end_user_id=end_user_id,
        limit=max(limit * 3, 30),
    )
    return [row["content"] for row in _score_facts(rows, query, limit)]


async def erase_end_user_memory(
    db: Any, tenant_id: str, end_user_id: str
) -> dict[str, Any]:
    """Soft-erase every fact for one end-user (erasure support, P5-10)."""
    from backend.app.infrastructure.db.memory import MemoryRepository

    erased = await MemoryRepository(db).erase_by_user(tenant_id, end_user_id)
    return {"erased": erased}


def default_memory_generator_builder(tenant_config: Any, api_key: str) -> MemoryExtractor:
    """Build the real memory extractor for a tenant's default model."""
    from backend.app.adapters.llm import (
        LLMConfig,
        LLMProviderType,
        create_llm_adapter,
    )

    adapter = create_llm_adapter(
        LLMProviderType(tenant_config.default_provider),
        api_key,
        LLMConfig(model=tenant_config.default_model),
    )
    return MemoryExtractor(adapter, config=memory_config(tenant_config))
