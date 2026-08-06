"""
P2-2 gateway/catalog tests: context-window sizes + price cards.

The catalog is the single source of model facts (owned by gateway,
consumed by the context layer).
"""

import pytest

from backend.app.gateway.catalog import (
    DEFAULT_CONTEXT_WINDOW,
    ModelCatalog,
    ModelSpec,
    default_catalog,
    get_default_catalog,
    model_key,
)


def test_model_key_normalization():
    assert model_key("OpenAI", "GPT-4o") == "openai:gpt-4o"


def test_seed_specs_present():
    assert default_catalog.get("openai", "gpt-4o") is not None
    assert default_catalog.get("anthropic", "claude-sonnet-4") is not None
    assert default_catalog.get("google", "gemini-2.5-pro") is not None


def test_context_window_lookup_and_default():
    catalog = ModelCatalog()
    assert catalog.context_window("openai", "gpt-4o") == 128_000
    assert catalog.context_window("anthropic", "claude-sonnet-4") == 200_000
    assert catalog.context_window("unknown", "model") == DEFAULT_CONTEXT_WINDOW


def test_estimate_cost_with_price_card():
    catalog = ModelCatalog()
    # gpt-4o: $2.50 / 1k in, $10.00 / 1k out
    assert catalog.estimate_cost("openai", "gpt-4o", 1000, 1000) == pytest.approx(12.50)
    assert catalog.estimate_cost("openai", "gpt-4o", 0, 0) == pytest.approx(0.0)


def test_estimate_cost_unknown_model_returns_none():
    catalog = ModelCatalog()
    assert catalog.estimate_cost("unknown", "model", 1000, 1000) is None


def test_register_and_upsert():
    catalog = ModelCatalog(specs=[])
    spec = catalog.register(ModelSpec("acme", "custom-1", context_window=64_000))
    assert catalog.get("acme", "custom-1") == spec

    merged = catalog.upsert(
        "acme", "custom-1", input_price_per_1k=1.0, output_price_per_1k=2.0
    )
    assert merged.context_window == 64_000  # seed preserved
    assert merged.input_price_per_1k == 1.0
    assert catalog.estimate_cost("acme", "custom-1", 1000, 1000) == pytest.approx(3.0)


def test_upsert_unknown_model_uses_defaults():
    catalog = ModelCatalog(specs=[])
    merged = catalog.upsert("acme", "new", context_window=32_000)
    assert merged.context_window == 32_000
    assert merged.input_price_per_1k == 0.0


def test_catalog_instance_isolation():
    catalog = ModelCatalog(specs=[])
    assert catalog.context_window("openai", "gpt-4o") == DEFAULT_CONTEXT_WINDOW
    assert default_catalog.context_window("openai", "gpt-4o") == 128_000


def test_get_default_catalog_returns_shared_instance():
    assert get_default_catalog() is default_catalog


def test_list_all_sorted():
    catalog = ModelCatalog()
    keys = [s.key for s in catalog.list_all()]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))
