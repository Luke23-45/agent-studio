"""
Tests for handoff service.
"""

import pytest
from uuid import uuid4


class TestHandoffService:
    """Tests for HandoffService."""

    @pytest.mark.asyncio
    async def test_create_handoff(self):
        """Test creating a handoff request."""
        from backend.app.application.handoff import create_handoff_service

        service = create_handoff_service(
            ticketing_webhook_url="http://test-webhook.com",
        )

        response = await service.create_handoff(
            tenant_id=uuid4(),
            conversation_id="conv-123",
            user_message="I need human help",
            model_response="I'm not sure how to help with that.",
            confidence=0.3,
            reason="low_confidence",
        )

        assert response.success is True
        assert response.ticket_id is not None or response.metadata.get("fallback")

    def test_get_handoff_status(self):
        """Test getting handoff status."""
        from backend.app.application.handoff import create_handoff_service

        service = create_handoff_service()
        
        # Create a handoff first (mocked)
        conversation_id = "conv-test-456"
        
        # Initially should be None
        status = service.get_handoff_status(conversation_id)
        assert status is None
