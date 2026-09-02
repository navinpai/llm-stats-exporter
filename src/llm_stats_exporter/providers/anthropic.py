"""Anthropic Admin API provider.

Uses the org-level Usage & Cost Admin API (requires an admin key,
``sk-ant-admin...``):

- ``/v1/organizations/usage_report/messages`` — tokens per api_key/workspace/model
- ``/v1/organizations/cost_report`` — billed cost per workspace/description
- ``/v1/organizations/api_keys`` / ``/v1/organizations/workspaces`` — name lookups

Docs: https://docs.anthropic.com/en/api/usage-cost-api
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import requests

from llm_stats_exporter.providers.base import UNKNOWN, Provider, to_date
from llm_stats_exporter.records import CostRecord, Snapshot, UsageRecord

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

    def __init__(self, admin_key: str, api_base: str = "https://api.anthropic.com") -> None:
        super().__init__()
        self.api_base = api_base.rstrip("/")
        self.session.headers.update({"x-api-key": admin_key, "anthropic-version": "2023-06-01"})

    def fetch(self, start: datetime, end: datetime) -> Snapshot:
        starting_at = start.strftime(ISO_FMT)
        ending_at = end.strftime(ISO_FMT)
        key_names = self._fetch_names("/v1/organizations/api_keys", "API key")
        workspace_names = self._fetch_names("/v1/organizations/workspaces", "workspace")
        return Snapshot(
            usage=self._fetch_usage(starting_at, ending_at, key_names, workspace_names),
            costs=self._fetch_costs(starting_at, ending_at, workspace_names),
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
                        tokens=tokens,
                    )
                )
        return records

    def _fetch_costs(
        self, starting_at: str, ending_at: str, workspace_names: dict[str, str]
    ) -> list[CostRecord]:
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
            log.warning("Anthropic cost report fetch failed (continuing): %s", exc)
            return records
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
