"""
P9-3 tests: helpdesk channel adapters (Zendesk / Jira / ServiceNow).

- each adapter translates the normalized payload and parses the vendor
  response via httpx MockTransport (no network)
- HTTP/JSON errors become TicketResult(ok=False) -- never exceptions
- registry: config gating (unconfigured -> None, unknown -> None)
- EscalationService dispatch: concrete adapter used when configured,
  generic webhook untouched
"""

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from backend.app.adapters.ticketing import (
    TicketingConfig,
    get_ticketing_adapter,
)
from backend.app.adapters.ticketing.jira import JiraAdapter
from backend.app.adapters.ticketing.servicenow import ServiceNowAdapter
from backend.app.adapters.ticketing.zendesk import ZendeskAdapter
from backend.app.domain.tenant import TenantConfig
from backend.app.modules.escalation import create_escalation_service
from backend.app.modules.escalation.models import HandoffRequest


def _payload():
    return {
        "subject": "Neryva Escalation - Confidence: 0.20",
        "description": "**Escalation Reason:** low confidence",
        "priority": "high",
        "tags": ["neryva", "escalation", "t1"],
    }


class TestZendeskAdapter:
    @pytest.mark.asyncio
    async def test_creates_ticket(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            captured["auth"] = request.headers.get("authorization", "")
            return httpx.Response(
                201,
                json={
                    "ticket": {
                        "id": 12345,
                        "url": "https://acme.zendesk.com/api/v2/tickets/12345.json",
                    }
                },
            )

        adapter = ZendeskAdapter(
            base_url="https://acme.zendesk.com",
            email="ops@neryva.example",
            api_token="tok",
            transport=httpx.MockTransport(handler),
        )
        result = await adapter.create_ticket(_payload())
        assert result.ok and result.ticket_id == "12345"
        assert result.url is not None
        assert "Basic" in captured["auth"]
        assert captured["body"]["ticket"]["subject"].startswith("Neryva Escalation")
        assert captured["body"]["ticket"]["priority"] == "urgent"
        await adapter.close()

    @pytest.mark.asyncio
    async def test_http_error_becomes_result(self):
        adapter = ZendeskAdapter(
            base_url="https://acme.zendesk.com",
            email="ops@neryva.example",
            api_token="tok",
            transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad token")),
        )
        result = await adapter.create_ticket(_payload())
        assert result.ok is False
        assert "401" in result.error


class TestJiraAdapter:
    @pytest.mark.asyncio
    async def test_creates_issue(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            captured["auth"] = request.headers.get("authorization", "")
            return httpx.Response(201, json={"id": "10001", "key": "SUP-7"})

        adapter = JiraAdapter(
            base_url="https://neryva.atlassian.net",
            api_token="tok",
            project_key="SUP",
            transport=httpx.MockTransport(handler),
        )
        result = await adapter.create_ticket(_payload())
        assert result.ok and result.ticket_id == "SUP-7"
        assert result.url == "https://neryva.atlassian.net/browse/SUP-7"
        assert captured["auth"] == "Bearer tok"
        assert captured["body"]["fields"]["project"] == {"key": "SUP"}
        assert captured["body"]["fields"]["priority"] == {"name": "Highest"}
        await adapter.close()

    @pytest.mark.asyncio
    async def test_error_becomes_result(self):
        adapter = JiraAdapter(
            base_url="https://neryva.atlassian.net",
            api_token="tok",
            project_key="SUP",
            transport=httpx.MockTransport(lambda r: httpx.Response(400, text="bad project")),
        )
        result = await adapter.create_ticket(_payload())
        assert result.ok is False


class TestServiceNowAdapter:
    @pytest.mark.asyncio
    async def test_creates_incident(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                201,
                json={"result": {"sys_id": "sys-1", "number": "INC001"}},
            )

        adapter = ServiceNowAdapter(
            base_url="https://neryva.service-now.com",
            username="integration",
            password="pw",
            transport=httpx.MockTransport(handler),
        )
        result = await adapter.create_ticket(_payload())
        assert result.ok and result.ticket_id == "sys-1"
        assert captured["body"]["priority"] == "1"
        await adapter.close()

    @pytest.mark.asyncio
    async def test_error_becomes_result(self):
        adapter = ServiceNowAdapter(
            base_url="https://neryva.service-now.com",
            username="integration",
            password="pw",
            transport=httpx.MockTransport(lambda r: httpx.Response(403, text="denied")),
        )
        result = await adapter.create_ticket(_payload())
        assert result.ok is False
        assert "403" in result.error


class TestRegistry:
    def test_config_gating(self):
        assert TicketingConfig().is_configured
        assert not TicketingConfig(adapter="zendesk").is_configured
        assert TicketingConfig(
            adapter="zendesk", zendesk_url="u", zendesk_email="e", zendesk_api_token="t"
        ).is_configured
        assert not TicketingConfig(adapter="weird").is_configured

    def test_get_adapter_none_when_unconfigured_or_unknown(self):
        assert get_ticketing_adapter(TicketingConfig(adapter="zendesk")) is None
        assert get_ticketing_adapter(TicketingConfig(adapter="unknown")) is None
        assert get_ticketing_adapter(TicketingConfig()) is None

    def test_get_adapter_instantiates(self):
        adapter = get_ticketing_adapter(
            TicketingConfig(
                adapter="jira",
                jira_url="https://x.atlassian.net",
                jira_api_token="t",
                jira_project_key="SUP",
            )
        )
        assert adapter is not None and adapter.name == "jira"


class TestEscalationDispatch:
    @pytest.mark.asyncio
    async def test_helpdesk_adapter_used_when_configured(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={"key": "SUP-9"})

        tenant = TenantConfig(id=uuid4(), name="t", slug="t")
        service = create_escalation_service(
            tenant,
            ticketing_config=TicketingConfig(
                adapter="jira",
                jira_url="https://x.atlassian.net",
                jira_api_token="t",
                jira_project_key="SUP",
            ),
            ticketing_transport=httpx.MockTransport(handler),
        )
        handoff = HandoffRequest(tenant_id=tenant.id, user_message="hi", confidence=0.2, reason="low")
        response = await service._send_to_ticketing_system(handoff)
        assert response.success and response.ticket_id == "SUP-9"
        assert captured["body"]["fields"]["summary"].startswith("Neryva Escalation")

    @pytest.mark.asyncio
    async def test_unconfigured_helpdesk_falls_back_to_local(self):
        tenant = TenantConfig(id=uuid4(), name="t", slug="t")
        service = create_escalation_service(
            tenant,
            ticketing_config=TicketingConfig(adapter="servicenow"),  # missing creds
        )
        handoff = HandoffRequest(tenant_id=tenant.id, user_message="hi", confidence=0.2, reason="low")
        response = await service._send_to_ticketing_system(handoff)
        assert response.success is True
        assert response.ticket_id is None

    @pytest.mark.asyncio
    async def test_generic_keeps_webhook_transport(self):
        tenant = TenantConfig(id=uuid4(), name="t", slug="t")
        service = create_escalation_service(
            tenant,
            ticketing_webhook_url="https://hooks.example/raise",
            ticketing_config=TicketingConfig(),  # generic
        )
        handoff = HandoffRequest(tenant_id=tenant.id, user_message="hi", confidence=0.2, reason="low")
        with pytest.raises(Exception):
            # no aiohttp server: transport error propagates (generic path
            # unchanged: failure -> exception -> caller records locally)
            await service._send_to_ticketing_system(handoff)

    @pytest.mark.asyncio
    async def test_adapter_error_is_caught_by_create_handoff(self):
        tenant = TenantConfig(id=uuid4(), name="t", slug="t")
        service = create_escalation_service(
            tenant,
            ticketing_config=TicketingConfig(
                adapter="zendesk",
                zendesk_url="https://acme.zendesk.com",
                zendesk_email="e",
                zendesk_api_token="t",
            ),
            ticketing_transport=httpx.MockTransport(lambda r: httpx.Response(401, text="no")),
        )
        handoff = HandoffRequest(tenant_id=tenant.id, user_message="hi", confidence=0.2, reason="low")
        response = await service.create_handoff(
            session_id="s1",
            user_message="hi",
            confidence=0.2,
            reason="low",
            policy_action=SimpleNamespace(name="ESCALATE"),
            conversation_history=[],
        )
        assert response.success is False
        assert "Ticketing API error" in response.message
