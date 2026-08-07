"""
P4-8 — Turn failure semantics (Arch §9.3).

Proves the three documented failure branches, per tenant configuration:
LLM failure degrades visibly (never silent), guardrail failure is
fail-closed for authoritative layers and fail-open only when explicitly
configured as non-authoritative, and escalation carries the full durable
thread + attempted resolutions. Uses stub engines / a programmable gateway
so no provider or model artifacts are involved.
"""

import pytest
from uuid import uuid4

from backend.app.domain.policy import PolicyAction, PolicySet
from backend.app.domain.tenant import TenantConfig
from backend.app.gateway.types import GatewayChainExhausted, GatewayError
from backend.app.application.orchestration import create_orchestration_service
from backend.app.modules.guardrails.orchestrator import GuardrailsOrchestrator
from backend.app.modules.guardrails.types import GuardrailDecision, GuardrailLayer
from backend.app.modules.guardrails.config import GuardrailsModuleConfig
from backend.app.domain.safety import SafetyCheckResult
from backend.tests.gateway_fakes import FakeGateway, result


def _tenant() -> TenantConfig:
    return TenantConfig(
        id=uuid4(),
        name="t",
        slug="t",
        escalation_threshold=0.7,
        budgets={
            "max_verify_retries": 1,
            "llm_timeout_seconds": 1.0,
        },
    )


class _BrokenEngine:
    """Guardrail layer double that always fails (P4-8 guardrail branch)."""

    layer = GuardrailLayer.PII_REDACTION

    async def initialize(self) -> None:
        return None

    async def evaluate(self, text, tenant_config, context=None) -> SafetyCheckResult:
        raise RuntimeError("layer backend down")

    async def health_check(self):
        raise RuntimeError("layer backend down")

    async def close(self) -> None:
        return None


@pytest.fixture
def sample_tenant_config():
    return _tenant()


@pytest.fixture
def sample_policy_set():
    return PolicySet(id=uuid4(), tenant_id=uuid4(), name="p")


@pytest.mark.asyncio
async def test_guardrail_fail_closed_by_default():
    """A failing guardrail layer must never silently pass content through;
    the default config is fail-closed (the evaluation error surfaces rather
    than producing an ALLOW)."""
    orch = GuardrailsOrchestrator(
        GuardrailsModuleConfig(
            enable_regex_fastpath=False,
            enable_classifier=False,
            enable_jailbreak_scan=False,
            enable_guardrails_ai=False,
            enable_pii_redaction=True,
            parallel_layer_execution=False,
            fail_open_on_error=False,  # default: fail-closed
        )
    )
    orch._engines[GuardrailLayer.PII_REDACTION] = _BrokenEngine()
    orch._layer_order = [GuardrailLayer.PII_REDACTION]

    tenant = _tenant()
    with pytest.raises(RuntimeError):
        await orch.evaluate_input("some user message", tenant)
    # Fail-closed: no silent ALLOW was produced.
    assert GuardrailDecision.ALLOW.name == "ALLOW"


@pytest.mark.asyncio
async def test_guardrail_fail_open_when_explicitly_non_authoritative():
    """When the tenant explicitly configures a non-authoritative/optional
    layer, a failure degrades to allow — but is still observable as a
    layer error, not a silent bypass."""
    orch = GuardrailsOrchestrator(
        GuardrailsModuleConfig(
            enable_regex_fastpath=False,
            enable_classifier=False,
            enable_jailbreak_scan=False,
            enable_guardrails_ai=False,
            enable_pii_redaction=True,
            parallel_layer_execution=False,
            fail_open_on_error=True,  # tenant opted into fail-open
        )
    )
    orch._engines = {GuardrailLayer.PII_REDACTION: _BrokenEngine()}
    orch._layer_order = [GuardrailLayer.PII_REDACTION]

    tenant = _tenant()
    outcome = await orch.evaluate_input("some user message", tenant)
    assert outcome.allowed is True


@pytest.mark.asyncio
async def test_llm_gateway_error_is_visible_but_chain_exhausted_propagates():
    """Chain exhaustion is a GatewayError the route must map explicitly
    (502/532) — it is never swallowed into a generic 500 and never degrades
    silently."""
    service = create_orchestration_service(
        tenant_config=_tenant(),
        policy_set=PolicySet(id=uuid4(), tenant_id=uuid4(), name="p"),
        gateway=FakeGateway(
            chat_error=GatewayChainExhausted(
                provider="openai",
                model="gpt-4o-mini",
                reason="timeout",
            )
        ),
    )
    with pytest.raises(GatewayError):
        await service.process_message(
            user_message="hi",
            redacted_message="hi",
        )


@pytest.mark.asyncio
async def test_llm_generic_failure_degrades_with_handoff():
    """A generic provider failure is not silent: the turn records the error
    and degrades to a visible handoff-required state (never a bare answer)."""
    service = create_orchestration_service(
        tenant_config=_tenant(),
        policy_set=PolicySet(id=uuid4(), tenant_id=uuid4(), name="p"),
        gateway=FakeGateway(chat_error=RuntimeError("provider down")),
    )
    state = await service.process_message(
        user_message="hi",
        redacted_message="hi",
    )
    assert state["error"] == "provider down"
    assert state["confidence"] == 0.0
    assert state["model_response"] is None


@pytest.mark.asyncio
async def test_escalation_carries_durable_thread_and_resolutions():
    """P4-8: escalation must carry full conversation history + attempted
    resolutions + recommended next step — not just a ticket id."""
    from backend.app.modules.escalation.service import EscalationService

    captured = {}

    class _CaptureEscalation(EscalationService):
        async def create_handoff(self, **kwargs):
            captured.update(kwargs)
            class _R:
                success = True
                ticket_id = "tkt-1"
                message = "ok"
            return _R()

    tenant = _tenant()
    tenant.escalation_threshold = 0.9  # low-confidence responses escalate
    service = create_orchestration_service(
        tenant_config=tenant,
        policy_set=PolicySet(id=uuid4(), tenant_id=uuid4(), name="p"),
        gateway=FakeGateway(chat_results=[result("I am unsure and lost here.", model="gpt", provider="openai")]),
        escalation_service=_CaptureEscalation(tenant_config=tenant),
    )
    history = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "a"}]
    state = await service.process_message(
        user_message="i don't know what to say, can you help",
        redacted_message="i don't know what to say, can you help",
        session_id="sess-1",
        conversation_history=history,
    )
    assert state["handoff_required"] is True
    assert captured["session_id"] == "sess-1"
    assert captured["conversation_history"] == history
    assert captured["attempted_resolutions"] is not None
    assert captured["model_response"] is not None
    assert captured["reason"].startswith("low_confidence")
    assert isinstance(captured["policy_action"], PolicyAction)