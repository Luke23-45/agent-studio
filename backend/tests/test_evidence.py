"""
Tests for policy-decision evidence packets (P1, audit 4.14 / ISO-42001 A.9):

- PolicySet.evaluate_with_results returns the action plus rule-level detail
- OrchestrationService emits an evidence packet on EVERY processed message
  (ALLOW included), via the injectable evidence_callback
- The callback may be sync or awaitable; failures are swallowed
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.adapters.llm import LLMProviderType
from backend.app.application.orchestration import create_orchestration_service
from backend.app.domain.policy import PolicyAction, PolicyRule, PolicySet, PolicyType
from backend.app.domain.tenant import TenantConfig
from backend.tests.gateway_fakes import FakeGateway


class _FakeAdapter:
    provider_type = LLMProviderType.OPENAI

    def __init__(self):
        self.config = SimpleNamespace(model="gpt-4")

    async def chat(self, messages):
        return SimpleNamespace(content="A complete and useful answer for the tenant.")


@pytest.fixture
def tenant():
    return TenantConfig(
        id=uuid4(),
        name="Acme",
        slug="acme",
        default_provider="openai",
        default_model="gpt-4",
        escalation_threshold=0.5,
    )


@pytest.fixture
def policy_set(tenant):
    # Deny-by-default (Arch 2.5): explicit allow rule so orchestration
    # turns end in ALLOW; block tests append higher-priority rules.
    return PolicySet(
        id=uuid4(),
        tenant_id=tenant.id,
        name="default",
        rules=[
            PolicyRule(
                name="allow-all",
                action=PolicyAction.ALLOW,
                conditions={},
                priority=1,
            )
        ],
        version=1,
    )


@pytest.fixture
def orchestration(tenant, policy_set):
    service = create_orchestration_service(
        tenant_config=tenant,
        policy_set=policy_set,
        gateway=FakeGateway(),
    )
    service.gateway = FakeGateway(chat_stub=_FakeAdapter())
    return service


class TestPolicySetEvidence:
    def test_no_matching_rules_denies_by_default(self):
        empty = PolicySet(tenant_id=uuid4(), name="default", rules=[], version=1)
        action, details = empty.evaluate_with_results({})
        assert action == PolicyAction.BLOCK
        assert details[0]["name"] == "deny-by-default"

    def test_matching_rule_details_are_reported(self):
        policy_set = PolicySet(tenant_id=uuid4(), name="default", version=1)
        rule = PolicyRule(
            name="block-low-confidence",
            policy_type=PolicyType.OUTPUT_VALIDATION,
            action=PolicyAction.ESCALATE,
            conditions={"confidence": 0.1},
            priority=5,
        )
        policy_set.rules.append(rule)

        action, details = policy_set.evaluate_with_results({"confidence": 0.1})
        assert action == PolicyAction.ESCALATE
        assert len(details) == 1
        d = details[0]
        assert d["rule_id"] == str(rule.id)
        assert d["name"] == "block-low-confidence"
        assert d["action"] == "escalate"
        assert d["policy_type"] == "output_validation"
        assert d["priority"] == 5


class TestDecisionEvidence:
    async def test_emits_packet_for_allow(self, orchestration):
        records = []
        orchestration.evidence_callback = records.append

        await orchestration.process_message(
            user_message="hi", redacted_message="hi", session_id="s1"
        )

        assert len(records) == 1
        record = records[0]
        assert record["direction"] == "policy"
        assert record["decision"] == "ALLOW"
        assert record["allowed"] is True
        assert record["session_id"] == "s1"
        assert len(record["input_hash"]) == 64
        assert "policy" in record["layers_evaluated"]
        assert "confidence" in record["layers_evaluated"]
        assert record["processing_time_ms"] >= 0.0
        assert record["metadata"]["confidence"] > 0.0

    async def test_emits_packet_for_block(self, tenant, orchestration):
        rule = PolicyRule(
            name="block-all",
            policy_type=PolicyType.OUTPUT_VALIDATION,
            action=PolicyAction.BLOCK,
            conditions={},
            priority=10,
        )
        orchestration.policy_set.rules.append(rule)

        records = []
        orchestration.evidence_callback = records.append

        await orchestration.process_message(
            user_message="hi", redacted_message="hi", session_id="s2"
        )

        assert len(records) == 1
        record = records[0]
        assert record["decision"] == "BLOCK"
        assert record["allowed"] is False
        assert record["violations"][0]["name"] == "block-all"

    async def test_supports_awaitable_callback(self, orchestration):
        records = []

        async def async_callback(record):
            records.append(record)

        orchestration.evidence_callback = async_callback

        await orchestration.process_message(
            user_message="hi", redacted_message="hi", session_id="s3"
        )

        assert len(records) == 1

    async def test_failing_callback_is_swallowed(self, orchestration):
        def failing_callback(record):
            raise RuntimeError("db down")

        orchestration.evidence_callback = failing_callback

        result = await orchestration.process_message(
            user_message="hi", redacted_message="hi", session_id="s4"
        )

        assert result["model_response"] is not None

    async def test_no_callback_means_no_evidence(self, orchestration):
        assert orchestration.evidence_callback is None
        result = await orchestration.process_message(
            user_message="hi", redacted_message="hi", session_id="s5"
        )
        assert result["model_response"] is not None
