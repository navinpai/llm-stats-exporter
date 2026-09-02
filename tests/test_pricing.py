import json

import pytest

from llm_stats_exporter.pricing import Pricing


@pytest.fixture
def pricing():
    return Pricing(
        {
            "gpt-4o": {"input": 2.5, "output": 10.0, "cache_read": 1.25},
            "gpt-4o-mini": {"input": 0.15, "output": 0.6},
            "default": {"input": 1.0, "output": 2.0},
        }
    )


def test_exact_match(pricing):
    assert pricing.rates_for("gpt-4o")["input"] == 2.5


def test_longest_prefix_match(pricing):
    assert pricing.rates_for("gpt-4o-mini-2024-07-18")["input"] == 0.15
    assert pricing.rates_for("gpt-4o-2024-08-06")["input"] == 2.5


def test_default_fallback(pricing):
    assert pricing.rates_for("some-new-model")["input"] == 1.0


def test_no_default_returns_none():
    assert Pricing({"gpt-4o": {"input": 2.5}}).rates_for("mystery") is None


def test_estimate_usd(pricing):
    tokens = {"input": 1_000_000, "output": 500_000, "cache_read": 2_000_000}
    estimate = pricing.estimate_usd("gpt-4o", tokens)
    assert estimate == pytest.approx(2.5 + 5.0 + 2.5)


def test_estimate_unpriced_model_is_none():
    assert Pricing({}).estimate_usd("mystery", {"input": 100}) is None


def test_load_bundled_pricing():
    pricing = Pricing.load()
    assert pricing.rates_for("claude-sonnet-4-6") is not None
    assert pricing.rates_for("gpt-4o") is not None


def test_load_custom_file(tmp_path):
    custom = tmp_path / "pricing.json"
    custom.write_text(json.dumps({"models": {"my-model": {"input": 42.0, "output": 84.0}}}))
    pricing = Pricing.load(str(custom))
    assert pricing.rates_for("my-model")["input"] == 42.0
    assert pricing.rates_for("gpt-4o") is None
