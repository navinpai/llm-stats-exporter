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
    assert snapshot.costs == []


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
