"""
P4-9 — Non-streaming / streaming output-validation parity (Arch 9.1–9.3).

The non-streaming path must apply the same full-output gate the streaming
path gets from its rolling moderation window, wired into the existing
bounded verify loop:

- a PII redaction passes and t) released text is the redacted one while the
  raw model output is preserved for the durable log;
- a rejected response is re-asked ``max_verify_retries`` times (bounded),
  then escalates to handoff — never releasing the rejected bytes;
- a crashing validator fails open (matches the window behaviour) so a
  guardrail backend problem can never take the turn hostage;
- when no validator is configured the P2-9 verify loop is unchanged.

These are orchestration-level assertions; the route layer wires a real
``evaluate_output`` closure only when ``ENABLE_PRESIDIO``.
"""

import pytest
from uuid import uuid4

from backend.app.domain.policy import PolicyAction, PolicyRule, PolicySet, PolicyType
from backend.app.domain.tenant import TenantConfig
from backend.app.application.orchestration import create_orchestration_service
from backend.app.modules.guardrails.models import GuardrailEvaluationResult
from backend.app.modules.guardrails.types import GuardrailDecision
from backend.tests.gateway_fakes import FakeGateway, result


def _tenant(max_verify_retries: int = 2, escalation_threshold: float = 0.7) -> TenantConfig:
    return TenantConfig(
        id=uuid4(),
        name="t",
        slug="t",
        escalation_threshold=escalation_threshold,
        budgets={
            "max_verify_retries": max_verify_retries,
            "llm_timeout_seconds": 1.0,
        },
    )


def _policy_set(tenant_id: UUID) -> PolicySet:
    # Allow-by-default for the test turn (production tenants carry a permit),
    # so policy routing does not pre-empt the output-validation assertions.
    return PolicySet(
        id=uuid4(),
        tenant_id=tenant_id,
        name="p",
        rules=[
            PolicyRule(
                name="allow-general",
                policy_type=PolicyType.TOPIC_FILTER,
                action=PolicyAction.ALLOW,
                priority=10,
            )
        ],
    )


def _service(**kwargs):
    tenant = kwargs.pop("tenant_config", _tenant())
    return create_orchestration_service(
        tenant_config=tenant,
        policy_set=_policy_set(tenant.id),
        **kwargs,
    )


async def _run_governed(service, first="hi", second="hi"):
    return await service.process_message(
        user_message=first,
        redacted_message=second,
        session_id="sess-1",
        conversation_history=[],
    )


@pytest.mark.asyncio
async def test_parity_redaction_passes_through_and_keeps_raw_output():
    """A redaction outcome is ALLOW-like: the released text is the redacted
    one and the raw model output stays visible for the durable log."""
    raw = "Please contact acct-88112 at lease-helping; thanks."
    redacted = "Please contact [REDACTED] at lease-helping; thanks."

    async def validator(text):
        return GuardrailEvaluationResult(
            allowed=True,
            decision=GuardrailDecision.REDACT,
            redacted_text=redacted,
        )

    service = _service(
        gateway=FakeGateway(chat_results=[result(raw)]),
        output_validator=validator,
        tenant_config=_tenant(),
    )
    state = await _run_governed(service)

    assert state["model_response"] == redacted
    assert state["context"]["output_raw"] == raw
    assert state["context"]["output_validation"]["checked"] is True
    assert state["context"]["output_validation"]["allowed"] is True
    assert state["context"]["output_validation"]["decision"] == "REDACT"
    assert state["verify_retries"] == 0


@pytest.mark.asyncio
async def test_rejected_output_retries_bounded_then_escalates():
    """A response the validator keeps rejecting is re-asked at most
    ``max_verify_retries`` times, then escalates — the rejected content is
    never returned to the client."""
    attempts = {"calls": 0}

    async def blocking_validator(text):
        attempts["calls"] += 1
        return GuardrailEvaluationResult(
            allowed=False,
            decision=GuardrailDecision.REASK,
        )

    tenant = _tenant(max_verify_retries=2)
    gateway = FakeGateway(
        chat_results=[
            result("attempt-one", model="gpt", provider="openai"),
            result("attempt-two", model="gpt", provider="openai"),
        ]
    )
    service = _service(
        gateway=gateway,
        output_validator=blocking_validator,
        tenant_config=tenant,
    )
    state = await _run_governed(service)

    # generate_response ran initial + one corrective re-ask, then the
    # (bounded) retry budget was exhausted on the second validation.
    assert gateway.generate_calls == 2
    assert attempts["calls"] == 2
    assert state["verify_retries"] == tenant.budgets["max_verify_retries"]
    assert state["verify_exhausted"] is True
    assert state["handoff_required"] is True
    assert state["context"]["output_validation"] and state["context"]["output_validation"]["checked"]
    assert not state["context"]["output_validation"]["allowed"]


@pytest.mark.asyncio
async def test_exhausted_out_of_reach_response_escalation_marker():
    """When the re-ask budget is exhausted the final released text is
    withheld (compliance) and escalation is flagged; the rejected bytes are
    not returned to the client."""
    async def blocking_validator(text):
        return GuardrailEvaluationResult(
            allowed=False,
            decision=GuardrailDecision.BLOCK,
        )

    tenant = _tenant(max_verify_retries=1)
    service = _service(
        gateway=FakeGateway(chat_results=[result("harmful-ish attempt", model="gpt", provider="openai")]),
        output_validator=blocking_validator,
        tenant_config=tenant,
    )
    state = await _run_governed(service)

    assert state["verify_exhausted"] is True
    assert state["handoff_required"] is True
    assert state["context"]["output_validation"]["decision"] == "BLOCK"


@pytest.mark.asyncio
async def test_validator_crash_fails_open_keeps_response():
    """A crashing output validator never takes the turn down: the response
    stays, and no re-ask / escalation happens (fail-open, matching the
    streaming window's validator-exception path)."""
    calls = {"n": 0}

    async def crashing_validator(text):
        calls["n"] += 1
        raise RuntimeError("validator backend down")

    service = _service(
        gateway=FakeGateway(chat_results=[result("fine answer", model="gpt", provider="openai")]),
        output_validator=crashing_validator,
        tenant_config=_tenant(escalation_threshold=0.0),
    )
    state = await _run_governed(service)

    assert calls["n"] == 1
    assert state["model_response"] == "fine answer"
    assert state["handoff_required"] is False
    assert state["verify_exhausted"] is False
    assert state["verify_retries"] == 0


@pytest.mark.asyncio
async def test_no_validator_leaves_verify_loop_unchanged():
    """Without an output validator (flag off) the P2-9 loop is untouched:
    valid responses pass cleanly."""
    service = _service(
        gateway=FakeGateway(chat_results=[result("plain answer", model="gpt", provider="openai")]),
        output_validator=None,
        tenant_config=_tenant(escalation_threshold=0.0),
    )
    state = await _run_governed(service)

    assert state["model_response"] == "plain answer"
    assert state["verify_retries"] == 0
    assert state["handoff_required"] is False