from __future__ import annotations

from prometheus_client import Counter, Gauge

from llm_stats_exporter import __version__

USAGE_LABELS = [
    "provider",
    "account",
    "operation",
    "project_id",
    "project_name",
    "api_key_id",
    "api_key_name",
    "model",
    "service_tier",
    "date",
]

USAGE_TOKENS = Gauge(
    "llm_usage_tokens",
    "Tokens consumed in the daily bucket. 'input' excludes cached tokens; "
    "input + cache_read = total prompt tokens.",
    [*USAGE_LABELS, "token_type"],
)
REQUESTS = Gauge(
    "llm_requests",
    "Number of model requests in the daily bucket (where the provider reports it).",
    USAGE_LABELS,
)
ESTIMATED_COST = Gauge(
    "llm_estimated_cost_usd",
    "Estimated USD cost in the daily bucket, computed from tokens and the pricing table.",
    USAGE_LABELS,
)
BILLED_COST = Gauge(
    "llm_cost_usd",
    "Billed USD cost in the daily bucket, from the provider's cost API.",
    ["provider", "account", "project_id", "project_name", "line_item", "date"],
)
MONTHLY_ESTIMATED_COST = Gauge(
    "llm_monthly_estimated_cost_usd",
    "Estimated USD cost for the current calendar month, per API key.",
    ["provider", "account", "api_key_id", "api_key_name"],
)
MONTHLY_BILLED_COST = Gauge(
    "llm_monthly_cost_usd",
    "Billed USD cost for the current calendar month, per project/workspace.",
    ["provider", "account", "project_id", "project_name"],
)
CLAUDE_CODE_LABELS = ["provider", "account", "actor", "actor_type", "date"]

CLAUDE_CODE_SESSIONS = Gauge(
    "llm_claude_code_sessions",
    "Claude Code sessions in the daily bucket, per org member or API key (Anthropic only).",
    CLAUDE_CODE_LABELS,
)
CLAUDE_CODE_LINES = Gauge(
    "llm_claude_code_lines_of_code",
    "Lines of code added/removed by Claude Code in the daily bucket.",
    [*CLAUDE_CODE_LABELS, "type"],
)
CLAUDE_CODE_COMMITS = Gauge(
    "llm_claude_code_commits",
    "Commits created by Claude Code in the daily bucket.",
    CLAUDE_CODE_LABELS,
)
CLAUDE_CODE_PULL_REQUESTS = Gauge(
    "llm_claude_code_pull_requests",
    "Pull requests created by Claude Code in the daily bucket.",
    CLAUDE_CODE_LABELS,
)
CLAUDE_CODE_TOKENS = Gauge(
    "llm_claude_code_tokens",
    "Tokens consumed by Claude Code in the daily bucket.",
    [*CLAUDE_CODE_LABELS, "model", "token_type"],
)
CLAUDE_CODE_COST = Gauge(
    "llm_claude_code_estimated_cost_usd",
    "Anthropic-estimated USD cost of Claude Code usage in the daily bucket.",
    [*CLAUDE_CODE_LABELS, "model"],
)
CLAUDE_CODE_MONTHLY_COST = Gauge(
    "llm_claude_code_monthly_estimated_cost_usd",
    "Anthropic-estimated USD Claude Code cost for the current calendar month, per actor.",
    ["provider", "account", "actor", "actor_type"],
)
UP = Gauge(
    "llm_exporter_up",
    "1 if the last poll cycle for this provider account succeeded, else 0.",
    ["provider", "account"],
)
LAST_SUCCESS = Gauge(
    "llm_exporter_last_success_timestamp_seconds",
    "Unix timestamp of the last successful poll cycle for this provider account.",
    ["provider", "account"],
)
POLL_ERRORS = Counter(
    "llm_exporter_poll_errors_total",
    "Total number of failed poll cycles per provider account.",
    ["provider", "account"],
)
PRICING_MODELS = Gauge(
    "llm_pricing_models",
    "Number of models in the active pricing table.",
    ["source"],
)
PRICING_LAST_REFRESH = Gauge(
    "llm_pricing_last_refresh_timestamp_seconds",
    "Unix timestamp of the last successful pricing table load/refresh.",
)
BUILD_INFO = Gauge(
    "llm_exporter_build_info",
    "Exporter build information.",
    ["version"],
)
BUILD_INFO.labels(__version__).set(1)
