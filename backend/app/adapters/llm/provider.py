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


class AzureAdapter(BaseLLMAdapter):
    """Azure OpenAI provider adapter (OpenAI-compatible API)."""

    def __init__(self, api_key: str, config: LLMConfig, endpoint: str, api_version: str):
        super().__init__(api_key, config)
        self.endpoint = endpoint
        self.api_version = api_version
        self._client: Any | None = None

    @property
    def provider_type(self) -> LLMProviderType:
        return LLMProviderType.AZURE

    def _get_client(self) -> Any:
        """Lazy load Azure OpenAI client."""
        if self._client is None:
            try:
                from openai import AsyncAzureOpenAI
                if not self.endpoint:
                    raise ValueError("AZURE_OPENAI_ENDPOINT is required for the azure provider")
                self._client = AsyncAzureOpenAI(
                    api_key=self.api_key,
                    azure_endpoint=self.endpoint,
                    api_version=self.api_version,
                )
            except ImportError:
                raise ImportError("openai package not installed")
        return self._client

    async def chat(self, messages: list[LLMMessage]) -> LLMResponse:
        """Send chat request to Azure OpenAI."""
        client = self._get_client()
        formatted_messages = [{"role": m.role, "content": m.content} for m in messages]
        response = await client.chat.completions.create(
            model=self.config.model,
            messages=formatted_messages,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            top_p=self.config.top_p,
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

    async def stream_chat(self, messages: list[LLMMessage]) -> Any:
        """Stream chat response from Azure OpenAI."""
        client = self._get_client()
        formatted_messages = [{"role": m.role, "content": m.content} for m in messages]
        return await client.chat.completions.create(
            model=self.config.model,
            messages=formatted_messages,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            stream=True,
        )


class OpenAIBasedAdapter(BaseLLMAdapter):
    """Adapter for any OpenAI-compatible endpoint (Google Gemini, custom gateways).

    Google Gemini exposes an OpenAI-compatible API at
    https://generativelanguage.googleapis.com/v1beta/openai/.
    """

    def __init__(self, api_key: str, config: LLMConfig, base_url: str, provider: LLMProviderType):
        super().__init__(api_key, config)
        self.base_url = base_url
        self.provider = provider
        self._client: Any | None = None

    @property
    def provider_type(self) -> LLMProviderType:
        return self.provider

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

    async def chat(self, messages: list[LLMMessage]) -> LLMResponse:
        """Send chat request to the OpenAI-compatible endpoint."""
        client = self._get_client()
        formatted_messages = [{"role": m.role, "content": m.content} for m in messages]
        response = await client.chat.completions.create(
            model=self.config.model,
            messages=formatted_messages,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            top_p=self.config.top_p,
        )
        choice = response.choices[0]
        return LLMResponse(
            content=choice.message.content or "",
            model=response.model,
            usage={
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                "total_tokens": getattr(response.usage, "total_tokens", 0),
            },
            finish_reason=choice.finish_reason,
            raw_response=response,
        )

    async def stream_chat(self, messages: list[LLMMessage]) -> Any:
        """Stream chat response from the OpenAI-compatible endpoint."""
        client = self._get_client()
        formatted_messages = [{"role": m.role, "content": m.content} for m in messages]
        return await client.chat.completions.create(
            model=self.config.model,
            messages=formatted_messages,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            stream=True,
        )


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
