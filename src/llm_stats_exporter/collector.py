"""Poll cycle orchestration.

Each cycle fetches a window from the start of the current month (or further
back, if LOOKBACK_DAYS reaches earlier) so daily metrics and month-to-date
totals come from a single fetch. Metrics are rebuilt from per-provider
snapshots; a failing provider keeps its last good snapshot so its series
don't disappear between scrapes.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

from llm_stats_exporter import metrics
from llm_stats_exporter.pricing import Pricing
from llm_stats_exporter.providers.base import Provider
from llm_stats_exporter.records import Snapshot

log = logging.getLogger(__name__)


def fetch_window(lookback_days: int, now: datetime | None = None) -> tuple[datetime, datetime]:
    """[start, end): from min(month start, today - lookback) to tomorrow 00:00 UTC."""
    now = now or datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today.replace(day=1)
    lookback_start = today - timedelta(days=lookback_days)
    return min(month_start, lookback_start), today + timedelta(days=1)


class Collector:
    def __init__(self, providers: list[Provider], pricing: Pricing, lookback_days: int) -> None:
        self.providers = providers
        self.pricing = pricing
        self.lookback_days = lookback_days
        self._snapshots: dict[str, Snapshot] = {}

    def poll(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        start, end = fetch_window(self.lookback_days, now)
        for provider in self.providers:
            try:
                self._snapshots[provider.name] = provider.fetch(start, end)
                metrics.UP.labels(provider.name).set(1)
                metrics.LAST_SUCCESS.labels(provider.name).set(time.time())
                log.info(
                    "Poll OK for %s: %d usage record(s), %d cost record(s).",
                    provider.name,
                    len(self._snapshots[provider.name].usage),
                    len(self._snapshots[provider.name].costs),
                )
            except Exception:
                metrics.UP.labels(provider.name).set(0)
                metrics.POLL_ERRORS.labels(provider.name).inc()
                log.exception("Poll failed for %s (keeping last snapshot).", provider.name)
        self._rebuild_metrics(now)

    def _rebuild_metrics(self, now: datetime) -> None:
        month = now.strftime("%Y-%m")
        daily_cutoff = (
            now.replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=self.lookback_days)
        ).strftime("%Y-%m-%d")

        metrics.USAGE_TOKENS.clear()
        metrics.REQUESTS.clear()
        metrics.ESTIMATED_COST.clear()
        metrics.BILLED_COST.clear()
        metrics.MONTHLY_ESTIMATED_COST.clear()
        metrics.MONTHLY_BILLED_COST.clear()

        for provider_name, snapshot in self._snapshots.items():
            monthly_estimated: dict[tuple[str, str], float] = {}
            monthly_billed: dict[tuple[str, str], float] = {}

            for record in snapshot.usage:
                estimate = self.pricing.estimate_usd(record.model, record.tokens)
                if estimate is not None and record.date.startswith(month):
                    key = (record.api_key_id, record.api_key_name)
                    monthly_estimated[key] = monthly_estimated.get(key, 0.0) + estimate

                if record.date < daily_cutoff:
                    continue
                labels = (
                    provider_name,
                    record.operation,
                    record.project_id,
                    record.project_name,
                    record.api_key_id,
                    record.api_key_name,
                    record.model,
                    record.date,
                )
                for token_type, count in record.tokens.items():
                    metrics.USAGE_TOKENS.labels(*labels, token_type).set(count)
                if record.requests:
                    metrics.REQUESTS.labels(*labels).set(record.requests)
                if estimate:
                    metrics.ESTIMATED_COST.labels(*labels).set(estimate)

            for cost in snapshot.costs:
                if cost.date.startswith(month):
                    key = (cost.project_id, cost.project_name)
                    monthly_billed[key] = monthly_billed.get(key, 0.0) + cost.amount_usd
                if cost.date < daily_cutoff or not cost.amount_usd:
                    continue
                metrics.BILLED_COST.labels(
                    provider_name, cost.project_id, cost.project_name, cost.line_item, cost.date
                ).set(cost.amount_usd)

            for (key_id, key_name), total in monthly_estimated.items():
                metrics.MONTHLY_ESTIMATED_COST.labels(provider_name, key_id, key_name).set(total)
            for (project_id, project_name), total in monthly_billed.items():
                metrics.MONTHLY_BILLED_COST.labels(provider_name, project_id, project_name).set(
                    total
                )
