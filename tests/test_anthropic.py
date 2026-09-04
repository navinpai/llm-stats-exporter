from datetime import UTC, datetime

import responses

from llm_stats_exporter.providers.anthropic import AnthropicProvider, extract_tokens

API = "https://api.anthropic.com"


def test_extract_tokens_maps_nested_cache_fields():
    result = {
        "api_key_id": "apikey_1",
        "uncached_input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 30,
        "cache_creation": {"ephemeral_5m_input_tokens": 20, "ephemeral_1h_input_tokens": 5},
    }
    assert extract_tokens(result) == {
        "input": 100.0,
        "output": 50.0,
        "cache_read": 30.0,
        "cache_write": 25.0,
    }


@responses.activate
def test_fetch_normalizes_usage_and_costs():
    responses.get(
        f"{API}/v1/organizations/api_keys",
        json={"data": [{"id": "apikey_1", "name": "prod-key"}], "has_more": False},
    )
    responses.get(
        f"{API}/v1/organizations/workspaces",
        json={"data": [{"id": "ws_1", "name": "production"}], "has_more": False},
    )
    responses.get(
        f"{API}/v1/organizations/usage_report/messages",
        json={
            "data": [
                {
                    "starting_at": "2026-09-02T00:00:00Z",
                    "results": [
                        {
                            "api_key_id": "apikey_1",
                            "workspace_id": "ws_1",
                            "model": "claude-sonnet-4-6",
                            "service_tier": "batch",
                            "uncached_input_tokens": 1000,
                            "output_tokens": 200,
                            "cache_read_input_tokens": 500,
                            "cache_creation": {"ephemeral_5m_input_tokens": 100},
                        },
                        {"api_key_id": "apikey_2", "model": "claude-haiku-4-5"},
                    ],
                }
            ],
            "has_more": False,
        },
    )
    responses.get(
        f"{API}/v1/organizations/cost_report",
        json={
            "data": [
                {
                    "starting_at": "2026-09-02T00:00:00Z",
                    "results": [
                        {
                            "workspace_id": "ws_1",
                            "description": "Claude Sonnet 4.6 Usage",
                            "amount": "1234.5",
                        }
                    ],
                }
            ],
            "has_more": False,
        },
    )

    provider = AnthropicProvider("sk-ant-admin-test")
    snapshot = provider.fetch(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 4, tzinfo=UTC))

    assert len(snapshot.usage) == 1  # all-zero record dropped
    usage = snapshot.usage[0]
    assert usage.date == "2026-09-02"
    assert usage.operation == "messages"
    assert usage.api_key_name == "prod-key"
    assert usage.project_id == "ws_1"
    assert usage.project_name == "production"
    assert usage.model == "claude-sonnet-4-6"
    assert usage.tokens == {
        "input": 1000.0,
        "output": 200.0,
        "cache_read": 500.0,
        "cache_write": 100.0,
    }
    assert usage.service_tier == "batch"

    usage_call = next(call for call in responses.calls if "/usage_report/" in call.request.url)
    assert "service_tier" in usage_call.request.url

    assert len(snapshot.costs) == 1
    cost = snapshot.costs[0]
    assert cost.amount_usd == 12.345  # cents -> USD
    assert cost.project_name == "production"
    assert cost.line_item == "Claude Sonnet 4.6 Usage"


@responses.activate
def test_fetch_survives_cost_endpoint_failure():
    responses.get(f"{API}/v1/organizations/api_keys", json={"data": [], "has_more": False})
    responses.get(f"{API}/v1/organizations/workspaces", json={"data": [], "has_more": False})
    responses.get(
        f"{API}/v1/organizations/usage_report/messages",
        json={"data": [], "has_more": False},
    )
    responses.get(f"{API}/v1/organizations/cost_report", status=403)

    provider = AnthropicProvider("sk-ant-admin-test")
    snapshot = provider.fetch(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 4, tzinfo=UTC))
    assert snapshot.costs is None


CLAUDE_CODE_ITEM = {
    "date": "2026-09-02T00:00:00Z",
    "actor": {"type": "user_actor", "email_address": "dev@example.com"},
    "customer_type": "api",
    "core_metrics": {
        "num_sessions": 13,
        "lines_of_code": {"added": 120, "removed": 30},
        "commits_by_claude_code": 4,
        "pull_requests_by_claude_code": 1,
    },
    "model_breakdown": [
        {
            "model": "claude-sonnet-4-6",
            "tokens": {"input": 540, "output": 1312, "cache_read": 155898, "cache_creation": 9498},
            "estimated_cost": {"currency": "USD", "amount": 3866},
        }
    ],
}


def _mock_base_endpoints():
    responses.get(f"{API}/v1/organizations/api_keys", json={"data": [], "has_more": False})
    responses.get(f"{API}/v1/organizations/workspaces", json={"data": [], "has_more": False})
    responses.get(
        f"{API}/v1/organizations/usage_report/messages", json={"data": [], "has_more": False}
    )
    responses.get(f"{API}/v1/organizations/cost_report", json={"data": [], "has_more": False})


@responses.activate
def test_claude_code_report_normalizes_actors_and_costs():
    _mock_base_endpoints()
    responses.get(
        f"{API}/v1/organizations/usage_report/claude_code",
        json={"data": [CLAUDE_CODE_ITEM], "has_more": False},
    )

    provider = AnthropicProvider("sk-ant-admin-test", claude_code_days=0)
    snapshot = provider.fetch(datetime(2026, 9, 2, tzinfo=UTC), datetime(2026, 9, 3, tzinfo=UTC))

    assert snapshot.claude_code is not None
    assert len(snapshot.claude_code) == 1
    record = snapshot.claude_code[0]
    assert record.date == "2026-09-02"
    assert record.actor == "dev@example.com"
    assert record.actor_type == "user"
    assert record.sessions == 13.0
    assert record.lines_added == 120.0
    assert record.lines_removed == 30.0
    assert record.commits == 4.0
    assert record.pull_requests == 1.0
    assert len(record.models) == 1
    usage = record.models[0]
    assert usage.model == "claude-sonnet-4-6"
    assert usage.tokens == {
        "input": 540.0,
        "output": 1312.0,
        "cache_read": 155898.0,
        "cache_write": 9498.0,
    }
    assert usage.estimated_cost_usd == 38.66  # cents -> USD

    cc_calls = [c for c in responses.calls if "/claude_code" in c.request.url]
    assert len(cc_calls) == 1  # one call per day in the window
    assert "starting_at=2026-09-02" in cc_calls[0].request.url


@responses.activate
def test_claude_code_report_disabled_after_403():
    _mock_base_endpoints()
    responses.get(f"{API}/v1/organizations/usage_report/claude_code", status=403)

    provider = AnthropicProvider("sk-ant-admin-test", claude_code_days=0)
    window = (datetime(2026, 9, 2, tzinfo=UTC), datetime(2026, 9, 3, tzinfo=UTC))
    assert provider.fetch(*window).claude_code == []
    assert provider.fetch(*window).claude_code == []
    cc_calls = [c for c in responses.calls if "/claude_code" in c.request.url]
    assert len(cc_calls) == 1  # not retried once marked unsupported


@responses.activate
def test_claude_code_report_transient_failure_returns_none():
    _mock_base_endpoints()
    responses.get(f"{API}/v1/organizations/usage_report/claude_code", status=500)

    provider = AnthropicProvider("sk-ant-admin-test", claude_code_days=0)
    snapshot = provider.fetch(datetime(2026, 9, 2, tzinfo=UTC), datetime(2026, 9, 3, tzinfo=UTC))
    assert snapshot.claude_code is None


@responses.activate
def test_claude_code_report_caches_stable_days():
    _mock_base_endpoints()
    responses.get(
        f"{API}/v1/organizations/usage_report/claude_code",
        json={"data": [CLAUDE_CODE_ITEM], "has_more": False},
    )

    provider = AnthropicProvider("sk-ant-admin-test", claude_code_days=0)
    window = (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 5, tzinfo=UTC))
    first = provider.fetch(*window).claude_code
    calls_after_first = len([c for c in responses.calls if "/claude_code" in c.request.url])
    second = provider.fetch(*window).claude_code
    calls_after_second = len([c for c in responses.calls if "/claude_code" in c.request.url])

    assert calls_after_first == 4  # one per day
    # 2026-09-01 is stable (>2 days behind end) and served from cache; the
    # trailing days are refetched.
    assert calls_after_second == 7
    assert first == second


@responses.activate
def test_claude_code_report_covers_trailing_30_days():
    _mock_base_endpoints()
    responses.get(
        f"{API}/v1/organizations/usage_report/claude_code",
        json={"data": [], "has_more": False},
    )

    provider = AnthropicProvider("sk-ant-admin-test")
    provider.fetch(datetime(2026, 9, 2, tzinfo=UTC), datetime(2026, 9, 3, tzinfo=UTC))

    cc_calls = [c for c in responses.calls if "/claude_code" in c.request.url]
    assert len(cc_calls) == 31  # window extends past the fetch start
    assert "starting_at=2026-08-03" in cc_calls[0].request.url
    assert "starting_at=2026-09-02" in cc_calls[-1].request.url


@responses.activate
def test_usage_pagination_follows_next_page():
    responses.get(f"{API}/v1/organizations/api_keys", json={"data": [], "has_more": False})
    responses.get(f"{API}/v1/organizations/workspaces", json={"data": [], "has_more": False})
    bucket = {
        "starting_at": "2026-09-02T00:00:00Z",
        "results": [{"api_key_id": "k", "model": "m", "uncached_input_tokens": 1}],
    }
    responses.get(
        f"{API}/v1/organizations/usage_report/messages",
        json={"data": [bucket], "has_more": True, "next_page": "page_2"},
    )
    responses.get(
        f"{API}/v1/organizations/usage_report/messages",
        json={"data": [bucket], "has_more": False},
    )
    responses.get(f"{API}/v1/organizations/cost_report", json={"data": [], "has_more": False})

    provider = AnthropicProvider("sk-ant-admin-test")
    snapshot = provider.fetch(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 4, tzinfo=UTC))
    assert len(snapshot.usage) == 2
    assert snapshot.usage[0].service_tier == "standard"  # default when absent
