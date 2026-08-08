"""Ticketing adapter registry (P9-3, feature-matrix 8.2).

Builds the configured adapter from settings/env. ``generic`` keeps the
pre-existing webhook transport; zendesk / jira / servicenow use the
concrete adapters. Unknown or unconfigured adapters yield None and the
escalation service falls back to local recording (no silent partial
delivery).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TicketingConfig:
    adapter: str = "generic"
    zendesk_url: str | None = None
    zendesk_email: str | None = None
    zendesk_api_token: str | None = None
    jira_url: str | None = None
    jira_api_token: str | None = None
    jira_project_key: str | None = None
    servicenow_url: str | None = None
    servicenow_username: str | None = None
    servicenow_password: str | None = None

    @classmethod
    def from_settings(cls) -> "TicketingConfig":
        from backend.app.settings.env import settings

        return cls(
            adapter=(settings.TICKETING_ADAPTER or "generic").strip().lower(),
            zendesk_url=settings.ZENDESK_URL,
            zendesk_email=settings.ZENDESK_EMAIL,
            zendesk_api_token=settings.ZENDESK_API_TOKEN,
            jira_url=settings.JIRA_URL,
            jira_api_token=settings.JIRA_API_TOKEN,
            jira_project_key=settings.JIRA_PROJECT_KEY,
            servicenow_url=settings.SERVICENOW_URL,
            servicenow_username=settings.SERVICENOW_USERNAME,
            servicenow_password=settings.SERVICENOW_PASSWORD,
        )

    @property
    def is_configured(self) -> bool:
        if self.adapter == "zendesk":
            return bool(self.zendesk_url and self.zendesk_email and self.zendesk_api_token)
        if self.adapter == "jira":
            return bool(self.jira_url and self.jira_api_token and self.jira_project_key)
        if self.adapter == "servicenow":
            return bool(self.servicenow_url and self.servicenow_username and self.servicenow_password)
        return self.adapter == "generic"


def get_ticketing_adapter(config: TicketingConfig, *, transport: Any = None) -> Any | None:
    """Instantiate the configured adapter (None when unconfigured/unknown)."""
    if config.adapter == "zendesk":
        if not config.is_configured:
            return None
        from .zendesk import ZendeskAdapter

        return ZendeskAdapter(
            base_url=config.zendesk_url,  # type: ignore[arg-type]
            email=config.zendesk_email,  # type: ignore[arg-type]
            api_token=config.zendesk_api_token,  # type: ignore[arg-type]
            transport=transport,
        )
    if config.adapter == "jira":
        if not config.is_configured:
            return None
        from .jira import JiraAdapter

        return JiraAdapter(
            base_url=config.jira_url,  # type: ignore[arg-type]
            api_token=config.jira_api_token,  # type: ignore[arg-type]
            project_key=config.jira_project_key,  # type: ignore[arg-type]
            transport=transport,
        )
    if config.adapter == "servicenow":
        if not config.is_configured:
            return None
        from .servicenow import ServiceNowAdapter

        return ServiceNowAdapter(
            base_url=config.servicenow_url,  # type: ignore[arg-type]
            username=config.servicenow_username,  # type: ignore[arg-type]
            password=config.servicenow_password,  # type: ignore[arg-type]
            transport=transport,
        )
    return None
