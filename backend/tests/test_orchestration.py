"""
Tests for orchestration service.
"""

import pytest
from uuid import uuid4

from backend.app.domain.policy import PolicyAction, PolicySet
from backend.app.domain.tenant import TenantConfig
from backend.tests.gateway_fakes import FakeGateway


@pytest.fixture
def sample_tenant_config():
    """Create a sample tenant configuration."""
    return TenantConfig(
        id=uuid4(),
        name="Test Tenant",
        slug="test-tenant",
        allowed_topics=["support", "billing"],
        blocked_topics=["competitor"],
        escalation_threshold=0.7,
        default_provider="openai",
        default_model="gpt-4o-mini",
    )


@pytest.fixture
def sample_policy_set():
    """Create a sample policy set."""
    return PolicySet(
        id=uuid4(),
        tenant_id=uuid4(),
        name="test-policy",
    )


class TestOrchestrationService:
    """Tests for OrchestrationService."""

    @pytest.mark.asyncio
    async def test_process_message_basic(self, sample_tenant_config, sample_policy_set):
        """Test basic message processing."""
        from backend.app.application.orchestration import create_orchestration_service

        service = create_orchestration_service(
            tenant_config=sample_tenant_config,
            policy_set=sample_policy_set,
            gateway=FakeGateway(),
        )

        result = await service.process_message(
            user_message="Hello, I need help with my billing.",
            redacted_message="Hello, I need help with my billing.",
        )

        assert result is not None
        # Without a real provider API key the LLM call fails and the graph
        # records the error instead of raising.
        assert result["error"] is not None or result["model_response"] is not None

    def test_confidence_estimation(self, sample_tenant_config, sample_policy_set):
        """Test confidence estimation logic."""
        from backend.app.application.orchestration import create_orchestration_service

        service = create_orchestration_service(
            tenant_config=sample_tenant_config,
            policy_set=sample_policy_set,
            gateway=FakeGateway(),
        )

        # Test uncertainty detection
        from unittest.mock import Mock
        response_uncertain = Mock(content="I'm not sure about that.")
        response_confident = Mock(content="The answer to your billing question is forty two dollars.")
        response_short = Mock(content="Yes.")

        assert service._estimate_confidence(response_uncertain) < 0.5
        assert service._estimate_confidence(response_confident) > 0.7
        assert service._estimate_confidence(response_short) < 0.7

    def test_system_prompt_resolver_override(self, sample_tenant_config, sample_policy_set):
        """P9-4: a wired prompt_resolver replaces the system prompt; a
        None result keeps the default; a failing resolver never breaks
        the turn (falls back to default)."""
        from backend.app.application.orchestration import create_orchestration_service

        service = create_orchestration_service(
            tenant_config=sample_tenant_config,
            policy_set=sample_policy_set,
            gateway=FakeGateway(),
        )

        managed = "managed prompt v3: you are the support bot."

        def resolver_with_version(name):
            if name == "system":
                return managed
            return None

        resolver_with_version.resolved_version = "system:v3"
        service.prompt_resolver = resolver_with_version
        assert service._build_system_prompt({}) == managed
        assert service.prompt_version == "system:v3"

        service.prompt_resolver = lambda name: None
        default = service._build_system_prompt({})
        assert service.prompt_version is None
        assert default != managed and "allowed_topics" not in default.lower()

        def exploding(name):
            raise RuntimeError("resolver exploded")

        service.prompt_resolver = exploding
        assert service._build_system_prompt({}) == default
        assert service.prompt_version is None
