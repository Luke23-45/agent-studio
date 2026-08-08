"""ServiceNow ticketing adapter (P9-3, feature-matrix 8.2).

POSTs the normalized handoff payload to the ServiceNow Table API
(``/api/now/table/incident``). Auth: HTTP basic with instance
credentials.
"""

from __future__ import annotations

import base64
import structlog
from typing import Any

import httpx

from .base import TicketResult, TicketingAdapter

logger = structlog.get_logger(__name__)

_PRIORITY_MAP = {"high": "1", "medium": "2", "low": "3"}


class ServiceNowAdapter(TicketingAdapter):
    name = "servicenow"

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 15.0,
        transport: Any = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            credential = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                transport=self._transport,
                headers={
                    "Authorization": f"Basic {credential}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        return self._client

    async def create_ticket(self, payload: dict[str, Any]) -> TicketResult:
        body = {
            "short_description": payload.get("subject", "Neryva escalation"),
            "description": payload.get("description", ""),
            "priority": _PRIORITY_MAP.get(str(payload.get("priority", "low")).lower(), "3"),
            "category": "Incident",
        }
        try:
            response = await self._get_client().post(
                f"{self.base_url}/api/now/table/incident", json=body
            )
            if response.status_code >= 400:
                return TicketResult(False, error=f"servicenow HTTP {response.status_code}: {response.text[:300]}")
            data = response.json()
            result = data.get("result") or {}
            sys_id = result.get("sys_id") or result.get("number")
            return TicketResult(
                True,
                ticket_id=str(sys_id) if sys_id else None,
                url=result.get("sys_id") and f"{self.base_url}/incident.do?sys_id={result.get('sys_id')}",
            )
        except Exception as e:  # noqa: BLE001 - errors are results, not crashes
            logger.warning("servicenow_create_ticket_failed", error=str(e))
            return TicketResult(False, error=str(e))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
