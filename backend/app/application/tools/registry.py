"""
Tool registry for the P2-9 runtime loop.

Tools are registered by name (executor + JSON-schema parameters) and
advertised to the model as OpenAI-function-format schemas; the
orchestration loop maps ``LLMResponse.tool_calls`` back to the registry,
authorizes each call (P5-3 gate, wired by the route), and executes it.
Execution never raises: failures become ``is_error`` result blocks the
model can read and recover from. MCP servers register through the
``ToolSource`` abstraction (P9-1, feature-matrix 6.3): a source connects
once and contributes specs; the P5-3 gate still authorizes every call.
"""

import inspect
import structlog
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from jsonschema import Draft7Validator, ValidationError

logger = structlog.get_logger(__name__)

_EMPTY_SCHEMA: dict[str, Any] = {"type": "object"}


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
        self._sources: list[Any] = []

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ToolAlreadyRegisteredError(f"Tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    async def register_source(self, source: Any) -> int:
        """Register every spec a ``ToolSource`` contributes.

        Collisions are skipped with a warning (first registrant wins) and
        never raised: one broken server must not take the whole registry
        down. Returns the number of tools registered. The source is tracked
        so ``close()`` can release its connections (P9-1 lifecycle).
        """
        added = 0
        for spec in source.tool_specs():
            if spec.name in self._tools:
                logger.warning(
                    "tool_source_collision_skipped",
                    tool=spec.name,
                    source=getattr(source, "server_name", type(source).__name__),
                )
                continue
            self._tools[spec.name] = spec
            added += 1
        self._sources.append(source)
        return added

    async def close(self) -> None:
        """Release every connected source (httpx clients, stdio processes).

        Idempotent; close failures are logged, never raised.
        """
        for source in self._sources:
            try:
                close = getattr(source, "close", None)
                if close is not None:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
            except Exception as e:  # noqa: BLE001 - lifecycle must not raise
                logger.warning(
                    "tool_source_close_failed",
                    source=getattr(source, "server_name", type(source).__name__),
                    error=str(e),
                )
        self._sources.clear()

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

    def validate_arguments(self, spec: ToolSpec, arguments: Any) -> list[str]:
        """Validate tool arguments against the spec's JSON schema.

        The degenerate ``{"type": "object"}`` schema (no properties) accepts
        anything, so no validator is built for it. Returns a list of human
        readable errors; empty means valid (feature-matrix 6.3 input
        validation on tool calls).
        """
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return ["arguments must be a JSON object"]
        if spec.parameters in (None, _EMPTY_SCHEMA) or not spec.parameters.get("properties"):
            return []
        validator = Draft7Validator(spec.parameters)
        errors: list[str] = []
        for err in sorted(
            validator.iter_errors(arguments), key=lambda e: list(e.path)
        ):
            errors.append(
                f"{'.'.join(str(p) for p in err.path) or '(root)'}: {err.message}"
            )
        return errors

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool, capturing failures into an ``is_error`` result.

        The registry never raises: unknown tools, invalid arguments
        (JSON-schema validation, P9-1) and executor exceptions become
        ``{"content": ..., "is_error": True}`` so the model can recover in
        the same turn. Callers may also be sync.
        """
        spec = self.get(name)
        if spec is None:
            return {
                "content": f"unknown tool: {name}",
                "is_error": True,
            }
        errors = self.validate_arguments(spec, arguments)
        if errors:
            logger.warning(
                "tool_arguments_invalid", tool=name, errors=errors
            )
            return {
                "content": (
                    f"tool {name}: invalid arguments: " + "; ".join(errors[:5])
                ),
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
