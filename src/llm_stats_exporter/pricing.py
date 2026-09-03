"""Pricing table for estimated per-key/per-model cost.

Prices are USD per 1M tokens. By default the table is fetched from LiteLLM's
community-maintained pricing JSON and refreshed periodically, with the bundled
snapshot (``data/pricing.json``) as fallback. Set ``PRICING_SOURCE=bundled``
to skip the network entirely, or ``PRICING_FILE`` to use a custom table.
Model lookup is exact match first, then longest prefix match (so dated
snapshots like ``gpt-4o-2024-08-06`` resolve to ``gpt-4o``), then ``default``
if set.
"""

from __future__ import annotations

import json
import logging
import time
from importlib import resources
from pathlib import Path

import requests

from llm_stats_exporter.metrics import PRICING_LAST_REFRESH, PRICING_MODELS

log = logging.getLogger(__name__)

BILLABLE_CATEGORIES = ("input", "output", "cache_read", "cache_write")

# Both OpenAI and Anthropic bill the Batch API at 50% of standard rates.
# Other tiers (flex, priority, ...) have model-specific premiums/discounts and
# default to standard rates; override via PRICING_TIER_MULTIPLIERS.
DEFAULT_TIER_MULTIPLIERS = {"batch": 0.5}

LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
LITELLM_PROVIDERS = frozenset({"openai", "anthropic"})
LITELLM_RATE_FIELDS = {
    "input_cost_per_token": "input",
    "output_cost_per_token": "output",
    "cache_read_input_token_cost": "cache_read",
    "cache_creation_input_token_cost": "cache_write",
}


class Pricing:
    def __init__(
        self,
        models: dict[str, dict[str, float]],
        tier_multipliers: dict[str, float] | None = None,
    ) -> None:
        self._models = models
        self._tier_multipliers = (
            DEFAULT_TIER_MULTIPLIERS if tier_multipliers is None else tier_multipliers
        )

    def __len__(self) -> int:
        return len(self._models)

    @classmethod
    def load(
        cls, path: str | None = None, tier_multipliers: dict[str, float] | None = None
    ) -> Pricing:
        if path:
            raw = Path(path).read_text(encoding="utf-8")
        else:
            raw = (resources.files("llm_stats_exporter") / "data" / "pricing.json").read_text(
                encoding="utf-8"
            )
        data = json.loads(raw)
        models = {
            name: {k: float(v) for k, v in rates.items()}
            for name, rates in data.get("models", {}).items()
        }
        log.info("Loaded pricing for %d model(s)%s.", len(models), f" from {path}" if path else "")
        return cls(models, tier_multipliers)

    def rates_for(self, model: str) -> dict[str, float] | None:
        if model in self._models:
            return self._models[model]
        best: str | None = None
        for name in self._models:
            if (
                name != "default"
                and model.startswith(name)
                and (best is None or len(name) > len(best))
            ):
                best = name
        if best is not None:
            return self._models[best]
        return self._models.get("default")

    def estimate_usd(
        self, model: str, tokens: dict[str, float], service_tier: str = "standard"
    ) -> float | None:
        """Estimated USD cost for a usage record, or None when model is unpriced."""
        rates = self.rates_for(model)
        if rates is None:
            log.debug("No pricing for model %r; skipping estimate.", model)
            return None
        multiplier = self._tier_multipliers.get(service_tier, 1.0)
        return multiplier * sum(
            tokens.get(cat, 0.0) / 1_000_000.0 * rates.get(cat, 0.0) for cat in BILLABLE_CATEGORIES
        )


def parse_litellm(data: dict[str, object]) -> dict[str, dict[str, float]]:
    """Convert LiteLLM's per-token pricing JSON to per-1M-token rates."""
    models: dict[str, dict[str, float]] = {}
    for name, entry in data.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("litellm_provider") not in LITELLM_PROVIDERS:
            continue
        rates: dict[str, float] = {}
        for field, category in LITELLM_RATE_FIELDS.items():
            value = entry.get(field)
            if isinstance(value, (int, float)):
                rates[category] = float(value) * 1_000_000.0
        if not rates:
            continue
        short_name = name.removeprefix("openai/").removeprefix("anthropic/")
        models[short_name] = rates
    return models


class PricingSource:
    """Provides the active pricing table, refreshing it when stale.

    ``source="litellm"`` fetches from the LiteLLM pricing URL; a ``file`` path
    re-reads the file (so mounted ConfigMap updates are picked up) and
    ``source="bundled"`` loads the packaged snapshot once. When a refresh
    fails, the previous table (or the bundled snapshot on first LiteLLM
    failure) is kept and the refresh is retried on the next check.
    """

    def __init__(
        self,
        source: str = "litellm",
        file: str | None = None,
        url: str = LITELLM_PRICING_URL,
        refresh_seconds: int = 86400,
        tier_multipliers: dict[str, float] | None = None,
    ) -> None:
        self._source = "file" if file else source
        self._file = file
        self._url = url
        self._refresh_seconds = refresh_seconds
        self._tier_multipliers = tier_multipliers
        self._pricing: Pricing | None = None
        # None = never successfully refreshed. A 0.0 sentinel would break on
        # freshly booted machines, where time.monotonic() is near zero.
        self._last_refresh: float | None = None

    def current(self) -> Pricing:
        if self._source == "bundled":
            if self._pricing is None:
                self._pricing = Pricing.load(tier_multipliers=self._tier_multipliers)
                self._mark_refreshed("bundled", len(self._pricing))
            return self._pricing

        stale = (
            self._last_refresh is None
            or time.monotonic() - self._last_refresh >= self._refresh_seconds
        )
        if self._pricing is None or stale:
            if self._source == "file":
                self._refresh_file()
            else:
                self._refresh_litellm()
        assert self._pricing is not None
        return self._pricing

    def _refresh_file(self) -> None:
        """Re-read the pricing file so e.g. ConfigMap updates are picked up."""
        assert self._file is not None
        try:
            pricing = Pricing.load(self._file, tier_multipliers=self._tier_multipliers)
        except (OSError, ValueError) as exc:
            if self._pricing is None:
                raise  # fail loudly at startup; a bad file should not go unnoticed
            log.warning(
                "Failed to re-read pricing file %s (%s); "
                "keeping the previous table and retrying next cycle.",
                self._file,
                exc,
            )
            return
        self._pricing = pricing
        self._mark_refreshed("file", len(pricing))

    def _refresh_litellm(self) -> None:
        try:
            response = requests.get(self._url, timeout=30)
            response.raise_for_status()
            models = parse_litellm(response.json())
            if not models:
                raise ValueError("no openai/anthropic models found in pricing data")
        except (requests.RequestException, ValueError) as exc:
            # Leave _last_refresh untouched so the fetch is retried on the
            # next poll cycle rather than after a full refresh interval.
            if self._pricing is None:
                log.warning(
                    "Failed to fetch LiteLLM pricing from %s (%s); "
                    "falling back to the bundled pricing table until a fetch succeeds.",
                    self._url,
                    exc,
                )
                self._pricing = Pricing.load(tier_multipliers=self._tier_multipliers)
                self._set_metrics("bundled", len(self._pricing))
            else:
                log.warning(
                    "Failed to refresh LiteLLM pricing from %s (%s); "
                    "keeping the previous table and retrying next cycle.",
                    self._url,
                    exc,
                )
            return
        self._pricing = Pricing(models, self._tier_multipliers)
        self._mark_refreshed("litellm", len(models))
        log.info("Loaded LiteLLM pricing for %d model(s) from %s.", len(models), self._url)

    def _mark_refreshed(self, source: str, model_count: int) -> None:
        self._last_refresh = time.monotonic()
        self._set_metrics(source, model_count)

    @staticmethod
    def _set_metrics(source: str, model_count: int) -> None:
        PRICING_MODELS.clear()
        PRICING_MODELS.labels(source).set(model_count)
        PRICING_LAST_REFRESH.set(time.time())
