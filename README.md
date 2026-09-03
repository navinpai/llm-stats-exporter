# llm-stats-exporter

A Prometheus exporter for LLM API usage and cost. Polls the **OpenAI** and/or
**Anthropic** org-level admin APIs and exposes unified `llm_*` metrics —
tokens, requests, billed cost, and estimated cost — split by account, API key,
model, project/workspace, and day. Multiple accounts per provider are supported.

Inspired by [claude-api-exporter](https://github.com/alacava/claude-api-exporter)
and [openai-exporter](https://github.com/foxdalas/openai-exporter), but with a
single metric namespace so cross-provider dashboards and cost rollups are trivial.

## Metrics

| Metric | Type | Labels | Description |
|---|---|---|---|
| `llm_usage_tokens` | Gauge | `provider, account, operation, project_id, project_name, api_key_id, api_key_name, model, date, token_type` | Tokens in the daily bucket. `token_type` is one of `input`, `output`, `cache_read`, `cache_write`, `input_audio`, `output_audio`. `input` is always **uncached** input; `input + cache_read` = total prompt tokens. |
| `llm_requests` | Gauge | `provider, account, operation, project_id, project_name, api_key_id, api_key_name, model, date` | Model requests in the daily bucket (OpenAI only; Anthropic's usage API doesn't report it). |
| `llm_estimated_cost_usd` | Gauge | same as `llm_requests` | Estimated USD cost from token counts × pricing table. This is the only per-key/per-model cost signal, since provider cost APIs don't break down by key. |
| `llm_cost_usd` | Gauge | `provider, account, project_id, project_name, line_item, date` | Billed USD cost from the provider's cost API. Anthropic workspaces map to `project_*`; Anthropic `description` and OpenAI `line_item` map to `line_item`. |
| `llm_monthly_estimated_cost_usd` | Gauge | `provider, account, api_key_id, api_key_name` | Month-to-date estimated cost per API key. |
| `llm_monthly_cost_usd` | Gauge | `provider, account, project_id, project_name` | Month-to-date billed cost per project/workspace. |
| `llm_exporter_up` | Gauge | `provider, account` | 1 if the last poll for the provider account succeeded. |
| `llm_exporter_last_success_timestamp_seconds` | Gauge | `provider, account` | Unix time of last successful poll. |
| `llm_exporter_poll_errors_total` | Counter | `provider, account` | Failed poll cycles. |
| `llm_pricing_models` | Gauge | `source` | Models in the active pricing table (`source` is `litellm`, `bundled`, or `file`). |
| `llm_pricing_last_refresh_timestamp_seconds` | Gauge | — | Unix time of the last successful pricing load/refresh. |
| `llm_exporter_build_info` | Gauge | `version` | Always 1; exporter version. |

Daily metrics use a `date` label (`YYYY-MM-DD`) and are re-set every poll for
the lookback window, so late-arriving usage is corrected in place. If a
provider poll fails, its last good snapshot is kept and `llm_exporter_up` drops to 0.

## Requirements

These are **organization admin** APIs — regular inference keys will not work:

- OpenAI: an **admin key** (`sk-admin-...`) from [platform settings](https://platform.openai.com/settings/organization/admin-keys).
- Anthropic: an **admin key** (`sk-ant-admin...`) from the [console](https://console.anthropic.com/settings/admin-keys).

Configure one or both providers.

## Configuration

All configuration is via environment variables. Every secret can be supplied
directly or via a `*_FILE` variant pointing at a file (e.g. a mounted
Kubernetes Secret) — set one or the other, not both.

| Variable | Default | Description |
|---|---|---|
| `OPENAI_ADMIN_KEY` / `OPENAI_ADMIN_KEY_FILE` | — | OpenAI admin key for the `default` account (enables the OpenAI provider). |
| `ANTHROPIC_ADMIN_KEY` / `ANTHROPIC_ADMIN_KEY_FILE` | — | Anthropic admin key for the `default` account (enables the Anthropic provider). |
| `OPENAI_ADMIN_KEY_<NAME>` / `..._FILE` | — | Additional OpenAI accounts (see below). |
| `ANTHROPIC_ADMIN_KEY_<NAME>` / `..._FILE` | — | Additional Anthropic accounts (see below). |
| `EXPORTER_PORT` | `9184` | Port for the `/metrics` endpoint. |
| `POLL_INTERVAL_SECONDS` | `300` | How often to poll the provider APIs. |
| `LOOKBACK_DAYS` | `2` | Days of daily buckets to (re-)export each poll. |
| `PRICING_SOURCE` | `litellm` | `litellm` (fetch community pricing, refresh periodically) or `bundled` (offline snapshot). |
| `PRICING_FILE` | — | Path to a custom pricing JSON (see below); takes precedence over `PRICING_SOURCE`. |
| `PRICING_URL` | LiteLLM main | Override URL for the LiteLLM pricing JSON. |
| `PRICING_REFRESH_SECONDS` | `86400` | How often to re-fetch LiteLLM pricing. |
| `LOG_LEVEL` | `INFO` | Python log level. |
| `OPENAI_API_BASE` | `https://api.openai.com` | Override for testing/proxies. |
| `ANTHROPIC_API_BASE` | `https://api.anthropic.com` | Override for testing/proxies. |

### Multiple accounts

To scrape several organizations of the same provider from one exporter, add
named key variables. The suffix (lowercased) becomes the `account` label on
every metric; the unnamed key maps to `account="default"`:

```sh
ANTHROPIC_ADMIN_KEY=sk-ant-admin...           # account="default"
ANTHROPIC_ADMIN_KEY_PROD=sk-ant-admin...      # account="prod"
OPENAI_ADMIN_KEY_TEAM_A_FILE=/secrets/team-a  # account="team_a"
```

```promql
sum by (provider, account) (llm_monthly_cost_usd)
```

### Pricing

`llm_estimated_cost_usd` multiplies token counts by a pricing table
(USD per 1M tokens). By default the table is fetched from
[LiteLLM's community-maintained pricing JSON](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json)
(OpenAI and Anthropic models only) and refreshed every `PRICING_REFRESH_SECONDS`.
If the fetch fails, the exporter logs a warning, falls back to the bundled
best-effort snapshot (or keeps the last good table), and retries on the next
poll cycle. Set `PRICING_SOURCE=bundled` for air-gapped deployments, or
provide your own table with `PRICING_FILE`:

```json
{
  "models": {
    "gpt-4o": {"input": 2.5, "output": 10.0, "cache_read": 1.25},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.3},
    "default": {"input": 3.0, "output": 15.0}
  }
}
```

Model names match exactly first, then by longest prefix (`gpt-4o-2024-08-06`
→ `gpt-4o`), then fall back to `default` if present.

## Running

### Docker

```sh
docker run -p 9184:9184 \
  -e OPENAI_ADMIN_KEY=sk-admin-... \
  -e ANTHROPIC_ADMIN_KEY=sk-ant-admin... \
  ghcr.io/navinpai/llm-stats-exporter:latest
```

### From source (uv)

```sh
uv sync
OPENAI_ADMIN_KEY=sk-admin-... uv run llm-stats-exporter
curl localhost:9184/metrics
```

### Kubernetes

Example manifests live in [`deploy/`](deploy/): a Deployment consuming the key
from a Secret via `secretKeyRef`, a variant using a Secret mounted as a file
with `*_FILE`, and a `ServiceMonitor` for the Prometheus Operator.

### Prometheus scrape config

```yaml
scrape_configs:
  - job_name: llm-stats-exporter
    scrape_interval: 60s
    static_configs:
      - targets: ["llm-stats-exporter:9184"]
```

## Example queries

```promql
# Month-to-date spend per provider
sum by (provider) (llm_monthly_cost_usd)

# Estimated cost per API key, today
sum by (provider, api_key_name) (llm_estimated_cost_usd{date="2026-09-03"})

# Cache hit ratio per model
sum by (model) (llm_usage_tokens{token_type="cache_read"})
  / sum by (model) (llm_usage_tokens{token_type=~"input|cache_read"})
```

## Development

```sh
uv sync                 # install deps + dev tools
uv run ruff check .     # lint
uv run ruff format .    # format
uv run mypy             # type-check
uv run pytest           # tests
```

## License

[MIT](LICENSE)
