import json
import logging

import pytest
import responses

from llm_stats_exporter.pricing import (
    LITELLM_PRICING_URL,
    Pricing,
    PricingSource,
    parse_litellm,
)


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


LITELLM_DATA = {
    "sample_spec": {"input_cost_per_token": 0.0, "litellm_provider": "not-a-provider"},
    "gpt-4o": {
        "litellm_provider": "openai",
        "input_cost_per_token": 2.5e-06,
        "output_cost_per_token": 1e-05,
        "cache_read_input_token_cost": 1.25e-06,
        "max_tokens": 16384,
    },
    "anthropic/claude-sonnet-4-6": {
        "litellm_provider": "anthropic",
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
    },
    "gemini-pro": {"litellm_provider": "vertex_ai", "input_cost_per_token": 1e-06},
    "whisper-1": {"litellm_provider": "openai", "mode": "audio_transcription"},
}


def test_parse_litellm_converts_and_filters():
    models = parse_litellm(LITELLM_DATA)
    assert set(models) == {"gpt-4o", "claude-sonnet-4-6"}
    assert models["gpt-4o"] == {"input": 2.5, "output": 10.0, "cache_read": 1.25}
    assert models["claude-sonnet-4-6"] == pytest.approx(
        {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.3}
    )


@responses.activate
def test_pricing_source_fetches_litellm():
    responses.get(LITELLM_PRICING_URL, json=LITELLM_DATA)
    source = PricingSource()
    pricing = source.current()
    assert pricing.rates_for("gpt-4o")["input"] == 2.5
    assert source.current() is pricing
    assert len(responses.calls) == 1


@responses.activate
def test_pricing_source_falls_back_to_bundled_and_retries(caplog):
    responses.get(LITELLM_PRICING_URL, status=500)
    source = PricingSource()
    with caplog.at_level(logging.WARNING):
        pricing = source.current()
    assert "falling back to the bundled pricing table" in caplog.text
    assert pricing.rates_for("gpt-4o") is not None

    responses.reset()
    responses.get(LITELLM_PRICING_URL, json=LITELLM_DATA)
    refreshed = source.current()
    assert refreshed is not pricing
    assert refreshed.rates_for("claude-sonnet-4-6")["cache_write"] == 3.75


@responses.activate
def test_pricing_source_keeps_table_on_refresh_failure(caplog):
    responses.get(LITELLM_PRICING_URL, json=LITELLM_DATA)
    source = PricingSource(refresh_seconds=0)
    pricing = source.current()

    responses.reset()
    responses.get(LITELLM_PRICING_URL, status=502)
    with caplog.at_level(logging.WARNING):
        assert source.current() is pricing
    assert "keeping the previous table" in caplog.text


@responses.activate
def test_pricing_source_rejects_empty_table(caplog):
    responses.get(LITELLM_PRICING_URL, json={"gemini": {"litellm_provider": "vertex_ai"}})
    source = PricingSource()
    with caplog.at_level(logging.WARNING):
        pricing = source.current()
    assert "no openai/anthropic models" in caplog.text
    assert pricing.rates_for("gpt-4o") is not None  # bundled fallback


def test_pricing_source_file_precedence(tmp_path):
    custom = tmp_path / "pricing.json"
    custom.write_text(json.dumps({"models": {"my-model": {"input": 42.0}}}))
    source = PricingSource(source="litellm", file=str(custom))
    pricing = source.current()
    assert pricing.rates_for("my-model")["input"] == 42.0
    assert source.current() is pricing  # no network, cached


def test_pricing_source_bundled():
    source = PricingSource(source="bundled")
    assert source.current().rates_for("claude-sonnet-4-6") is not None
