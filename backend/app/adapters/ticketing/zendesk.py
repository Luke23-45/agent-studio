"""Zendesk ticketing adapter (P9-3, feature-matrix 8.2).

POSTs the normalized handoff payload to the Zendesk Support API
(``/api/v2/tickets.json``). Auth: basic with ``{email}/token`` as the
username and the API token as the password (Zendesk's standard pattern
for API tokens).
"""

from __future__ import annotations

import base64
import structlog
from typing import Any

import httpx

from .base import TicketResult, TicketingAdapter

logger = structlog.get_logger(__name__)

_MAP = {"high": "urgent", "medium": "high", "low": "normal"}


class ZendeskAdapter(TicketingAdapter):
    name = "zendesk"

    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        timeout: float = 15.0,
        transport: Any = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.api_token = api_token
        self.timeout = timeout
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            credential = base64.b64encode(f"{self.email}/token:{self.api_token}".encode()).decode()
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                transport=self._transport,
                headers={
                    "Authorization": f"Basic {credential}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def create_ticket(self, payload: dict[str, Any]) -> TicketResult:
        body = {
            "ticket": {
                "subject": payload.get("subject", "Neryva escalation"),
                "comment": {
                    "body": payload.get("description", ""),
                    "public": True,
                },
                "priority": _MAP.get(str(payload.get("priority", "low")).lower(), "normal"),
                "tags": list(payload.get("tags", [])) + ["neryva"],
            }
        }
        try:
            response = await self._get_client().post(
                f"{self.base_url}/api/v2/tickets.json", json=body
            )
            if response.status_code >= 400:
                return TicketResult(False, error=f"zendesk HTTP {response.status_code}: {response.text[:300]}")
            data = response.json()
            ticket = data.get("ticket") or {}
            return TicketResult(
                True,
                ticket_id=str(ticket.get("id") or ""),
                url=ticket.get("url"),
            )
        except Exception as e:  # noqa: BLE001 - errors are results, not crashes
            logger.warning("zendesk_create_ticket_failed", error=str(e))
            return TicketResult(False, error=str(e))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
