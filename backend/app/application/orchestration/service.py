"""
Orchestration service using LangGraph.

Implements the agent decision loop: propose -> evaluate -> select -> execute.
"""

import structlog
from typing import Any
from uuid import UUID

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from typing_extensions import TypedDict

from backend.app.adapters.llm import (
    BaseLLMAdapter,
    LLMConfig,
    LLMMessage,
    LLMProviderType,
    create_llm_adapter,
)
from backend.app.domain.policy import PolicyAction, PolicySet
from backend.app.domain.tenant import TenantConfig

logger = structlog.get_logger(__name__)


class AgentState(TypedDict):
    """State passed through the LangGraph workflow."""

    tenant_id: UUID
    user_message: str
    redacted_message: str
    context: dict[str, Any]
    retrieved_docs: list[dict[str, Any]]
    model_response: str | None
    validation_result: dict[str, Any]
    policy_action: PolicyAction
    confidence: float
    handoff_required: bool
    error: str | None


class OrchestrationService:
    """Main orchestration service for agent workflows."""

    def __init__(
        self,
        tenant_config: TenantConfig,
        policy_set: PolicySet,
        llm_api_key: str,
    ):
        self.tenant_config = tenant_config
        self.policy_set = policy_set
        self.llm_api_key = llm_api_key
        self.graph: CompiledStateGraph | None = None
        self._build_graph()

    def _get_llm_adapter(self) -> BaseLLMAdapter:
        """Create LLM adapter based on tenant config."""
        provider_type = LLMProviderType(self.tenant_config.default_provider)
        config = LLMConfig(model=self.tenant_config.default_model)
        return create_llm_adapter(provider_type, self.llm_api_key, config)

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
        }
        return state

    async def _retrieve_context(self, state: AgentState) -> AgentState:
        """Retrieve relevant context from knowledge base."""
        logger.info("retrieving_context", tenant_id=state["tenant_id"])
        # This will be implemented with RAG service
        state["retrieved_docs"] = []
        return state

    async def _generate_response(self, state: AgentState) -> AgentState:
        """Generate response using the LLM."""
        logger.info("generating_response", tenant_id=state["tenant_id"])

        llm = self._get_llm_adapter()

        # Build messages with context
        system_prompt = self._build_system_prompt(state)
        messages = [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=state["redacted_message"]),
        ]

        try:
            response = await llm.chat(messages)
            state["model_response"] = response.content
            state["confidence"] = self._estimate_confidence(response)
        except Exception as e:
            logger.error("llm_error", error=str(e))
            state["error"] = str(e)
            state["confidence"] = 0.0

        return state

    def _validate_output(self, state: AgentState) -> AgentState:
        """Validate the generated output."""
        logger.info("validating_output", tenant_id=state["tenant_id"])

        if not state["model_response"]:
            state["validation_result"] = {"valid": False, "reason": "empty_response"}
            return state

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

        action = self.policy_set.evaluate(context)
        state["policy_action"] = action

        # Check if escalation is needed
        if state["confidence"] < self.tenant_config.escalation_threshold:
            state["handoff_required"] = True

        return state

    def _prepare_handoff(self, state: AgentState) -> AgentState:
        """Prepare data for human handoff."""
        logger.info("preparing_handoff", tenant_id=state["tenant_id"])
        # Handoff logic will be implemented in escalation module
        return state

    def _route_after_policy(self, state: AgentState) -> str:
        """Determine next step based on policy action."""
        action = state["policy_action"]

        if action == PolicyAction.BLOCK or action == PolicyAction.ESCALATE:
            return "block" if action == PolicyAction.BLOCK else "escalate"
        elif action == PolicyAction.REDACT:
            return "redact"
        else:
            return "allow"

    def _build_system_prompt(self, state: AgentState) -> str:
        """Build system prompt with tenant configuration."""
        allowed_topics = ", ".join(self.tenant_config.allowed_topics) or "general"
        return f"""You are a helpful assistant configured for a specific tenant.

Tenant Configuration:
- Allowed topics: {allowed_topics}
- Default provider: {self.tenant_config.default_provider}
- Default model: {self.tenant_config.default_model}

Guidelines:
1. Stay within the allowed topics
2. If asked about blocked topics, politely decline
3. If you're unsure about something, acknowledge uncertainty
4. Do not make up information

Context from knowledge base:
{state.get('retrieved_docs', [])}
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

    async def process_message(self, user_message: str, redacted_message: str) -> AgentState:
        """Process a user message through the full workflow."""
        if self.graph is None:
            raise RuntimeError("Graph not initialized")

        initial_state = AgentState(
            tenant_id=self.tenant_config.id,
            user_message=user_message,
            redacted_message=redacted_message,
            context={},
            retrieved_docs=[],
            model_response=None,
            validation_result={},
            policy_action=PolicyAction.ALLOW,
            confidence=0.0,
            handoff_required=False,
            error=None,
        )

        result = await self.graph.ainvoke(initial_state)
        return result


def create_orchestration_service(
    tenant_config: TenantConfig,
    policy_set: PolicySet,
    llm_api_key: str,
) -> OrchestrationService:
    """Factory function to create orchestration service."""
    return OrchestrationService(tenant_config, policy_set, llm_api_key)
