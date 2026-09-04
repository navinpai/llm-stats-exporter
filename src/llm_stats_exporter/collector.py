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
from dataclasses import replace
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
        self._snapshots: dict[tuple[str, str], Snapshot] = {}

    def poll(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        start, end = fetch_window(self.lookback_days, now)
        for provider in self.providers:
            key = (provider.name, provider.account)
            try:
                snapshot = provider.fetch(start, end)
                if snapshot.costs is None:
                    previous = self._snapshots.get(key)
                    kept = previous.costs or [] if previous else []
                    snapshot = replace(snapshot, costs=kept)
                    metrics.POLL_ERRORS.labels(*key).inc()
                    log.warning(
                        "Costs unavailable for %s/%s; keeping %d previous cost record(s).",
                        provider.name,
                        provider.account,
                        len(kept),
                    )
                if snapshot.claude_code is None:
                    previous = self._snapshots.get(key)
                    kept_cc = previous.claude_code or [] if previous else []
                    snapshot = replace(snapshot, claude_code=kept_cc)
                    metrics.POLL_ERRORS.labels(*key).inc()
                    log.warning(
                        "Claude Code data unavailable for %s/%s; keeping %d previous record(s).",
                        provider.name,
                        provider.account,
                        len(kept_cc),
                    )
                self._snapshots[key] = snapshot
                metrics.UP.labels(*key).set(1)
                metrics.LAST_SUCCESS.labels(*key).set(time.time())
                log.info(
                    "Poll OK for %s/%s: %d usage record(s), %d cost record(s).",
                    provider.name,
                    provider.account,
                    len(snapshot.usage),
                    len(snapshot.costs or []),
                )
            except Exception:
                metrics.UP.labels(*key).set(0)
                metrics.POLL_ERRORS.labels(*key).inc()
                log.exception(
                    "Poll failed for %s/%s (keeping last snapshot).",
                    provider.name,
                    provider.account,
                )
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
        metrics.CLAUDE_CODE_SESSIONS.clear()
        metrics.CLAUDE_CODE_LINES.clear()
        metrics.CLAUDE_CODE_COMMITS.clear()
        metrics.CLAUDE_CODE_PULL_REQUESTS.clear()
        metrics.CLAUDE_CODE_TOKENS.clear()
        metrics.CLAUDE_CODE_COST.clear()
        metrics.CLAUDE_CODE_MONTHLY_COST.clear()

        for (provider_name, account), snapshot in self._snapshots.items():
            monthly_estimated: dict[tuple[str, str], float] = {}
            monthly_billed: dict[tuple[str, str], float] = {}

            for record in snapshot.usage:
                estimate = self.pricing.estimate_usd(
                    record.model, record.tokens, record.service_tier
                )
                if estimate is not None and record.date.startswith(month):
                    key = (record.api_key_id, record.api_key_name)
                    monthly_estimated[key] = monthly_estimated.get(key, 0.0) + estimate

                if record.date < daily_cutoff:
                    continue
                labels = (
                    provider_name,
                    account,
                    record.operation,
                    record.project_id,
                    record.project_name,
                    record.api_key_id,
                    record.api_key_name,
                    record.model,
                    record.service_tier,
                    record.date,
                )
                for token_type, count in record.tokens.items():
                    metrics.USAGE_TOKENS.labels(*labels, token_type).set(count)
                if record.requests:
                    metrics.REQUESTS.labels(*labels).set(record.requests)
                if estimate:
                    metrics.ESTIMATED_COST.labels(*labels).set(estimate)

            for cost in snapshot.costs or []:
                if cost.date.startswith(month):
                    key = (cost.project_id, cost.project_name)
                    monthly_billed[key] = monthly_billed.get(key, 0.0) + cost.amount_usd
                if cost.date < daily_cutoff or not cost.amount_usd:
                    continue
                metrics.BILLED_COST.labels(
                    provider_name,
                    account,
                    cost.project_id,
                    cost.project_name,
                    cost.line_item,
                    cost.date,
                ).set(cost.amount_usd)

            self._export_claude_code(provider_name, account, snapshot, month)

            for (key_id, key_name), total in monthly_estimated.items():
                metrics.MONTHLY_ESTIMATED_COST.labels(provider_name, account, key_id, key_name).set(
                    total
                )
            for (project_id, project_name), total in monthly_billed.items():
                metrics.MONTHLY_BILLED_COST.labels(
                    provider_name, account, project_id, project_name
                ).set(total)

    def _export_claude_code(
        self, provider_name: str, account: str, snapshot: Snapshot, month: str
    ) -> None:
        """Claude Code records are exported for every fetched day (the provider
        fetches a trailing 30-day window), not just the daily lookback."""
        monthly: dict[tuple[str, str], float] = {}
        for cc in snapshot.claude_code or []:
            cost_usd = sum(m.estimated_cost_usd for m in cc.models)
            if cost_usd and cc.date.startswith(month):
                actor_key = (cc.actor, cc.actor_type)
                monthly[actor_key] = monthly.get(actor_key, 0.0) + cost_usd
            labels = (provider_name, account, cc.actor, cc.actor_type, cc.date)
            if cc.sessions:
                metrics.CLAUDE_CODE_SESSIONS.labels(*labels).set(cc.sessions)
            if cc.lines_added:
                metrics.CLAUDE_CODE_LINES.labels(*labels, "added").set(cc.lines_added)
            if cc.lines_removed:
                metrics.CLAUDE_CODE_LINES.labels(*labels, "removed").set(cc.lines_removed)
            if cc.commits:
                metrics.CLAUDE_CODE_COMMITS.labels(*labels).set(cc.commits)
            if cc.pull_requests:
                metrics.CLAUDE_CODE_PULL_REQUESTS.labels(*labels).set(cc.pull_requests)
            for usage in cc.models:
                for token_type, count in usage.tokens.items():
                    metrics.CLAUDE_CODE_TOKENS.labels(*labels, usage.model, token_type).set(count)
                if usage.estimated_cost_usd:
                    metrics.CLAUDE_CODE_COST.labels(*labels, usage.model).set(
                        usage.estimated_cost_usd
                    )
        for (actor, actor_type), total in monthly.items():
            metrics.CLAUDE_CODE_MONTHLY_COST.labels(provider_name, account, actor, actor_type).set(
                total
            )
