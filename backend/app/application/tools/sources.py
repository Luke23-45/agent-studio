"""
Tool source abstraction (feature-matrix 6.3, P1).

A ``ToolSource`` contributes zero or more ``ToolSpec`` objects to a
registry. Sources are async because connecting may require I/O (an MCP
handshake); after ``connect()`` the specs are static for the lifetime of
the request or process. The P5-3 gate still authorizes every executed
tool call by name against the tenant registry + surface allowlist -- a
source only decides WHAT can be called, never WHETHER it may be called.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ToolSource(ABC):
    """Asynchronous contributor of tool specs to a ``ToolRegistry``."""

    @abstractmethod
    async def connect(self) -> None:
        """Perform any handshake/discovery I/O before ``tool_specs``."""

    @abstractmethod
    def tool_specs(self) -> list[Any]:
        """The specs this source contributes (stable after ``connect``)."""

    @abstractmethod
    async def close(self) -> None:
        """Release transport resources. Safe to call more than once."""
