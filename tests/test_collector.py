from datetime import UTC, datetime

import pytest
from prometheus_client import REGISTRY

from llm_stats_exporter.collector import Collector, fetch_window
from llm_stats_exporter.pricing import Pricing
from llm_stats_exporter.providers.base import Provider
from llm_stats_exporter.records import CostRecord, Snapshot, UsageRecord

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

USAGE_LABELS = {
    "provider": "fake",
    "account": "default",
    "operation": "messages",
    "project_id": "ws_1",
    "project_name": "prod",
    "api_key_id": "key_1",
    "api_key_name": "backend",
    "model": "claude-sonnet-4-6",
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
