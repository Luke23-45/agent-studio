"""
P9-3 — Ticketing adapter contracts (feature-matrix 8.2).

One adapter per helpdesk system (Zendesk / Jira / ServiceNow), plus the
pre-existing generic webhook as the default transport. Adapters translate
the normalized ``HandoffRequest.to_ticket_payload()`` dict into each
vendor's API shape; ``TicketResult`` normalizes the outcome for the
escalation service.

All adapters use httpx with an injectable transport so tests never touch
the network.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class TicketResult:
    ok: bool
    ticket_id: str | None = None
    url: str | None = None
    error: str | None = None


class TicketingAdapter(ABC):
    """Create a ticket in one helpdesk system."""

    name: str = "generic"

    @abstractmethod
    async def create_ticket(self, payload: dict[str, Any]) -> TicketResult:
        """Send the normalized payload; never raises (errors become results)."""
