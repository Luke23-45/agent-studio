"""Jira ticketing adapter (P9-3, feature-matrix 8.2).

POSTs the normalized handoff payload to the Jira REST API
(``/rest/api/2/issue``) as a new issue in the configured project.
Auth: bearer (personal access token or OAuth2 access token).
"""

from __future__ import annotations

import structlog
from typing import Any

import httpx

from .base import TicketResult, TicketingAdapter

logger = structlog.get_logger(__name__)

_PRIORITY_MAP = {"high": "Highest", "medium": "High", "low": "Medium"}


class JiraAdapter(TicketingAdapter):
    name = "jira"

    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        project_key: str,
        email: str | None = None,
        timeout: float = 15.0,
        transport: Any = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.project_key = project_key
        self.timeout = timeout
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                transport=self._transport,
                headers={
                    "Authorization": f"Bearer {self.api_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        return self._client

    async def create_ticket(self, payload: dict[str, Any]) -> TicketResult:
        body = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": payload.get("subject", "Neryva escalation"),
                "description": payload.get("description", ""),
                "priority": {"name": _PRIORITY_MAP.get(
                    str(payload.get("priority", "low")).lower(), "Medium"
                )},
                "labels": [label for label in payload.get("tags", [])][:20],
            }
        }
        try:
            response = await self._get_client().post(
                f"{self.base_url}/rest/api/2/issue", json=body
            )
            if response.status_code >= 400:
                return TicketResult(False, error=f"jira HTTP {response.status_code}: {response.text[:300]}")
            data = response.json()
            key = data.get("key") or data.get("id")
            return TicketResult(
                True,
                ticket_id=str(key),
                url=f"{self.base_url}/browse/{key}" if key else None,
            )
        except Exception as e:  # noqa: BLE001 - errors are results, not crashes
            logger.warning("jira_create_ticket_failed", error=str(e))
            return TicketResult(False, error=str(e))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
