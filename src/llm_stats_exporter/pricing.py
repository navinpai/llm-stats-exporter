"""Pricing table for estimated per-key/per-model cost.

Prices are USD per 1M tokens. The bundled table (``data/pricing.json``) is a
best-effort snapshot; override it with the ``PRICING_FILE`` env var. Model
lookup is exact match first, then longest prefix match (so dated snapshots
like ``gpt-4o-2024-08-06`` resolve to ``gpt-4o``), then ``default`` if set.
"""

from __future__ import annotations

import json
import logging
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)

BILLABLE_CATEGORIES = ("input", "output", "cache_read", "cache_write")


class Pricing:
    def __init__(self, models: dict[str, dict[str, float]]) -> None:
        self._models = models

    @classmethod
    def load(cls, path: str | None = None) -> Pricing:
        if path:
            raw = Path(path).read_text(encoding="utf-8")
        else:
            raw = (
                resources.files("llm_stats_exporter") / "data" / "pricing.json"
            ).read_text(encoding="utf-8")
        data = json.loads(raw)
        models = {
            name: {k: float(v) for k, v in rates.items()}
            for name, rates in data.get("models", {}).items()
        }
        log.info("Loaded pricing for %d model(s)%s.", len(models), f" from {path}" if path else "")
        return cls(models)

    def rates_for(self, model: str) -> dict[str, float] | None:
        if model in self._models:
            return self._models[model]
        best: str | None = None
        for name in self._models:
            if name != "default" and model.startswith(name):
                if best is None or len(name) > len(best):
                    best = name
        if best is not None:
            return self._models[best]
        return self._models.get("default")

    def estimate_usd(self, model: str, tokens: dict[str, float]) -> float | None:
        """Estimated USD cost for a usage record, or None when model is unpriced."""
        rates = self.rates_for(model)
        if rates is None:
            log.debug("No pricing for model %r; skipping estimate.", model)
            return None
        return sum(
            tokens.get(cat, 0.0) / 1_000_000.0 * rates.get(cat, 0.0)
            for cat in BILLABLE_CATEGORIES
        )
