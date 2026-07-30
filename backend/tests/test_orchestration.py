"""
Tests for orchestration service.
"""

import pytest
from uuid import uuid4

from backend.app.domain.policy import PolicyAction, PolicySet
from backend.app.domain.tenant import TenantConfig


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
            llm_api_key="test-key",
        )

        result = await service.process_message(
            user_message="Hello, I need help with my billing.",
            redacted_message="Hello, I need help with my billing.",
        )

        assert result is not None
        assert "error" not in result or result["error"] is None

    def test_confidence_estimation(self, sample_tenant_config, sample_policy_set):
        """Test confidence estimation logic."""
        from backend.app.application.orchestration import create_orchestration_service

        service = create_orchestration_service(
            tenant_config=sample_tenant_config,
            policy_set=sample_policy_set,
            llm_api_key="test-key",
        )

        # Test uncertainty detection
        from unittest.mock import Mock
        response_uncertain = Mock(content="I'm not sure about that.")
        response_confident = Mock(content="The answer is 42.")
        response_short = Mock(content="Yes.")

        assert service._estimate_confidence(response_uncertain) < 0.5
        assert service._estimate_confidence(response_confident) > 0.7
        assert service._estimate_confidence(response_short) < 0.7
