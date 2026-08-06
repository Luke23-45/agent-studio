"""
Orchestration service using LangGraph.

Implements the agent decision loop: propose -> evaluate -> select -> execute.
"""

import asyncio
import structlog
from typing import Any, AsyncGenerator, Awaitable, Callable
from uuid import UUID

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from typing_extensions import TypedDict

from backend.app.adapters.llm import (
    BaseLLMAdapter,
    LLMConfig,
    LLMMessage,
    LLMProviderType,
    LLMResponse,
    _extract_usage,
    create_llm_adapter,
)
from backend.app.application.compaction import (
    CompactionConfig,
    CompactionHook,
)
from backend.app.context import (
    AssembledContext,
    ContextBudgetExceeded,
    ContextTurn,
    SessionContextLoader,
)
from backend.app.domain.policy import PolicyAction, PolicySet
from backend.app.domain.tenant import TenantConfig
from backend.app.gateway.catalog import ModelCatalog, get_default_catalog
from backend.app.gateway.client import (
    GatewayClientConfig,
    ResilientLLMClient,
    get_llm_client,
)

logger = structlog.get_logger(__name__)


class AgentState(TypedDict):
    """State passed through the LangGraph workflow."""

    tenant_id: UUID
    session_id: str
    user_message: str
    redacted_message: str
    context: dict[str, Any]
    conversation_history: list[dict[str, Any]]
    retrieved_docs: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    model_response: str | None
    validation_result: dict[str, Any]
    policy_action: PolicyAction
    confidence: float
    handoff_required: bool
    redact_attempts: int
    budget_exceeded: bool
    context_summary: str | None
    context_summary_position: int | None
    # P2-6: immutable summary layers (each byte-stable across compactions).
    context_summary_layers: list[dict[str, Any]] | None
    error: str | None


class OrchestrationService:
    """Main orchestration service for agent workflows."""

    def __init__(
        self,
        tenant_config: TenantConfig,
        policy_set: PolicySet,
        llm_api_key: str,
        retrieval_service: Any | None = None,
        escalation_service: Any | None = None,
        model_override: str | None = None,
        context_loader: SessionContextLoader | None = None,
        model_catalog: ModelCatalog | None = None,
        compaction_callback: CompactionHook | None = None,
        compaction_config: CompactionConfig | None = None,
    ):
        self.tenant_config = tenant_config
        self.policy_set = policy_set
        self.llm_api_key = llm_api_key
        self.retrieval_service = retrieval_service
        self.escalation_service = escalation_service
        self.model_override = model_override
        # P2-2: model facts (context windows, price cards) come from the
        # gateway catalog; the context layer consumes it, never redefines it.
        self.model_catalog = model_catalog or get_default_catalog()
        # P2-1: the context assembler is the single component that builds
        # provider messages; the generate node only renders its output.
        self.context_loader = context_loader or SessionContextLoader(
            model_catalog=self.model_catalog
        )
        # P2-3: optional compaction hook (provided by the route, which owns
        # DB access). When set, the generate node triggers preemptive/
        # reactive compaction and retries a context overflow exactly once.
        self.compaction_callback = compaction_callback
        self.compaction_config = compaction_config or CompactionConfig()
        self.graph: CompiledStateGraph | None = None
        # When set, the generate node streams token deltas through it
        # (used by the SSE endpoint); None keeps the plain chat() path.
        self.stream_callback: Callable[[str], Awaitable[None]] | None = None
        # When set, a policy-decision evidence packet is emitted on every
        # processed message (may be sync or awaitable; failures are logged,
        # never raised).
        self.evidence_callback: Callable[[dict[str, Any]], Any] | None = None
        # When set, per-turn token usage (Arch 10, P0-10) is emitted after
        # every completed generation (may be sync or awaitable; failures are
        # logged, never raised).
        self.usage_callback: Callable[[dict[str, Any]], Any] | None = None
        self._build_graph()

    def _get_llm_adapter(self) -> BaseLLMAdapter:
        """Create LLM adapter based on tenant config."""
        provider_type = LLMProviderType(self.tenant_config.default_provider)
        model = self.model_override or self.tenant_config.default_model
        config = LLMConfig(model=model)
        return create_llm_adapter(provider_type, self.llm_api_key, config)

    def _get_llm_client(self) -> ResilientLLMClient:
        """Resilient gateway client (timeout, retry, breaker) for the tenant.

        Per-call timeout is tenant-tunable via ``budgets.llm_timeout_seconds``.
        """
        adapter = self._get_llm_adapter()
        timeout = self.tenant_config.budgets.get("llm_timeout_seconds")
        config = GatewayClientConfig(
            call_timeout_seconds=float(timeout) if timeout else 60.0
        )
        return get_llm_client(str(self.tenant_config.id), adapter, config=config)

    def _build_graph(self) -> None:
        """Build the LangGraph workflow."""
        workflow = StateGraph(AgentState)

        # Add nodes
        workflow.add_node("process_input", self._process_input)
        workflow.add_node("retrieve_context", self._retrieve_context)
        workflow.add_node("generate_response", self._generate_response)
        workflow.add_node("validate_output", self._validate_output)
        workflow.add_node("check_policy", self._check_policy)
        workflow.add_node("prepare_handoff", self._prepare_handoff)

        # Set entry point
        workflow.set_entry_point("process_input")

        # Add edges
        workflow.add_edge("process_input", "retrieve_context")
        workflow.add_edge("retrieve_context", "generate_response")
        workflow.add_edge("generate_response", "validate_output")
        workflow.add_edge("validate_output", "check_policy")

        # Conditional edges after policy check
        workflow.add_conditional_edges(
            "check_policy",
            self._route_after_policy,
            {
                "allow": END,
                "block": "prepare_handoff",
                "escalate": "prepare_handoff",
                "redact": "generate_response",
            },
        )

        workflow.add_edge("prepare_handoff", END)

        self.graph = workflow.compile()

    def _process_input(self, state: AgentState) -> AgentState:
        """Process and validate input message."""
        logger.info("processing_input", tenant_id=state["tenant_id"])
        # Input already redacted by PII layer before reaching here
        state["context"] = {
            "tenant_id": str(state["tenant_id"]),
            "user_message": state["redacted_message"],
            "conversation_history": state.get("conversation_history", []),
        }
        return state

    async def _retrieve_context(self, state: AgentState) -> AgentState:
        """Retrieve relevant context from knowledge base."""
        logger.info("retrieving_context", tenant_id=state["tenant_id"])

        if not self.retrieval_service:
            logger.info("retrieval_disabled", tenant_id=state["tenant_id"])
            state["retrieved_docs"] = []
            return state

        try:
            result = await self.retrieval_service.retrieve(
                query_text=state["redacted_message"],
                tenant_id=self.tenant_config.id,
                allowed_sources=self.tenant_config.knowledge_allowlist,
            )
            state["retrieved_docs"] = [
                {
                    "content": r.document.content,
                    "score": r.score,
                    "metadata": r.document.metadata,
                }
                for r in result.results
            ]
            # Citations for the final response: source, document id, score.
            state["citations"] = [
                {
                    "source": doc["metadata"].get("source"),
                    "document_id": doc["metadata"].get("document_id"),
                    "chunk_id": doc["metadata"].get("chunk_id"),
                    "score": doc["score"],
                }
                for doc in state["retrieved_docs"]
            ]
            if result.context:
                state["context"]["retrieval_context"] = result.context
            logger.info(
                "retrieval_complete",
                tenant_id=state["tenant_id"],
                result_count=len(state["retrieved_docs"]),
            )
        except Exception as e:
            logger.warning("retrieval_failed", tenant_id=state["tenant_id"], error=str(e))
            state["retrieved_docs"] = []
            state["citations"] = []
        return state

    async def _generate_response(self, state: AgentState) -> AgentState:
        """Generate response using the LLM.

        P2-3: when a compaction hook is wired, the node triggers compaction
        preemptively (~70%) / reactively (~95%) via the hook, and a context
        overflow (local preflight or provider error) is recovered by
        compacting and retrying exactly once; a second overflow is a hard
        error that degrades — never an auto-fallback.
        """
        logger.info("generating_response", tenant_id=state["tenant_id"])

        try:
            llm = self._get_llm_client()
            system_prompt = self._build_system_prompt(state)
            compacted_this_turn = False
            stream_emitted = False
            response: LLMResponse | None = None

            while True:
                try:
                    assembled = self._assemble_context(state, system_prompt)
                except ContextBudgetExceeded as e:
                    if compacted_this_turn or self.compaction_callback is None:
                        raise
                    compacted_this_turn = True
                    logger.info(
                        "compaction_overflow_preflight",
                        tenant_id=state["tenant_id"],
                        error=str(e),
                    )
                    if await self._recover_overflow(state, assembled=None):
                        continue
                    raise

                # P2-3 preemptive/reactive trigger (at most once per turn).
                if (
                    self.compaction_callback is not None
                    and not compacted_this_turn
                    and assembled.total_tokens
                    > assembled.budget_tokens * self.compaction_config.preemptive_ratio
                ):
                    checkpoint = await self.compaction_callback(
                        {
                            "estimated_tokens": assembled.total_tokens,
                            "budget_tokens": assembled.budget_tokens,
                            "has_summary": state.get("context_summary") is not None,
                            "overflow": False,
                        }
                    )
                    if checkpoint:
                        state["context_summary"] = checkpoint.get("summary")
                        state["context_summary_position"] = checkpoint.get("position")
                        state["context_summary_layers"] = checkpoint.get("layers")
                        compacted_this_turn = True  # one compaction per turn
                        continue

                messages = [
                    LLMMessage(
                        role=m["role"],
                        content=m["content"],
                        metadata=m.get("metadata") or {},
                    )
                    for m in assembled.messages
                ]
                logger.info(
                    "context_assembled",
                    tenant_id=state["tenant_id"],
                    total_tokens=assembled.total_tokens,
                    budget_tokens=assembled.budget_tokens,
                    message_count=len(assembled.messages),
                    omitted_turns=len(assembled.omitted_turns),
                    omitted_docs=len(assembled.omitted_docs),
                )

                try:
                    if self.stream_callback is not None:
                        original_callback = self.stream_callback

                        async def _tracked(chunk: str) -> None:
                            nonlocal stream_emitted
                            stream_emitted = True
                            await original_callback(chunk)

                        self.stream_callback = _tracked
                        try:
                            response = await self._generate_response_streaming(
                                llm, messages
                            )
                        finally:
                            self.stream_callback = original_callback
                    else:
                        response = await llm.chat(messages)
                    break
                except Exception as e:
                    # Provider-overflow one-shot recovery: retried exactly
                    # once with a fresh checkpoint; mid-stream overflows
                    # cannot retry (deltas already sent).
                    if (
                        not compacted_this_turn
                        and not stream_emitted
                        and self.compaction_callback is not None
                        and self._is_provider_overflow(e)
                    ):
                        compacted_this_turn = True
                        logger.info(
                            "compaction_overflow_recovery",
                            tenant_id=state["tenant_id"],
                            error=str(e),
                        )
                        if await self._recover_overflow(state, assembled=assembled):
                            continue
                    raise

            state["model_response"] = response.content
            state["confidence"] = self._estimate_confidence(response)
            await self._record_usage(state, llm, response)
        except Exception as e:
            logger.error("llm_error", error=str(e))
            state["error"] = str(e)
            state["confidence"] = 0.0

        return state

    def _assemble_context(
        self, state: AgentState, system_prompt: str
    ) -> AssembledContext:
        """Render the provider messages via the SessionContextLoader (P2-1)."""
        turns = [
            ContextTurn(
                role=turn.get("role", "user"),
                content=turn.get("content", ""),
                seq=turn.get("seq"),
                message_id=turn.get("message_id"),
                has_tool_payload=bool(turn.get("has_tool_payload")),
            )
            for turn in state.get("conversation_history", [])
        ]
        return self.context_loader.assemble(
            provider=self.tenant_config.default_provider,
            model=self.model_override or self.tenant_config.default_model,
            system_prompt=system_prompt,
            current_message=state["redacted_message"],
            history_turns=turns,
            summary=state.get("context_summary"),
            summary_position=state.get("context_summary_position"),
            summary_layers=state.get("context_summary_layers"),
            retrieved_docs=state.get("retrieved_docs") or [],
        )

    async def _recover_overflow(
        self,
        state: AgentState,
        assembled: AssembledContext | None,
    ) -> bool:
        """Ask the compaction hook for a fresh checkpoint (P2-3 overflow path).

        Returns True when a checkpoint was produced (caller retries the
        generation exactly once with it); False means the session continues
        degraded without retrying.
        """
        checkpoint = await self.compaction_callback(
            {
                "estimated_tokens": (
                    assembled.total_tokens if assembled else None
                ),
                "budget_tokens": (
                    assembled.budget_tokens if assembled else None
                ),
                "has_summary": state.get("context_summary") is not None,
                "overflow": True,
            }
        )
        if not checkpoint:
            return False
        state["context_summary"] = checkpoint.get("summary")
        state["context_summary_position"] = checkpoint.get("position")
        state["context_summary_layers"] = checkpoint.get("layers")
        return True

    @staticmethod
    def _is_provider_overflow(exc: Exception) -> bool:
        """Detect context-overflow errors from any provider by message text."""
        text = str(exc).lower()
        markers = (
            "maximum context length",
            "context length exceeded",
            "context window",
            "prompt is too long",
            "input is too long",
            "context_length_exceeded",
            "maximum tokens",
            "token limit exceeded",
        )
        return any(m in text for m in markers)

    async def _generate_response_streaming(
        self, llm: ResilientLLMClient, messages: list[LLMMessage]
    ) -> LLMResponse:
        """Generate via stream_chat, forwarding token deltas."""
        stream = await llm.stream_chat(messages)
        parts: list[str] = []
        usage: dict[str, int] = {}
        async for chunk in stream:
            delta = self._extract_stream_delta(llm.adapter.provider_type, chunk)
            if delta:
                parts.append(delta)
                await self.stream_callback(delta)
            chunk_usage = _extract_usage(llm.adapter.provider_type, chunk)
            if chunk_usage:
                usage = {
                    k: usage.get(k, 0) + v for k, v in chunk_usage.items()
                }
        content = "".join(parts)
        return LLMResponse(
            content=content,
            model=getattr(llm.adapter.config, "model", ""),
            usage=usage,
        )

    async def _record_usage(
        self,
        state: AgentState,
        llm: ResilientLLMClient,
        response: LLMResponse,
    ) -> None:
        """Emit per-turn token usage (Arch 10, P0-10) via usage_callback."""
        import inspect

        if self.usage_callback is None:
            return
        usage = response.usage or {}
        provider = llm.adapter.provider_type.value
        model = getattr(llm.adapter.config, "model", "")
        record = {
            "tenant_id": str(state["tenant_id"]),
            "conversation_id": state.get("context", {}).get("conversation_id"),
            "session_id": state.get("session_id"),
            "provider": provider,
            "model": model,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "reasoning_tokens": usage.get("reasoning_tokens", 0),
            "cached_tokens": usage.get("cached_tokens", 0),
            # P2-2: price-card estimate (0.0 when the model has no card;
            # spend_events.usd is NOT NULL).
            "usd": self.model_catalog.estimate_cost(
                provider,
                model,
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
            )
            or 0.0,
        }
        try:
            outcome = self.usage_callback(record)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("usage_emit_failed", error=str(e))
        # P2-6: per-tenant prompt-cache hit-rate metric (provider-reported
        # cached tokens; failures must never affect the turn).
        try:
            from backend.app.context.metrics import get_prompt_cache_metrics

            get_prompt_cache_metrics().record(
                str(state["tenant_id"]),
                provider,
                usage.get("input_tokens", 0),
                usage.get("cached_tokens", 0),
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("prompt_cache_metric_failed", error=str(e))

    @staticmethod
    def _extract_stream_delta(provider_type: LLMProviderType, chunk: Any) -> str:
        """Extract the incremental text from a provider stream chunk."""
        try:
            if provider_type in (LLMProviderType.OPENAI, LLMProviderType.AZURE, LLMProviderType.CUSTOM):
                choice = chunk.choices[0]
                delta = getattr(choice, "delta", None) or choice.message
                return delta.content or ""
            if provider_type == LLMProviderType.ANTHROPIC:
                content_block = chunk.delta
                if content_block and getattr(content_block, "type", "") == "text_delta":
                    return content_block.text or ""
                return ""
            if provider_type == LLMProviderType.GOOGLE:
                candidates = chunk.candidates or []
                if candidates and candidates[0].content.parts:
                    return candidates[0].content.parts[0].text or ""
                return ""
        except (AttributeError, IndexError, TypeError):
            return ""
        return ""

    def _validate_output(self, state: AgentState) -> AgentState:
        """Validate the generated output."""
        logger.info("validating_output", tenant_id=state["tenant_id"])

        if not state["model_response"]:
            state["validation_result"] = {"valid": False, "reason": "empty_response"}
            return state

        # Grounding: when context was retrieved, check the answer is faithful
        # to it (lexical-overlap heuristic; see modules/grounding).
        retrieved = state.get("retrieved_docs") or []
        if retrieved:
            from backend.app.modules.grounding import FaithfulnessChecker

            check = FaithfulnessChecker().check(state["model_response"], retrieved)
            state["context"]["faithfulness"] = {
                "score": check.score,
                "grounded": check.grounded,
                "source_count": check.source_count,
                "matches": check.matches,
                "checked": check.checked,
            }
        else:
            state["context"]["faithfulness"] = {
                "score": 1.0,
                "grounded": True,
                "source_count": 0,
                "matches": [],
                "checked": False,
            }

        # Basic validation - can be extended with Guardrails AI
        state["validation_result"] = {
            "valid": True,
            "length": len(state["model_response"]),
            "has_content": bool(state["model_response"].strip()),
        }

        return state

    def _check_policy(self, state: AgentState) -> AgentState:
        """Check policy rules for the response."""
        logger.info("checking_policy", tenant_id=state["tenant_id"])

        context = {
            "response": state["model_response"],
            "confidence": state["confidence"],
            "validation": state["validation_result"],
        }

        action, rule_details = self.policy_set.evaluate_with_results(context)
        state["policy_action"] = action
        state["context"]["policy_rules"] = rule_details

        if action == PolicyAction.REDACT:
            state["redact_attempts"] += 1
            max_redact = int(self.tenant_config.budgets.get("max_redact_iterations", 2))
            if state["redact_attempts"] >= max_redact:
                state["budget_exceeded"] = True
                logger.warning(
                    "redact_loop_guard",
                    tenant_id=state["tenant_id"],
                    max_redact_iterations=max_redact,
                )

        # Check if escalation is needed
        if state["confidence"] < self.tenant_config.escalation_threshold:
            state["handoff_required"] = True

        return state

    async def _prepare_handoff(self, state: AgentState) -> AgentState:
        """Prepare data for human handoff."""
        logger.info("preparing_handoff", tenant_id=state["tenant_id"])
        state["handoff_required"] = True

        if not self.escalation_service:
            logger.info("handoff_skipped_no_escalation_service", tenant_id=state["tenant_id"])
            return state

        reason = f"policy_action={state['policy_action'].name}"
        if state["confidence"] < self.tenant_config.escalation_threshold:
            reason = (
                f"low_confidence={state['confidence']:.2f} "
                f"(threshold={self.tenant_config.escalation_threshold})"
            )

        try:
            history_turns: list[dict[str, str]] = [
                {"role": turn.get("role", "user"), "content": turn.get("content", "")}
                for turn in (state.get("conversation_history") or [])
                if turn.get("content")
            ]
            handoff = await self.escalation_service.create_handoff(
                session_id=state["session_id"],
                user_message=state["redacted_message"],
                confidence=state["confidence"],
                reason=reason,
                policy_action=state["policy_action"],
                conversation_history=history_turns,
                model_response=state.get("model_response"),
                attempted_resolutions=[state["model_response"]] if state.get("model_response") else None,
                conversation_id=state.get("context", {}).get("conversation_id"),
            )
            state["context"]["handoff"] = {
                "success": handoff.success,
                "ticket_id": handoff.ticket_id,
                "message": handoff.message,
            }
            logger.info(
                "handoff_prepared",
                tenant_id=state["tenant_id"],
                success=handoff.success,
                ticket_id=handoff.ticket_id,
            )
        except Exception as e:
            logger.error("handoff_creation_failed", tenant_id=state["tenant_id"], error=str(e))
            state["context"]["handoff"] = {"success": False, "error": str(e)}

        return state

    def _route_after_policy(self, state: AgentState) -> str:
        """Determine next step based on policy action."""
        action = state["policy_action"]
        max_redact = int(self.tenant_config.budgets.get("max_redact_iterations", 2))

        if state.get("budget_exceeded"):
            return "allow"
        if action == PolicyAction.BLOCK or action == PolicyAction.ESCALATE:
            return "block" if action == PolicyAction.BLOCK else "escalate"
        elif action == PolicyAction.REDACT:
            # Avoid infinite regeneration loops when a redact policy keeps matching
            if state.get("redact_attempts", 0) >= max_redact:
                return "allow"
            return "redact"
        elif state.get("handoff_required"):
            # Low confidence responses route through the escalation path
            return "escalate"
        else:
            return "allow"

    def _build_system_prompt(self, state: AgentState) -> str:
        """Build the system prompt: a stable per-tenant prefix only.

        P2-1: history, summary and knowledge are separate blocks emitted by
        the SessionContextLoader (Arch 8.1). Keeping this prefix free of
        per-turn content makes it byte-identical across turns, which is the
        precondition for provider prompt caching (P2-6).
        """
        allowed_topics = ", ".join(self.tenant_config.allowed_topics) or "general"
        blocked_topics = ", ".join(self.tenant_config.blocked_topics) or "none"

        return f"""You are a helpful assistant configured for a specific tenant.

Tenant Configuration:
- Allowed topics: {allowed_topics}
- Blocked topics: {blocked_topics}
- Default provider: {self.tenant_config.default_provider}
- Default model: {self.tenant_config.default_model}

Guidelines:
1. Stay within the allowed topics
2. If asked about blocked topics, politely decline
3. If you're unsure about something, acknowledge uncertainty
4. Do not make up information
5. Base your answer on the provided context when available
6. Be consistent with the prior conversation in this session
"""

    def _estimate_confidence(self, response: Any) -> float:
        """Estimate confidence in the response."""
        # Simple heuristic - can be improved with a classifier
        if not response.content:
            return 0.0

        # Check for uncertainty markers
        uncertainty_phrases = [
            "i'm not sure",
            "i don't know",
            "i cannot",
            "i can't",
            "unable to",
        ]

        content_lower = response.content.lower()
        if any(phrase in content_lower for phrase in uncertainty_phrases):
            return 0.3

        # Check response length as a proxy for completeness
        if len(response.content) < 20:
            return 0.5

        return 0.85

    async def _emit_decision_evidence(
        self,
        state: AgentState,
        started_at: float,
    ) -> None:
        """Emit a policy-decision evidence packet (ISO-42001 A.9, EU-AI-Act Art.12).

        Called on every processed message, not just blocked ones, so the audit
        trail covers ALLOW/ESCALATE/REDACT/BLOCK decisions alike. Mirrors the
        guardrails ``_emit_evidence`` contract: callback may be sync or
        awaitable; failures are logged, never raised.
        """
        if self.evidence_callback is None:
            return

        import hashlib
        import inspect
        import time

        elapsed_ms = (time.monotonic() - started_at) * 1000.0
        action = state["policy_action"]
        record = {
            "tenant_id": str(state["tenant_id"]),
            "conversation_id": None,
            "session_id": state["session_id"],
            "direction": "policy",
            "decision": action.name,
            "allowed": action != PolicyAction.BLOCK,
            "input_hash": hashlib.sha256(
                state["redacted_message"].encode("utf-8")
            ).hexdigest(),
            "violations": state.get("context", {}).get("policy_rules", []),
            "layers_evaluated": ["policy", "confidence"],
            "processing_time_ms": round(elapsed_ms, 3),
            "metadata": {
                "confidence": state["confidence"],
                "escalation_threshold": self.tenant_config.escalation_threshold,
                "handoff_required": bool(state["handoff_required"]),
                "redact_attempts": state["redact_attempts"],
                "has_response": state["model_response"] is not None,
            },
        }
        try:
            outcome = self.evidence_callback(record)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("policy_evidence_emit_failed", error=str(e))

    async def process_message(
        self,
        user_message: str,
        redacted_message: str,
        session_id: str = "",
        conversation_history: list[dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
        context_summary: str | None = None,
        context_summary_position: int | None = None,
        context_summary_layers: list[dict[str, Any]] | None = None,
    ) -> AgentState:
        """Process a user message through the full workflow.

        Session memory is supplied via `conversation_history` (prior
        user/assistant turns, oldest first, redacted content only) and
        `context_summary` + `context_summary_position` (immutable
        compaction checkpoint, Arch 8.2). `context_summary_layers` (P2-6)
        carries the byte-stable summary layers; when absent the legacy
        `context_summary` string renders as a single layer. A LangGraph
        checkpointer can be added later for graph-level persistence;
        DB-backed history keeps the semantics explicit and survives
        restarts.
        """
        if self.graph is None:
            raise RuntimeError("Graph not initialized")

        import time

        started_at = time.monotonic()

        initial_state = AgentState(
            tenant_id=self.tenant_config.id,
            session_id=session_id,
            user_message=user_message,
            redacted_message=redacted_message,
            context=context or {},
            conversation_history=conversation_history or [],
            retrieved_docs=[],
            model_response=None,
            validation_result={},
            policy_action=PolicyAction.ALLOW,
            confidence=0.0,
            handoff_required=False,
            redact_attempts=0,
            budget_exceeded=False,
            context_summary=context_summary,
            context_summary_position=context_summary_position,
            context_summary_layers=context_summary_layers,
            error=None,
        )

        max_steps = int(self.tenant_config.budgets.get("max_graph_steps", 50))
        result = await self.graph.ainvoke(
            initial_state,
            config={"recursion_limit": max_steps},
        )
        await self._emit_decision_evidence(result, started_at)
        return result

    async def stream_message(
        self,
        user_message: str,
        redacted_message: str,
        session_id: str = "",
        conversation_history: list[dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
        context_summary: str | None = None,
        context_summary_position: int | None = None,
        context_summary_layers: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Process a message with token streaming.

        Yields events: ``{"type": "delta", "content": str}`` per token and a
        final ``{"type": "result", ...}`` (or ``{"type": "error", ...}``)
        mirroring the AgentState fields of ``process_message``.
        """
        if self.graph is None:
            raise RuntimeError("Graph not initialized")

        queue: asyncio.Queue = asyncio.Queue()

        async def on_delta(chunk: str) -> None:
            await queue.put({"type": "delta", "content": chunk})

        self.stream_callback = on_delta

        async def _run() -> AgentState:
            try:
                result = await self.process_message(
                    user_message=user_message,
                    redacted_message=redacted_message,
                    session_id=session_id,
                    conversation_history=conversation_history,
                    context=context,
                    context_summary=context_summary,
                    context_summary_position=context_summary_position,
                    context_summary_layers=context_summary_layers,
                )
            except Exception as e:
                result = AgentState(
                    tenant_id=self.tenant_config.id,
                    session_id=session_id,
                    user_message=user_message,
                    redacted_message=redacted_message,
                    context={},
                    conversation_history=conversation_history or [],
                    retrieved_docs=[],
                    citations=[],
                    model_response=None,
                    validation_result={},
                    policy_action=PolicyAction.ALLOW,
                    confidence=0.0,
                    handoff_required=False,
                    redact_attempts=0,
                    budget_exceeded=False,
                    context_summary=context_summary,
                    context_summary_position=context_summary_position,
                    context_summary_layers=context_summary_layers,
                    error=str(e),
                )
            # Sentinel: every delta is put before the graph returns, so this
            # ordering guarantees no event is lost.
            await queue.put(None)
            return result

        task = asyncio.create_task(_run())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            result = await task

        if result.get("error"):
            yield {"type": "error", "error": result["error"]}
        else:
            yield {
                "type": "result",
                "response": result.get("model_response") or "",
                "confidence": result.get("confidence", 0.0),
                "policy_action": (
                    result.get("policy_action").name if result.get("policy_action") else None
                ),
                "handoff_required": bool(result.get("handoff_required", False)),
                "budget_exceeded": bool(result.get("budget_exceeded", False)),
                "citations": result.get("citations") or [],
                "faithfulness": result.get("context", {}).get("faithfulness"),
            }


def create_orchestration_service(
    tenant_config: TenantConfig,
    policy_set: PolicySet,
    llm_api_key: str,
    retrieval_service: Any | None = None,
    escalation_service: Any | None = None,
    model_override: str | None = None,
    context_loader: SessionContextLoader | None = None,
    model_catalog: ModelCatalog | None = None,
    compaction_callback: CompactionHook | None = None,
    compaction_config: CompactionConfig | None = None,
) -> OrchestrationService:
    """Factory function to create orchestration service."""
    return OrchestrationService(
        tenant_config,
        policy_set,
        llm_api_key,
        retrieval_service=retrieval_service,
        escalation_service=escalation_service,
        model_override=model_override,
        context_loader=context_loader,
        model_catalog=model_catalog,
        compaction_callback=compaction_callback,
        compaction_config=compaction_config,
    )
