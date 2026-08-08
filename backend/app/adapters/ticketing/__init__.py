"""Ticketing adapters: Zendesk / Jira / ServiceNow + registry (P9-3)."""

from .base import TicketResult, TicketingAdapter
from .registry import TicketingConfig, get_ticketing_adapter

__all__ = [
    "TicketResult",
    "TicketingAdapter",
    "TicketingConfig",
    "get_ticketing_adapter",
]
