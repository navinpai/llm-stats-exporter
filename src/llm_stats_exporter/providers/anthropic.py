"""Anthropic Admin API provider.

Uses the org-level Usage & Cost Admin API (requires an admin key,
``sk-ant-admin...``):

- ``/v1/organizations/usage_report/messages`` — tokens per api_key/workspace/model
- ``/v1/organizations/cost_report`` — billed cost per workspace/description
- ``/v1/organizations/usage_report/claude_code`` — Claude Code sessions/cost per member
- ``/v1/organizations/api_keys`` / ``/v1/organizations/workspaces`` — name lookups

Docs: https://docs.anthropic.com/en/api/usage-cost-api
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import requests

from llm_stats_exporter.providers.base import UNKNOWN, Provider, to_date
from llm_stats_exporter.records import (
    ClaudeCodeModelUsage,
    ClaudeCodeRecord,
    CostRecord,
    Snapshot,
    UsageRecord,
)

log = logging.getLogger(__name__)

ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Maps the usage report's token fields (some arrive nested under
# cache_creation) onto normalized token types.
TOKEN_FIELD_MAP = {
    "uncached_input_tokens": "input",
    "output_tokens": "output",
    "cache_read_input_tokens": "cache_read",
    "cache_creation_input_tokens": "cache_write",
    "ephemeral_5m_input_tokens": "cache_write",
    "ephemeral_1h_input_tokens": "cache_write",
}


def extract_tokens(result: dict[str, Any]) -> dict[str, float]:
    totals: dict[str, float] = {}

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, dict | list):
                    walk(value)
                elif isinstance(value, int | float) and "token" in key:
                    token_type = TOKEN_FIELD_MAP.get(key)
                    if token_type:
                        totals[token_type] = totals.get(token_type, 0.0) + value
                    else:
                        log.debug("Unmapped Anthropic token field %r=%s", key, value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(result)
    return totals


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(
        self,
        admin_key: str,
        api_base: str = "https://api.anthropic.com",
        account: str = "default",
    ) -> None:
        super().__init__(account)
        self.api_base = api_base.rstrip("/")
        self.session.headers.update({"x-api-key": admin_key, "anthropic-version": "2023-06-01"})
        self._cc_supported = True
        self._cc_cache: dict[str, list[ClaudeCodeRecord]] = {}

    def fetch(self, start: datetime, end: datetime) -> Snapshot:
        starting_at = start.strftime(ISO_FMT)
        ending_at = end.strftime(ISO_FMT)
        key_names = self._fetch_names("/v1/organizations/api_keys", "API key")
        workspace_names = self._fetch_names("/v1/organizations/workspaces", "workspace")
        return Snapshot(
            usage=self._fetch_usage(starting_at, ending_at, key_names, workspace_names),
            costs=self._fetch_costs(starting_at, ending_at, workspace_names),
            claude_code=self._fetch_claude_code(start, end),
        )

    def _fetch_names(self, path: str, kind: str) -> dict[str, str]:
        """List endpoints paginate with has_more/last_id (unlike the report endpoints)."""
        names: dict[str, str] = {}
        after_id: str | None = None
        try:
            while True:
                params: list[tuple[str, Any]] = [("limit", "100")]
                if after_id:
                    params.append(("after_id", after_id))
                resp = self.session.get(f"{self.api_base}{path}", params=params, timeout=60)
                resp.raise_for_status()
                body = resp.json()
                for item in body.get("data", []):
                    if item.get("id"):
                        names[item["id"]] = item.get("name") or item["id"]
                if body.get("has_more") and body.get("last_id"):
                    after_id = body["last_id"]
                else:
                    break
        except requests.RequestException as exc:
            log.warning("Could not list Anthropic %ss (falling back to IDs): %s", kind, exc)
        return names

    def _fetch_usage(
        self,
        starting_at: str,
        ending_at: str,
        key_names: dict[str, str],
        workspace_names: dict[str, str],
    ) -> list[UsageRecord]:
        params: list[tuple[str, Any]] = [
            ("starting_at", starting_at),
            ("ending_at", ending_at),
            ("bucket_width", "1d"),
            ("group_by[]", "api_key_id"),
            ("group_by[]", "workspace_id"),
            ("group_by[]", "model"),
            ("group_by[]", "service_tier"),
            ("limit", "31"),
        ]
        buckets = self._get_paginated(
            f"{self.api_base}/v1/organizations/usage_report/messages", params
        )
        records: list[UsageRecord] = []
        for bucket in buckets:
            date = to_date(bucket.get("starting_at", ""))
            for result in bucket.get("results", []):
                key_id = result.get("api_key_id") or UNKNOWN
                workspace_id = result.get("workspace_id") or "default"
                tokens = extract_tokens(result)
                if not tokens:
                    continue
                records.append(
                    UsageRecord(
                        date=date,
                        operation="messages",
                        project_id=workspace_id,
                        project_name=workspace_names.get(workspace_id, workspace_id),
                        api_key_id=key_id,
                        api_key_name=key_names.get(key_id, key_id),
                        model=result.get("model") or UNKNOWN,
                        service_tier=result.get("service_tier") or "standard",
                        tokens=tokens,
                    )
                )
        return records

    def _fetch_claude_code(
        self, start: datetime, end: datetime
    ) -> list[ClaudeCodeRecord] | None:
        """The Claude Code report returns one day per call, so iterate the window.

        Days more than two days behind ``end`` no longer change and are served
        from an in-memory cache to keep the call count per poll low."""
        if not self._cc_supported:
            return []
        stable_cutoff = (end - timedelta(days=3)).strftime("%Y-%m-%d")
        records: list[ClaudeCodeRecord] = []
        day = start
        try:
            while day < end:
                date = day.strftime("%Y-%m-%d")
                day += timedelta(days=1)
                cached = self._cc_cache.get(date)
                if cached is not None and date < stable_cutoff:
                    records.extend(cached)
                    continue
                day_records = [
                    self._claude_code_record(item)
                    for item in self._get_paginated(
                        f"{self.api_base}/v1/organizations/usage_report/claude_code",
                        [("starting_at", date), ("limit", "100")],
                    )
                ]
                self._cc_cache[date] = day_records
                records.extend(day_records)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (403, 404):
                log.warning("Claude Code analytics not available for this org (disabling): %s", exc)
                self._cc_supported = False
                return []
            log.warning("Claude Code report fetch failed (keeping previous data): %s", exc)
            return None
        except requests.RequestException as exc:
            log.warning("Claude Code report fetch failed (keeping previous data): %s", exc)
            return None
        window_start = start.strftime("%Y-%m-%d")
        self._cc_cache = {d: r for d, r in self._cc_cache.items() if d >= window_start}
        return records

    def _claude_code_record(self, item: dict[str, Any]) -> ClaudeCodeRecord:
        actor = item.get("actor") or {}
        core = item.get("core_metrics") or {}
        lines = core.get("lines_of_code") or {}
        models: list[ClaudeCodeModelUsage] = []
        for entry in item.get("model_breakdown") or []:
            raw_tokens = entry.get("tokens") or {}
            tokens = {
                norm: float(raw_tokens.get(raw) or 0)
                for raw, norm in (
                    ("input", "input"),
                    ("output", "output"),
                    ("cache_read", "cache_read"),
                    ("cache_creation", "cache_write"),
                )
                if raw_tokens.get(raw)
            }
            cost = entry.get("estimated_cost") or {}
            models.append(
                ClaudeCodeModelUsage(
                    model=entry.get("model") or UNKNOWN,
                    tokens=tokens,
                    # Estimated cost is reported in cents.
                    estimated_cost_usd=float(cost.get("amount") or 0) / 100.0,
                )
            )
        return ClaudeCodeRecord(
            date=to_date(item.get("date", "")),
            actor=actor.get("email_address") or actor.get("api_key_name") or UNKNOWN,
            actor_type="user" if actor.get("type") == "user_actor" else "api_key",
            sessions=float(core.get("num_sessions") or 0),
            lines_added=float(lines.get("added") or 0),
            lines_removed=float(lines.get("removed") or 0),
            commits=float(core.get("commits_by_claude_code") or 0),
            pull_requests=float(core.get("pull_requests_by_claude_code") or 0),
            models=models,
        )

    def _fetch_costs(
        self, starting_at: str, ending_at: str, workspace_names: dict[str, str]
    ) -> list[CostRecord] | None:
        params: list[tuple[str, Any]] = [
            ("starting_at", starting_at),
            ("ending_at", ending_at),
            ("group_by[]", "workspace_id"),
            ("group_by[]", "description"),
        ]
        records: list[CostRecord] = []
        try:
            buckets = self._get_paginated(f"{self.api_base}/v1/organizations/cost_report", params)
        except requests.RequestException as exc:
            # The cost endpoint is unavailable on some platforms (e.g. Bedrock
            # orgs); usage data is still valuable, so don't fail the cycle.
            log.warning("Anthropic cost report fetch failed (keeping previous cost data): %s", exc)
            return None
        for bucket in buckets:
            date = to_date(bucket.get("starting_at", ""))
            for result in bucket.get("results", []):
                workspace_id = result.get("workspace_id") or "default"
                try:
                    # Cost API reports decimal strings in cents.
                    amount_usd = float(result.get("amount", 0)) / 100.0
                except (TypeError, ValueError):
                    continue
                records.append(
                    CostRecord(
                        date=date,
                        project_id=workspace_id,
                        project_name=workspace_names.get(workspace_id, workspace_id),
                        line_item=result.get("description") or UNKNOWN,
                        amount_usd=amount_usd,
                    )
                )
        return records
