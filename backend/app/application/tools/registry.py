"""
Tool registry for the P2-9 runtime loop.

Tools are registered by name (executor + JSON-schema parameters) and
advertised to the model as OpenAI-function-format schemas; the
orchestration loop maps ``LLMResponse.tool_calls`` back to the registry,
authorizes each call (P5-3 gate, wired by the route), and executes it.
Execution never raises: failures become ``is_error`` result blocks the
model can read and recover from. MCP servers become a tool source behind
the registry in a later phase (ledger P5-3).
"""

import inspect
import structlog
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = structlog.get_logger(__name__)


class ToolAlreadyRegisteredError(ValueError):
    """Raised when a tool name is registered twice."""


@dataclass
class ToolSpec:
    """A registered tool: metadata for the model, executor for the loop.

    ``executor`` may be sync or async; ``parameters`` is a JSON schema
    object (``{"type": "object", "properties": {...}}``).
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    executor: Callable[[dict[str, Any]], Any] = field(default_factory=lambda: lambda args: {})


class ToolRegistry:
    """Name-keyed tool registry; safe for concurrent execution."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ToolAlreadyRegisteredError(f"Tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def list(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda s: s.name)

    def schemas(self) -> list[dict[str, Any]]:
        """Tool schemas in OpenAI function format (adapter wire format)."""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self.list()
        ]

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool, capturing failures into an ``is_error`` result.

        The registry never raises: unknown tools and executor exceptions
        become ``{"content": ..., "is_error": True}`` so the model can
        recover in the same turn. Callers may also be sync.
        """
        spec = self.get(name)
        if spec is None:
            return {
                "content": f"unknown tool: {name}",
                "is_error": True,
            }
        try:
            outcome = spec.executor(arguments)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            if isinstance(outcome, dict):
                result = dict(outcome)
                result.setdefault("is_error", False)
                return result
            return {"content": str(outcome), "is_error": False}
        except Exception as e:  # noqa: BLE001 - failures are results, not crashes
            logger.warning("tool_execution_failed", tool=name, error=str(e))
            return {
                "content": f"tool error: {e}",
                "is_error": True,
            }


def create_tool_registry(tools: list[ToolSpec] | None = None) -> ToolRegistry:
    """Build a registry from a list of specs (duplicates raise)."""
    registry = ToolRegistry()
    for spec in tools or []:
        registry.register(spec)
    return registry
