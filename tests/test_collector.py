from datetime import UTC, datetime

import pytest
from prometheus_client import REGISTRY

from llm_stats_exporter.collector import Collector, fetch_window
from llm_stats_exporter.pricing import Pricing
from llm_stats_exporter.providers.base import Provider
from llm_stats_exporter.records import (
    ClaudeCodeModelUsage,
    ClaudeCodeRecord,
    CostRecord,
    Snapshot,
    UsageRecord,
)

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

USAGE = UsageRecord(
    date="2026-09-15",
    operation="messages",
    project_id="ws_1",
    project_name="prod",
    api_key_id="key_1",
    api_key_name="backend",
    model="claude-sonnet-4-6",
    tokens={"input": 1_000_000.0, "output": 100_000.0},
    requests=42.0,
)
OLD_USAGE = UsageRecord(
    date="2026-09-01",  # inside the month, outside the 2-day lookback
    operation="messages",
    project_id="ws_1",
    project_name="prod",
    api_key_id="key_1",
    api_key_name="backend",
    model="claude-sonnet-4-6",
    tokens={"input": 1_000_000.0},
)
COST = CostRecord(
    date="2026-09-15", project_id="ws_1", project_name="prod", line_item="li", amount_usd=5.0
)
OLD_COST = CostRecord(
    date="2026-09-01", project_id="ws_1", project_name="prod", line_item="li", amount_usd=7.0
)

CLAUDE_CODE = ClaudeCodeRecord(
    date="2026-09-15",
    actor="dev@example.com",
    actor_type="user",
    sessions=13.0,
    lines_added=120.0,
    lines_removed=30.0,
    commits=4.0,
    pull_requests=1.0,
    models=[
        ClaudeCodeModelUsage(
            model="claude-sonnet-4-6",
            tokens={"input": 540.0, "output": 1312.0},
            estimated_cost_usd=38.66,
        )
    ],
)
OLD_CLAUDE_CODE = ClaudeCodeRecord(
    date="2026-09-01",  # before the 2-day lookback; still exported daily
    actor="dev@example.com",
    actor_type="user",
    sessions=2.0,
    models=[ClaudeCodeModelUsage(model="claude-sonnet-4-6", estimated_cost_usd=10.0)],
)

CC_LABELS = {
    "provider": "fake",
    "account": "default",
    "actor": "dev@example.com",
    "actor_type": "user",
    "date": "2026-09-15",
}

USAGE_LABELS = {
    "provider": "fake",
    "account": "default",
    "operation": "messages",
    "project_id": "ws_1",
    "project_name": "prod",
    "api_key_id": "key_1",
    "api_key_name": "backend",
    "model": "claude-sonnet-4-6",
    "service_tier": "standard",
    "date": "2026-09-15",
}


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, snapshot: Snapshot, account: str = "default") -> None:
        super().__init__(account)
        self.snapshot = snapshot
        self.fail = False

    def fetch(self, start: datetime, end: datetime) -> Snapshot:
        if self.fail:
            raise RuntimeError("boom")
        return self.snapshot


@pytest.fixture
def pricing():
    # input $3/M, output $15/M
    return Pricing({"claude-sonnet-4-6": {"input": 3.0, "output": 15.0}})


def sample(name, labels):
    return REGISTRY.get_sample_value(name, labels)


def test_fetch_window_covers_month_start():
    start, end = fetch_window(2, NOW)
    assert start == datetime(2026, 9, 1, tzinfo=UTC)
    assert end == datetime(2026, 9, 16, tzinfo=UTC)


def test_fetch_window_lookback_crosses_month_boundary():
    start, _ = fetch_window(5, datetime(2026, 9, 3, tzinfo=UTC))
    assert start == datetime(2026, 8, 29, tzinfo=UTC)


def test_poll_exports_daily_and_monthly_metrics(pricing):
    provider = FakeProvider(Snapshot(usage=[USAGE, OLD_USAGE], costs=[COST, OLD_COST]))
    collector = Collector([provider], pricing, lookback_days=2)
    collector.poll(NOW)

    assert sample("llm_usage_tokens", {**USAGE_LABELS, "token_type": "input"}) == 1_000_000.0
    assert sample("llm_usage_tokens", {**USAGE_LABELS, "token_type": "output"}) == 100_000.0
    assert sample("llm_requests", USAGE_LABELS) == 42.0
    # 1M input * $3/M + 0.1M output * $15/M
    assert sample("llm_estimated_cost_usd", USAGE_LABELS) == pytest.approx(4.5)
    assert (
        sample(
            "llm_cost_usd",
            {
                "provider": "fake",
                "account": "default",
                "project_id": "ws_1",
                "project_name": "prod",
                "line_item": "li",
                "date": "2026-09-15",
            },
        )
        == 5.0
    )

    # OLD_* records are outside the lookback: no daily series for 2026-09-01 ...
    assert (
        sample("llm_usage_tokens", {**USAGE_LABELS, "date": "2026-09-01", "token_type": "input"})
        is None
    )
    # ... but they still count toward month-to-date totals (4.5 + 3.0, 5 + 7).
    assert sample(
        "llm_monthly_estimated_cost_usd",
        {
            "provider": "fake",
            "account": "default",
            "api_key_id": "key_1",
            "api_key_name": "backend",
        },
    ) == pytest.approx(7.5)
    assert (
        sample(
            "llm_monthly_cost_usd",
            {
                "provider": "fake",
                "account": "default",
                "project_id": "ws_1",
                "project_name": "prod",
            },
        )
        == 12.0
    )

    assert sample("llm_exporter_up", {"provider": "fake", "account": "default"}) == 1.0


def test_failed_poll_keeps_last_snapshot(pricing):
    provider = FakeProvider(Snapshot(usage=[USAGE], costs=[COST]))
    collector = Collector([provider], pricing, lookback_days=2)
    collector.poll(NOW)

    provider.fail = True
    up_labels = {"provider": "fake", "account": "default"}
    errors_before = sample("llm_exporter_poll_errors_total", up_labels) or 0.0
    collector.poll(NOW)

    assert sample("llm_exporter_up", up_labels) == 0.0
    assert sample("llm_exporter_poll_errors_total", up_labels) == errors_before + 1
    # Series from the last good snapshot are still exported.
    assert sample("llm_usage_tokens", {**USAGE_LABELS, "token_type": "input"}) == 1_000_000.0


def test_unknown_costs_keep_previous_cost_records(pricing):
    provider = FakeProvider(Snapshot(usage=[USAGE], costs=[COST]))
    collector = Collector([provider], pricing, lookback_days=2)
    collector.poll(NOW)

    provider.snapshot = Snapshot(usage=[USAGE], costs=None)
    up_labels = {"provider": "fake", "account": "default"}
    errors_before = sample("llm_exporter_poll_errors_total", up_labels) or 0.0
    collector.poll(NOW)

    # Usage stays fresh, the poll counts as up, but the failure is visible.
    assert sample("llm_exporter_up", up_labels) == 1.0
    assert sample("llm_exporter_poll_errors_total", up_labels) == errors_before + 1
    # Cost series come from the last snapshot with known costs.
    cost_labels = {
        "provider": "fake",
        "account": "default",
        "project_id": "ws_1",
        "project_name": "prod",
        "line_item": "li",
        "date": "2026-09-15",
    }
    assert sample("llm_cost_usd", cost_labels) == 5.0
    assert (
        sample(
            "llm_monthly_cost_usd",
            {k: v for k, v in cost_labels.items() if k not in ("line_item", "date")},
        )
        == 5.0
    )


def test_unknown_costs_with_no_previous_snapshot_exports_none(pricing):
    provider = FakeProvider(Snapshot(usage=[USAGE], costs=None))
    collector = Collector([provider], pricing, lookback_days=2)
    collector.poll(NOW)

    assert sample("llm_exporter_up", {"provider": "fake", "account": "default"}) == 1.0
    assert sample("llm_usage_tokens", {**USAGE_LABELS, "token_type": "input"}) == 1_000_000.0
    assert (
        sample(
            "llm_cost_usd",
            {
                "provider": "fake",
                "account": "default",
                "project_id": "ws_1",
                "project_name": "prod",
                "line_item": "li",
                "date": "2026-09-15",
            },
        )
        is None
    )


def test_claude_code_daily_and_monthly_metrics(pricing):
    provider = FakeProvider(
        Snapshot(usage=[], costs=[], claude_code=[CLAUDE_CODE, OLD_CLAUDE_CODE])
    )
    collector = Collector([provider], pricing, lookback_days=2)
    collector.poll(NOW)

    assert sample("llm_claude_code_sessions", CC_LABELS) == 13.0
    assert sample("llm_claude_code_lines_of_code", {**CC_LABELS, "type": "added"}) == 120.0
    assert sample("llm_claude_code_lines_of_code", {**CC_LABELS, "type": "removed"}) == 30.0
    assert sample("llm_claude_code_commits", CC_LABELS) == 4.0
    assert sample("llm_claude_code_pull_requests", CC_LABELS) == 1.0
    assert (
        sample(
            "llm_claude_code_tokens",
            {**CC_LABELS, "model": "claude-sonnet-4-6", "token_type": "input"},
        )
        == 540.0
    )
    assert sample(
        "llm_claude_code_estimated_cost_usd", {**CC_LABELS, "model": "claude-sonnet-4-6"}
    ) == pytest.approx(38.66)

    # Claude Code daily series ignore the lookback cutoff: every fetched
    # day is exported so dashboards can show the full 30-day window.
    assert sample("llm_claude_code_sessions", {**CC_LABELS, "date": "2026-09-01"}) == 2.0
    assert sample(
        "llm_claude_code_monthly_estimated_cost_usd",
        {k: v for k, v in CC_LABELS.items() if k != "date"},
    ) == pytest.approx(48.66)


def test_unknown_claude_code_keeps_previous_records(pricing):
    provider = FakeProvider(Snapshot(usage=[USAGE], costs=[], claude_code=[CLAUDE_CODE]))
    collector = Collector([provider], pricing, lookback_days=2)
    collector.poll(NOW)

    provider.snapshot = Snapshot(usage=[USAGE], costs=[], claude_code=None)
    up_labels = {"provider": "fake", "account": "default"}
    errors_before = sample("llm_exporter_poll_errors_total", up_labels) or 0.0
    collector.poll(NOW)

    assert sample("llm_exporter_up", up_labels) == 1.0
    assert sample("llm_exporter_poll_errors_total", up_labels) == errors_before + 1
    assert sample("llm_claude_code_sessions", CC_LABELS) == 13.0


def test_multiple_accounts_export_distinct_series(pricing):
    default_account = FakeProvider(Snapshot(usage=[USAGE], costs=[]))
    prod_account = FakeProvider(Snapshot(usage=[USAGE], costs=[]), account="prod")
    collector = Collector([default_account, prod_account], pricing, lookback_days=2)
    collector.poll(NOW)

    assert sample("llm_usage_tokens", {**USAGE_LABELS, "token_type": "input"}) == 1_000_000.0
    assert (
        sample("llm_usage_tokens", {**USAGE_LABELS, "account": "prod", "token_type": "input"})
        == 1_000_000.0
    )
    assert sample("llm_exporter_up", {"provider": "fake", "account": "prod"}) == 1.0
