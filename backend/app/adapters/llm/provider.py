"""
LLM Provider adapters.

Provides unified interface for different LLM providers (OpenAI, Anthropic, etc.).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


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


@dataclass
class LLMResponse:
    """Response from an LLM provider."""

    content: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str | None = None
    raw_response: Any = None


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
    async def stream_chat(
        self, messages: list[LLMMessage]
    ) -> Any:
        """Stream a chat response."""
        pass

    @property
    @abstractmethod
    def provider_type(self) -> LLMProviderType:
        """Return the provider type."""
        pass


class OpenAIAdapter(BaseLLMAdapter):
    """OpenAI provider adapter."""

    def __init__(self, api_key: str, config: LLMConfig):
        super().__init__(api_key, config)
        self._client: Any | None = None

    @property
    def provider_type(self) -> LLMProviderType:
        return LLMProviderType.OPENAI

    def _get_client(self) -> Any:
        """Lazy load OpenAI client."""
        if self._client is None:
            try:
                from openai import AsyncOpenAI
                self._client = AsyncOpenAI(api_key=self.api_key)
            except ImportError:
                raise ImportError("openai package not installed")
        return self._client

    async def chat(self, messages: list[LLMMessage]) -> LLMResponse:
        """Send chat request to OpenAI."""
        client = self._get_client()

        formatted_messages = [
            {"role": m.role, "content": m.content} for m in messages
        ]

        response = await client.chat.completions.create(
            model=self.config.model,
            messages=formatted_messages,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            top_p=self.config.top_p,
            frequency_penalty=self.config.frequency_penalty,
            presence_penalty=self.config.presence_penalty,
            stop=self.config.stop_sequences,
        )

        choice = response.choices[0]
        return LLMResponse(
            content=choice.message.content or "",
            model=response.model,
            usage={
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            },
            finish_reason=choice.finish_reason,
            raw_response=response,
        )

    async def stream_chat(
        self, messages: list[LLMMessage]
    ) -> Any:
        """Stream chat response from OpenAI."""
        client = self._get_client()

        formatted_messages = [
            {"role": m.role, "content": m.content} for m in messages
        ]

        stream = await client.chat.completions.create(
            model=self.config.model,
            messages=formatted_messages,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            stream=True,
        )

        return stream


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

    async def chat(self, messages: list[LLMMessage]) -> LLMResponse:
        """Send chat request to Anthropic."""
        client = self._get_client()

        # Separate system message
        system_message = ""
        chat_messages = []
        for msg in messages:
            if msg.role == "system":
                system_message = msg.content
            else:
                chat_messages.append({"role": msg.role, "content": msg.content})

        response = await client.messages.create(
            model=self.config.model,
            max_tokens=self.config.max_tokens or 1024,
            system=system_message,
            messages=chat_messages,
        )

        return LLMResponse(
            content=response.content[0].text,
            model=response.model,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            finish_reason=response.stop_reason,
            raw_response=response,
        )

    async def stream_chat(
        self, messages: list[LLMMessage]
    ) -> Any:
        """Stream chat response from Anthropic."""
        client = self._get_client()

        system_message = ""
        chat_messages = []
        for msg in messages:
            if msg.role == "system":
                system_message = msg.content
            else:
                chat_messages.append({"role": msg.role, "content": msg.content})

        stream = await client.messages.stream(
            model=self.config.model,
            max_tokens=self.config.max_tokens or 1024,
            system=system_message,
            messages=chat_messages,
        )

        return stream


def create_llm_adapter(
    provider_type: LLMProviderType,
    api_key: str,
    config: LLMConfig,
) -> BaseLLMAdapter:
    """Factory function to create appropriate LLM adapter."""
    adapters = {
        LLMProviderType.OPENAI: OpenAIAdapter,
        LLMProviderType.ANTHROPIC: AnthropicAdapter,
    }

    if provider_type not in adapters:
        raise ValueError(f"Unsupported provider: {provider_type}")

    return adapters[provider_type](api_key, config)
