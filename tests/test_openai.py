from datetime import UTC, datetime

import responses

from llm_stats_exporter.providers.openai import (
    OpenAIProvider,
    extract_tokens,
    normalize_service_tier,
)

API = "https://api.openai.com"
EMPTY = {"data": [], "has_more": False}


def test_extract_tokens_normalizes_input_to_uncached():
    result = {
        "input_tokens": 1000,
        "input_cached_tokens": 400,
        "output_tokens": 200,
        "input_audio_tokens": 10,
        "output_audio_tokens": 5,
    }
    assert extract_tokens(result) == {
        "input": 600.0,
        "cache_read": 400.0,
        "output": 200.0,
        "input_audio": 10.0,
        "output_audio": 5.0,
    }


def test_normalize_service_tier():
    assert normalize_service_tier({}) == "standard"
    assert normalize_service_tier({"service_tier": "default"}) == "standard"
    assert normalize_service_tier({"service_tier": "flex"}) == "flex"
    assert normalize_service_tier({"service_tier": "priority"}) == "priority"
    # The Batch API flag wins over service_tier.
    assert normalize_service_tier({"batch": True, "service_tier": "default"}) == "batch"


def _mock_static_endpoints():
    responses.get(
        f"{API}/v1/organization/projects",
        json={"data": [{"id": "proj_1", "name": "my-project"}], "has_more": False},
    )
    responses.get(f"{API}/v1/organization/usage/embeddings", json=EMPTY)
    responses.get(f"{API}/v1/organization/usage/moderations", json=EMPTY)


@responses.activate
def test_fetch_normalizes_usage_and_costs():
    _mock_static_endpoints()
    responses.get(
        f"{API}/v1/organization/usage/completions",
        json={
            "data": [
                {
                    "start_time": 1788307200,  # 2026-09-02 UTC
                    "results": [
                        {
                            "project_id": "proj_1",
                            "api_key_id": "key_1",
                            "model": "gpt-4o",
                            "input_tokens": 1000,
                            "input_cached_tokens": 400,
                            "output_tokens": 200,
                            "num_model_requests": 7,
                        },
                        {
                            "project_id": "proj_1",
                            "api_key_id": "key_1",
                            "model": "gpt-4o",
                            "batch": True,
                            "input_tokens": 5000,
                            "num_model_requests": 1,
                        },
                    ],
                }
            ],
            "has_more": False,
        },
    )
    responses.get(
        f"{API}/v1/organization/projects/proj_1/api_keys/key_1",
        json={"id": "key_1", "name": "backend-key"},
    )
    responses.get(
        f"{API}/v1/organization/costs",
        json={
            "data": [
                {
                    "start_time": 1788307200,
                    "results": [
                        {
                            "project_id": "proj_1",
                            "line_item": "gpt-4o, input",
                            "amount": {"value": 1.25, "currency": "usd"},
                        }
                    ],
                }
            ],
            "has_more": False,
        },
    )

    provider = OpenAIProvider("sk-admin-test")
    snapshot = provider.fetch(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 4, tzinfo=UTC))

    assert len(snapshot.usage) == 2
    usage = snapshot.usage[0]
    assert usage.date == "2026-09-02"
    assert usage.operation == "completions"
    assert usage.project_name == "my-project"
    assert usage.api_key_name == "backend-key"
    assert usage.model == "gpt-4o"
    assert usage.requests == 7
    assert usage.tokens == {"input": 600.0, "cache_read": 400.0, "output": 200.0}
    assert usage.service_tier == "standard"
    assert snapshot.usage[1].service_tier == "batch"

    completions_call = next(
        call for call in responses.calls if "/usage/completions" in call.request.url
    )
    assert "group_by=batch" in completions_call.request.url
    assert "group_by=service_tier" in completions_call.request.url
    embeddings_call = next(
        call for call in responses.calls if "/usage/embeddings" in call.request.url
    )
    assert "group_by=batch" not in embeddings_call.request.url

    assert len(snapshot.costs) == 1
    cost = snapshot.costs[0]
    assert cost.amount_usd == 1.25
    assert cost.line_item == "gpt-4o, input"
    assert cost.project_name == "my-project"


@responses.activate
def test_api_key_name_falls_back_to_id_and_caches():
    _mock_static_endpoints()
    responses.get(
        f"{API}/v1/organization/usage/completions",
        json={
            "data": [
                {
                    "start_time": 1788307200,
                    "results": [
                        {
                            "project_id": "proj_1",
                            "api_key_id": "key_gone",
                            "model": "gpt-4o",
                            "input_tokens": 10,
                        }
                    ],
                }
            ],
            "has_more": False,
        },
    )
    responses.get(f"{API}/v1/organization/projects/proj_1/api_keys/key_gone", status=404)
    responses.get(f"{API}/v1/organization/admin_api_keys/key_gone", status=404)
    responses.get(f"{API}/v1/organization/costs", json=EMPTY)

    provider = OpenAIProvider("sk-admin-test")
    snapshot = provider.fetch(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 4, tzinfo=UTC))
    assert snapshot.usage[0].api_key_name == "key_gone"
    assert provider._api_key_names["key_gone"] == "key_gone"


@responses.activate
def test_fetch_survives_cost_endpoint_failure():
    _mock_static_endpoints()
    responses.get(f"{API}/v1/organization/usage/completions", json=EMPTY)
    responses.get(f"{API}/v1/organization/costs", status=500)

    provider = OpenAIProvider("sk-admin-test")
    snapshot = provider.fetch(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 4, tzinfo=UTC))
    assert snapshot.costs is None
