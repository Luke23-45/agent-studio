"""
LLM provider adapters (Arch 10, P3-1).

One contract for chat, streaming, tool calls, and structured output across
OpenAI, Anthropic, Gemini, Azure, and self-hosted (OpenAI-compatible)
endpoints; all wire translation lives inside this module. Application code
calls the interface — zero provider branches (ledger P3-1 acceptance).

Contract:
- ``chat()``          : one-shot completion -> ``LLMResponse``.
- ``stream()``        : normalized streaming -> ``AsyncIterator[LLMStreamEvent]``
                        (delta, tool_use_start/delta/end, usage, done, error).
- ``stream_chat()``   : legacy raw stream (pre-P3-9 consumers only; the
                        gateway path uses ``stream()``).
- ``LLMConfig.structured_output``: JSON schema; OpenAI/Azure/Gemini use
  ``response_format``, Anthropic a forced hidden tool (arguments decode to
  the JSON body — consumers read ``content``/final tool_use arguments).
- Usage is canonical everywhere (input/output/reasoning/cached tokens,
  P0-10); reasoning tokens surface in usage of every response.

``create_llm_adapter`` is the only entry point used by runtime code.
"""

from __future__ import annotations

import json
import structlog
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator

logger = structlog.get_logger(__name__)

STRUCTURED_OUTPUT_TOOL_NAME = "__structured_output__"
"""Anthropic hidden tool used for structured output (P3-1)."""


class LLMProviderType(Enum):
    """Supported LLM providers."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    AZURE = "azure"
    CUSTOM = "custom"


@dataclass
class LLMMessage:
    """A single message in a conversation."""

    role: str  # "system", "user", "assistant"
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


# Canonical usage keys (Arch 10 usage contract, P0-10): every adapter maps
# provider-specific usage to input/output/reasoning/cached tokens.
USAGE_INPUT = "input_tokens"
USAGE_OUTPUT = "output_tokens"
USAGE_REASONING = "reasoning_tokens"
USAGE_CACHED = "cached_tokens"

_ZERO_USAGE: dict[str, int] = {
    USAGE_INPUT: 0,
    USAGE_OUTPUT: 0,
    USAGE_REASONING: 0,
    USAGE_CACHED: 0,
}


@dataclass
class LLMStreamEvent:
    """One normalized streaming event (P3-1).

    Types:
    - ``delta``           : text content delta.
    - ``tool_use_start``  : a tool call began (``id``, ``name``, ``index``).
    - ``tool_use_delta``  : incremental arguments JSON (``arguments``).
    - ``tool_use_end``    : the tool call finished (cumulative raw JSON in
                            ``arguments`` on Anthropic; empty on OpenAI —
                            consumers accumulate deltas).
    - ``usage``           : canonical usage snapshot (may arrive mid-stream).
    - ``done``            : stream complete (``finish_reason``, final usage).
    - ``error``           : provider-side error; the stream ends after this.
    """

    type: str  # delta | tool_use_start | tool_use_delta | tool_use_end | usage | done | error
    content: str = ""
    index: int | None = None
    id: str = ""
    name: str = ""
    arguments: str = ""
    usage: dict[str, int] | None = None
    finish_reason: str | None = None
    error: str = ""
    raw: Any = None


@dataclass
class LLMResponse:
    """Response from an LLM provider."""

    content: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str | None = None
    raw_response: Any = None
    # P2-9: provider-agnostic tool calls, each {"id", "name", "arguments"}
    # with ``arguments`` decoded to a dict (raw string kept when the model
    # emitted malformed JSON -- the runtime treats that as an error result).
    tool_calls: list[dict[str, Any]] | None = None


@dataclass
class LLMConfig:
    """Configuration for LLM calls."""

    model: str
    temperature: float = 0.7
    max_tokens: int | None = None
    top_p: float = 1.0
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    stop_sequences: list[str] = field(default_factory=list)
    # P2-9: tool schemas advertised on the request, OpenAI function format:
    # [{"type": "function", "function": {"name", "description", "parameters"}}].
    # Adapters translate to their provider's wire format; empty = no tools.
    tools: list[dict[str, Any]] = field(default_factory=list)
    # P3-1: JSON schema for structured output. OpenAI/Azure/Gemini use
    # response_format json_schema; Anthropic a forced hidden tool; CUSTOM
    # endpoints skip it (compat gateways may reject unknown parameters —
    # logged, never silently assumed).
    structured_output: dict[str, Any] | None = None


class BaseLLMAdapter(ABC):
    """Base adapter for LLM providers."""

    def __init__(self, api_key: str, config: LLMConfig):
        self.api_key = api_key
        self.config = config

    @abstractmethod
    async def chat(self, messages: list[LLMMessage]) -> LLMResponse:
        """Send a chat request and get a response."""
        pass

    @abstractmethod
    async def stream(self, messages: list[LLMMessage]) -> AsyncIterator[LLMStreamEvent]:
        """Normalized streaming (P3-1): yields ``LLMStreamEvent`` events."""
        pass

    @abstractmethod
    async def stream_chat(
        self, messages: list[LLMMessage]
    ) -> Any:
        """Legacy raw stream (pre-P3-9 consumers only)."""
        pass

    @property
    @abstractmethod
    def provider_type(self) -> LLMProviderType:
        """Return the provider type."""
        pass


def _openai_usage(response: Any) -> dict[str, int]:
    """Normalize an OpenAI-family usage object (None-safe)."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return dict(_ZERO_USAGE)
    completion_details = getattr(usage, "completion_tokens_details", None)
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    return {
        USAGE_INPUT: getattr(usage, "prompt_tokens", 0) or 0,
        USAGE_OUTPUT: getattr(usage, "completion_tokens", 0) or 0,
        USAGE_REASONING: getattr(completion_details, "reasoning_tokens", 0) or 0,
        USAGE_CACHED: getattr(prompt_details, "cached_tokens", 0) or 0,
    }


def _anthropic_usage(usage: Any) -> dict[str, int]:
    """Normalize an Anthropic usage object (None-safe)."""
    if usage is None:
        return dict(_ZERO_USAGE)
    cached = (getattr(usage, "cache_read_input_tokens", 0) or 0) + (
        getattr(usage, "cache_creation_input_tokens", 0) or 0
    )
    return {
        USAGE_INPUT: getattr(usage, "input_tokens", 0) or 0,
        USAGE_OUTPUT: getattr(usage, "output_tokens", 0) or 0,
        USAGE_REASONING: getattr(usage, "reasoning_tokens", 0) or 0,
        USAGE_CACHED: cached,
    }


def _extract_usage(provider_type: LLMProviderType, response: Any) -> dict[str, int]:
    """Extract canonical usage from a raw chat or stream chunk response."""
    if provider_type == LLMProviderType.ANTHROPIC:
        return _anthropic_usage(getattr(response, "usage", None))
    return _openai_usage(response)


def _parse_openai_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    """Normalize an OpenAI-family ``message.tool_calls`` list (None-safe).

    Each entry becomes {"id", "name", "arguments"}; the arguments JSON
    string is decoded when possible, kept verbatim otherwise (malformed
    JSON is surfaced to the runtime as an error result, never dropped).
    """
    parsed: list[dict[str, Any]] = []
    for tc in tool_calls or []:
        fn = getattr(tc, "function", None)
        fn_name = getattr(fn, "name", None) or "" if fn else ""
        raw_arguments = getattr(fn, "arguments", "") or "" if fn else ""
        try:
            decoded: Any = json.loads(raw_arguments) if raw_arguments else {}
            if not isinstance(decoded, dict):
                decoded = raw_arguments
        except (TypeError, ValueError):
            decoded = raw_arguments
        parsed.append(
            {
                "id": getattr(tc, "id", None) or "",
                "name": fn_name,
                "arguments": decoded,
            }
        )
    return [p for p in parsed if p["name"]]


def _parse_anthropic_content(
    content_blocks: Any, structured_tool_name: str | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Split Anthropic content blocks into (text, tool_calls).

    Text blocks are joined in order; ``tool_use`` blocks become canonical
    tool calls. Tool-only responses (previously an AttributeError on
    ``content[0].text``) now round-trip cleanly. When
    ``structured_tool_name`` is set, that tool's input is returned as
    ``content`` (JSON body) and excluded from ``tool_calls``.
    """
    text_parts: list[str] = []
    calls: list[dict[str, Any]] = []
    for block in content_blocks or []:
        block_type = getattr(block, "type", None)
        if block_type == "tool_use":
            name = getattr(block, "name", None) or ""
            if structured_tool_name and name == structured_tool_name:
                text_parts.append(json.dumps(getattr(block, "input", None) or {}))
                continue
            calls.append(
                {
                    "id": getattr(block, "id", None) or "",
                    "name": name,
                    "arguments": getattr(block, "input", None) or {},
                }
            )
        elif block_type == "text":
            text_parts.append(getattr(block, "text", "") or "")
    return "".join(text_parts), calls


def _anthropic_tools_param(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate OpenAI-style schemas to the Anthropic ``tools`` format."""
    if not tools:
        return []
    translated: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function", tool)
        translated.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object"}),
            }
        )
    return translated


def _structured_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize a structured-output schema (must be a JSON object)."""
    if schema.get("type") != "object":
        return {"type": "object", "properties": {}, "additionalProperties": False, **schema}
    return schema


class _OpenAICompatAdapter(BaseLLMAdapter):
    """Shared OpenAI-compatible adapter behavior (OpenAI, Azure, Gemini,
    custom gateways). Provider-specific wiring is confined to
    ``_get_client`` and the ``provider_type`` property."""

    provider: LLMProviderType = LLMProviderType.OPENAI
    include_usage_stream: bool = True
    structured_supported: bool = True

    @property
    def provider_type(self) -> LLMProviderType:
        return self.provider

    def _get_client(self) -> Any:
        raise NotImplementedError

    @staticmethod
    def _formatted(messages: list[LLMMessage]) -> list[dict[str, Any]]:
        return [{"role": m.role, "content": m.content} for m in messages]

    def _request_kwargs(self, *, stream: bool) -> dict[str, Any]:
        kwargs: dict[str, Any] = dict(
            model=self.config.model,
            messages=self._formatted([]),  # replaced by callers
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            top_p=self.config.top_p,
            frequency_penalty=self.config.frequency_penalty,
            presence_penalty=self.config.presence_penalty,
            stop=self.config.stop_sequences,
            tools=self.config.tools or None,
            stream=stream,
        )
        if stream and self.include_usage_stream:
            kwargs["stream_options"] = {"include_usage": True}
        if self.config.structured_output and self.structured_supported:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "schema": _structured_schema(self.config.structured_output),
                    "strict": True,
                },
            }
        elif self.config.structured_output:
            logger.warning(
                "structured_output_skipped_for_provider",
                provider=self.provider.value,
                hint="compat gateway may reject response_format; pass through a structured-capable provider",
            )
        return kwargs

    async def chat(self, messages: list[LLMMessage]) -> LLMResponse:
        """Send chat request to the OpenAI-compatible endpoint."""
        client = self._get_client()
        kwargs = self._request_kwargs(stream=False)
        kwargs["messages"] = self._formatted(messages)
        response = await client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        return LLMResponse(
            content=choice.message.content or "",
            model=response.model,
            usage=_openai_usage(response),
            finish_reason=choice.finish_reason,
            raw_response=response,
            tool_calls=_parse_openai_tool_calls(choice.message.tool_calls),
        )

    async def stream(self, messages: list[LLMMessage]) -> AsyncIterator[LLMStreamEvent]:
        """Normalized streaming (P3-1): delta / tool_use_* / usage / done."""
        client = self._get_client()
        kwargs = self._request_kwargs(stream=True)
        kwargs["messages"] = self._formatted(messages)
        stream = await client.chat.completions.create(**kwargs)

        usage = dict(_ZERO_USAGE)
        finish_reason: str | None = None
        open_tools: dict[int, dict[str, Any]] = {}

        async for chunk in stream:
            if getattr(chunk, "usage", None) is not None:
                usage = _openai_usage(chunk)
                yield LLMStreamEvent(type="usage", usage=usage)
                continue
            for choice in chunk.choices or []:
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                delta = choice.delta
                if delta and delta.content:
                    yield LLMStreamEvent(
                        type="delta", content=delta.content, index=choice.index
                    )
                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = getattr(tc, "index", 0)
                        state = open_tools.get(idx)
                        if state is None:
                            state = {
                                "id": getattr(tc, "id", "") or "",
                                "name": (tc.function.name if tc.function else "") or "",
                            }
                            open_tools[idx] = state
                            yield LLMStreamEvent(
                                type="tool_use_start",
                                index=idx,
                                id=state["id"],
                                name=state["name"],
                            )
                        if tc.function and tc.function.arguments:
                            yield LLMStreamEvent(
                                type="tool_use_delta",
                                index=idx,
                                id=state["id"],
                                arguments=tc.function.arguments,
                            )
        for idx, state in open_tools.items():
            yield LLMStreamEvent(
                type="tool_use_end", index=idx, id=state["id"], name=state["name"]
            )
        yield LLMStreamEvent(type="done", finish_reason=finish_reason, usage=usage)

    async def stream_chat(self, messages: list[LLMMessage]) -> Any:
        """Legacy raw stream (pre-P3-9 consumers only)."""
        client = self._get_client()
        kwargs = self._request_kwargs(stream=True)
        kwargs["messages"] = self._formatted(messages)
        return await client.chat.completions.create(**kwargs)


class OpenAIAdapter(_OpenAICompatAdapter):
    """OpenAI provider adapter."""

    provider = LLMProviderType.OPENAI

    def __init__(self, api_key: str, config: LLMConfig):
        super().__init__(api_key, config)
        self._client: Any | None = None

    def _get_client(self) -> Any:
        """Lazy load OpenAI client."""
        if self._client is None:
            try:
                from openai import AsyncOpenAI

                self._client = AsyncOpenAI(api_key=self.api_key)
            except ImportError:
                raise ImportError("openai package not installed")
        return self._client


class AzureAdapter(_OpenAICompatAdapter):
    """Azure OpenAI provider adapter (OpenAI-compatible API)."""

    provider = LLMProviderType.AZURE

    def __init__(self, api_key: str, config: LLMConfig, endpoint: str, api_version: str):
        super().__init__(api_key, config)
        self.endpoint = endpoint
        self.api_version = api_version
        self._client: Any | None = None

    def _get_client(self) -> Any:
        """Lazy load Azure OpenAI client."""
        if self._client is None:
            try:
                from openai import AsyncAzureOpenAI

                if not self.endpoint:
                    raise ValueError(
                        "AZURE_OPENAI_ENDPOINT is required for the azure provider"
                    )
                self._client = AsyncAzureOpenAI(
                    api_key=self.api_key,
                    azure_endpoint=self.endpoint,
                    api_version=self.api_version,
                )
            except ImportError:
                raise ImportError("openai package not installed")
        return self._client


class OpenAIBasedAdapter(_OpenAICompatAdapter):
    """Adapter for any OpenAI-compatible endpoint (Google Gemini, custom gateways).

    Google Gemini exposes an OpenAI-compatible API at
    https://generativelanguage.googleapis.com/v1beta/openai/.
    """

    provider = LLMProviderType.CUSTOM
    include_usage_stream = False
    structured_supported = False

    def __init__(
        self,
        api_key: str,
        config: LLMConfig,
        base_url: str,
        provider: LLMProviderType,
    ):
        super().__init__(api_key, config)
        self.base_url = base_url
        self.provider = provider
        if provider == LLMProviderType.GOOGLE:
            self.structured_supported = True
        self._client: Any | None = None

    def _get_client(self) -> Any:
        """Lazy load OpenAI-compatible client."""
        if self._client is None:
            try:
                from openai import AsyncOpenAI

                if not self.base_url:
                    raise ValueError(
                        f"A base URL is required for the {self.provider.value} provider"
                    )
                self._client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
            except ImportError:
                raise ImportError("openai package not installed")
        return self._client


class AnthropicAdapter(BaseLLMAdapter):
    """Anthropic provider adapter."""

    def __init__(self, api_key: str, config: LLMConfig):
        super().__init__(api_key, config)
        self._client: Any | None = None

    @property
    def provider_type(self) -> LLMProviderType:
        return LLMProviderType.ANTHROPIC

    def _get_client(self) -> Any:
        """Lazy load Anthropic client."""
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic

                self._client = AsyncAnthropic(api_key=self.api_key)
            except ImportError:
                raise ImportError("anthropic package not installed")
        return self._client

    @staticmethod
    def _system_param(messages: list[LLMMessage]) -> Any:
        """Build the Anthropic ``system`` parameter from all system messages.

        Every system-role message (system prefix, summary layers, memory,
        knowledge) is included — earlier code kept only the last one. When
        any block carries cache_control metadata (P2-6), the system is sent
        as text blocks with explicit ephemeral breakpoints on the marked
        blocks (Anthropic caches from each breakpoint to the end of the
        request); otherwise the joined string form keeps the wire format
        unchanged.
        """
        blocks: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role != "system":
                continue
            block: dict[str, Any] = {"type": "text", "text": msg.content}
            if msg.metadata.get("cache_control"):
                block["cache_control"] = {"type": "ephemeral"}
            blocks.append(block)
        if not blocks:
            return ""
        if not any("cache_control" in block for block in blocks):
            return "\n\n".join(block["text"] for block in blocks)
        return blocks

    @staticmethod
    def _chat_params(messages: list[LLMMessage]) -> list[dict[str, Any]]:
        """Non-system messages for the ``messages`` parameter.

        Messages with cache_control metadata are sent in content-block form
        with the ephemeral breakpoint (required by Anthropic for per-message
        caching); unmarked messages keep the plain-string form.
        """
        chat_messages: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == "system":
                continue
            if msg.metadata.get("cache_control"):
                chat_messages.append(
                    {
                        "role": msg.role,
                        "content": [{"type": "text", "text": msg.content}],
                        "cache_control": {"type": "ephemeral"},
                    }
                )
            else:
                chat_messages.append({"role": msg.role, "content": msg.content})
        return chat_messages

    def _params(self, messages: list[LLMMessage]) -> dict[str, Any]:
        """Shared request parameters, including structured-output wiring."""
        params: dict[str, Any] = dict(
            model=self.config.model,
            max_tokens=self.config.max_tokens or 1024,
            system=self._system_param(messages),
            messages=self._chat_params(messages),
        )
        tools = _anthropic_tools_param(self.config.tools)
        if self.config.structured_output:
            tools = tools + [
                {
                    "name": STRUCTURED_OUTPUT_TOOL_NAME,
                    "description": "Structured output response tool (internal)",
                    "input_schema": _structured_schema(self.config.structured_output),
                }
            ]
            params["tool_choice"] = {
                "type": "tool",
                "name": STRUCTURED_OUTPUT_TOOL_NAME,
            }
        if tools:
            params["tools"] = tools
        return params

    async def chat(self, messages: list[LLMMessage]) -> LLMResponse:
        """Send chat request to Anthropic."""
        client = self._get_client()
        response = await client.messages.create(**self._params(messages))

        content, tool_calls = _parse_anthropic_content(
            response.content,
            structured_tool_name=(
                STRUCTURED_OUTPUT_TOOL_NAME if self.config.structured_output else None
            ),
        )
        return LLMResponse(
            content=content,
            model=response.model,
            usage=_anthropic_usage(response.usage),
            finish_reason=response.stop_reason,
            raw_response=response,
            tool_calls=tool_calls or None,
        )

    async def stream(self, messages: list[LLMMessage]) -> AsyncIterator[LLMStreamEvent]:
        """Normalized streaming (P3-1) over Anthropic raw events."""
        client = self._get_client()
        params = self._params(messages)

        usage = dict(_ZERO_USAGE)
        finish_reason: str | None = None
        open_tools: dict[int, dict[str, Any]] = {}

        async with client.messages.stream(**params) as stream:
            async for event in stream:
                etype = getattr(event, "type", None)
                if etype == "message_start":
                    u = getattr(event, "message", None)
                    uu = getattr(u, "usage", None)
                    if uu is not None:
                        usage = _anthropic_usage(uu)
                elif etype == "content_block_start":
                    block = getattr(event, "content_block", None)
                    btype = getattr(block, "type", None)
                    idx = getattr(event, "index", None)
                    if btype == "tool_use":
                        state = {
                            "id": getattr(block, "id", "") or "",
                            "name": getattr(block, "name", "") or "",
                        }
                        open_tools[idx] = state
                        yield LLMStreamEvent(
                            type="tool_use_start",
                            index=idx,
                            id=state["id"],
                            name=state["name"],
                        )
                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dtype = getattr(delta, "type", None)
                    idx = getattr(event, "index", None)
                    if dtype == "text_delta":
                        yield LLMStreamEvent(
                            type="delta",
                            content=getattr(delta, "text", "") or "",
                            index=idx,
                        )
                    elif dtype == "input_json_delta":
                        state = open_tools.get(idx, {})
                        yield LLMStreamEvent(
                            type="tool_use_delta",
                            index=idx,
                            id=state.get("id", ""),
                            arguments=getattr(delta, "partial_json", "") or "",
                        )
                elif etype == "content_block_stop":
                    idx = getattr(event, "index", None)
                    state = open_tools.pop(idx, None)
                    if state:
                        yield LLMStreamEvent(
                            type="tool_use_end",
                            index=idx,
                            id=state["id"],
                            name=state["name"],
                        )
                elif etype == "message_delta":
                    d = getattr(event, "delta", None)
                    if getattr(d, "stop_reason", None):
                        finish_reason = d.stop_reason
                    u = getattr(event, "usage", None)
                    if u is not None and getattr(u, "output_tokens", None) is not None:
                        usage[USAGE_OUTPUT] = u.output_tokens
                elif etype == "error":
                    err = getattr(event, "error", None)
                    yield LLMStreamEvent(
                        type="error", error=str(getattr(err, "message", "") or err)
                    )
                elif etype == "message_stop":
                    break
        yield LLMStreamEvent(type="done", finish_reason=finish_reason, usage=usage)

    async def stream_chat(self, messages: list[LLMMessage]) -> Any:
        """Legacy raw stream (pre-P3-9 consumers only)."""
        client = self._get_client()
        return await client.messages.stream(**self._params(messages))


def create_llm_adapter(
    provider_type: LLMProviderType,
    api_key: str,
    config: LLMConfig,
) -> BaseLLMAdapter:
    """Factory function to create appropriate LLM adapter."""
    from backend.app.settings.env import settings

    if provider_type == LLMProviderType.OPENAI:
        return OpenAIAdapter(api_key, config)
    if provider_type == LLMProviderType.ANTHROPIC:
        return AnthropicAdapter(api_key, config)
    if provider_type == LLMProviderType.AZURE:
        return AzureAdapter(
            api_key, config,
            endpoint=settings.AZURE_OPENAI_ENDPOINT or "",
            api_version=settings.AZURE_OPENAI_API_VERSION,
        )
    if provider_type == LLMProviderType.GOOGLE:
        return OpenAIBasedAdapter(
            api_key, config,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            provider=LLMProviderType.GOOGLE,
        )
    if provider_type == LLMProviderType.CUSTOM:
        return OpenAIBasedAdapter(
            api_key, config,
            base_url=settings.CUSTOM_LLM_BASE_URL or "",
            provider=LLMProviderType.CUSTOM,
        )

    raise ValueError(f"Unsupported provider: {provider_type}")
