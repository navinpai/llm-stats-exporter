# llm-stats-exporter

A Prometheus exporter for LLM API usage and cost. Polls the **OpenAI** and/or
**Anthropic** org-level admin APIs and exposes unified `llm_*` metrics —
tokens, requests, billed cost, and estimated cost — split by API key, model,
project/workspace, and day.

Inspired by [claude-api-exporter](https://github.com/alacava/claude-api-exporter)
and [openai-exporter](https://github.com/foxdalas/openai-exporter), but with a
single metric namespace so cross-provider dashboards and cost rollups are trivial.

## Metrics

| Metric | Type | Labels | Description |
|---|---|---|---|
| `llm_usage_tokens` | Gauge | `provider, operation, project_id, project_name, api_key_id, api_key_name, model, date, token_type` | Tokens in the daily bucket. `token_type` is one of `input`, `output`, `cache_read`, `cache_write`, `input_audio`, `output_audio`. `input` is always **uncached** input; `input + cache_read` = total prompt tokens. |
| `llm_requests` | Gauge | `provider, operation, project_id, project_name, api_key_id, api_key_name, model, date` | Model requests in the daily bucket (OpenAI only; Anthropic's usage API doesn't report it). |
| `llm_estimated_cost_usd` | Gauge | same as `llm_requests` | Estimated USD cost from token counts × pricing table. This is the only per-key/per-model cost signal, since provider cost APIs don't break down by key. |
| `llm_cost_usd` | Gauge | `provider, project_id, project_name, line_item, date` | Billed USD cost from the provider's cost API. Anthropic workspaces map to `project_*`; Anthropic `description` and OpenAI `line_item` map to `line_item`. |
| `llm_monthly_estimated_cost_usd` | Gauge | `provider, api_key_id, api_key_name` | Month-to-date estimated cost per API key. |
| `llm_monthly_cost_usd` | Gauge | `provider, project_id, project_name` | Month-to-date billed cost per project/workspace. |
| `llm_exporter_up` | Gauge | `provider` | 1 if the last poll for the provider succeeded. |
| `llm_exporter_last_success_timestamp_seconds` | Gauge | `provider` | Unix time of last successful poll. |
| `llm_exporter_poll_errors_total` | Counter | `provider` | Failed poll cycles. |

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
| `OPENAI_ADMIN_KEY` / `OPENAI_ADMIN_KEY_FILE` | — | OpenAI admin key (enables the OpenAI provider). |
| `ANTHROPIC_ADMIN_KEY` / `ANTHROPIC_ADMIN_KEY_FILE` | — | Anthropic admin key (enables the Anthropic provider). |
| `EXPORTER_PORT` | `9184` | Port for the `/metrics` endpoint. |
| `POLL_INTERVAL_SECONDS` | `300` | How often to poll the provider APIs. |
| `LOOKBACK_DAYS` | `2` | Days of daily buckets to (re-)export each poll. |
| `PRICING_FILE` | bundled | Path to a custom pricing JSON (see below). |
| `LOG_LEVEL` | `INFO` | Python log level. |
| `OPENAI_API_BASE` | `https://api.openai.com` | Override for testing/proxies. |
| `ANTHROPIC_API_BASE` | `https://api.anthropic.com` | Override for testing/proxies. |

### Pricing

`llm_estimated_cost_usd` multiplies token counts by a pricing table
(USD per 1M tokens). A best-effort table is bundled; **verify it against
current provider pricing** and override with `PRICING_FILE`:

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
