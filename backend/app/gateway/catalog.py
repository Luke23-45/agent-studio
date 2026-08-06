"""
Model facts catalog (Arch 8.1, §10; P2-2).

Owned by the gateway, consumed by the context layer: context-window sizes
drive the assembler's budget math; price cards drive cost estimation
(routing, spend caps, usage records). This is the single source of model
facts — the context layer must not carry its own window table.

The seed table is a curated snapshot of public list prices (USD per 1k
tokens); P3-5 billing reconciles against provider-reported usage, and
``register``/``upsert`` allow deployment-time overrides.
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass
from typing import Iterable

logger = structlog.get_logger(__name__)

DEFAULT_CONTEXT_WINDOW: int = 128_000
"""Fallback context window for models absent from the catalog."""


def model_key(provider: str, model: str) -> str:
    return f"{provider.strip().lower()}:{model.strip().lower()}"


@dataclass(frozen=True)
class ModelSpec:
    """Facts + price card for one provider/model pair."""

    provider: str
    model: str
    context_window: int = DEFAULT_CONTEXT_WINDOW
    input_price_per_1k: float = 0.0
    output_price_per_1k: float = 0.0

    @property
    def key(self) -> str:
        return model_key(self.provider, self.model)


# Seed table: public list prices (USD per 1k tokens), context windows
# conservative. Keys normalized via model_key().
_SEED_SPECS: list[ModelSpec] = [
    # OpenAI
    ModelSpec("openai", "gpt-4o", 128_000, 2.50, 10.00),
    ModelSpec("openai", "gpt-4o-mini", 128_000, 0.15, 0.60),
    ModelSpec("openai", "gpt-4.1", 1_000_000, 2.00, 8.00),
    ModelSpec("openai", "gpt-4-turbo", 128_000, 10.00, 30.00),
    ModelSpec("openai", "gpt-4", 8_192, 30.00, 60.00),
    ModelSpec("openai", "o1", 200_000, 15.00, 60.00),
    ModelSpec("openai", "o1-mini", 128_000, 1.10, 4.40),
    ModelSpec("openai", "o3", 200_000, 2.00, 8.00),
    ModelSpec("openai", "o3-mini", 200_000, 1.10, 4.40),
    ModelSpec("openai", "o4-mini", 200_000, 1.10, 4.40),
    # Anthropic
    ModelSpec("anthropic", "claude-3-5-sonnet", 200_000, 3.00, 15.00),
    ModelSpec("anthropic", "claude-3-5-haiku", 200_000, 0.80, 4.00),
    ModelSpec("anthropic", "claude-3-7-sonnet", 200_000, 3.00, 15.00),
    ModelSpec("anthropic", "claude-3-haiku", 200_000, 0.25, 1.25),
    ModelSpec("anthropic", "claude-sonnet-4", 200_000, 3.00, 15.00),
    ModelSpec("anthropic", "claude-sonnet-4.5", 200_000, 3.00, 15.00),
    ModelSpec("anthropic", "claude-sonnet-4.6", 200_000, 3.00, 15.00),
    ModelSpec("anthropic", "claude-opus-4", 200_000, 15.00, 75.00),
    ModelSpec("anthropic", "claude-opus-4.5", 200_000, 5.00, 25.00),
    ModelSpec("anthropic", "claude-haiku-4.5", 200_000, 1.00, 5.00),
    # Google
    ModelSpec("google", "gemini-1.5-pro", 1_000_000, 1.25, 5.00),
    ModelSpec("google", "gemini-1.5-flash", 1_000_000, 0.075, 0.30),
    ModelSpec("google", "gemini-2.0-flash", 1_000_000, 0.10, 0.40),
    ModelSpec("google", "gemini-2.5-pro", 1_000_000, 1.25, 10.00),
]


class ModelCatalog:
    """Registry of ModelSpecs: context windows + price cards."""

    def __init__(self, specs: Iterable[ModelSpec] | None = None):
        self._specs: dict[str, ModelSpec] = {}
        for spec in specs if specs is not None else _SEED_SPECS:
            self._specs[spec.key] = spec

    # -- read --------------------------------------------------------------

    def get(self, provider: str, model: str) -> ModelSpec | None:
        return self._specs.get(model_key(provider, model))

    def context_window(self, provider: str, model: str) -> int:
        """Context window for the model, or the conservative default."""
        spec = self.get(provider, model)
        return spec.context_window if spec else DEFAULT_CONTEXT_WINDOW

    def estimate_cost(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float | None:
        """Estimated USD for the call, or None when no price card exists."""
        spec = self.get(provider, model)
        if spec is None:
            return None
        return (
            input_tokens / 1000.0 * spec.input_price_per_1k
            + output_tokens / 1000.0 * spec.output_price_per_1k
        )

    def list_all(self) -> list[ModelSpec]:
        return sorted(self._specs.values(), key=lambda s: s.key)

    # -- write -------------------------------------------------------------

    def register(self, spec: ModelSpec) -> ModelSpec:
        """Register (or override) a model's facts + price card."""
        self._specs[spec.key] = spec
        return spec

    def upsert(
        self,
        provider: str,
        model: str,
        *,
        context_window: int | None = None,
        input_price_per_1k: float | None = None,
        output_price_per_1k: float | None = None,
    ) -> ModelSpec:
        """Merge fields into the spec (seed defaults preserved)."""
        existing = self.get(provider, model)
        spec = ModelSpec(
            provider=provider,
            model=model,
            context_window=context_window
            if context_window is not None
            else (existing.context_window if existing else DEFAULT_CONTEXT_WINDOW),
            input_price_per_1k=(
                input_price_per_1k
                if input_price_per_1k is not None
                else (existing.input_price_per_1k if existing else 0.0)
            ),
            output_price_per_1k=(
                output_price_per_1k
                if output_price_per_1k is not None
                else (existing.output_price_per_1k if existing else 0.0)
            ),
        )
        return self.register(spec)


# Shared process-wide instance: the default single source of model facts.
# Deployments may build their own ModelCatalog for overrides.
default_catalog = ModelCatalog()


def get_default_catalog() -> ModelCatalog:
    return default_catalog
