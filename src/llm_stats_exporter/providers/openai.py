"""OpenAI Admin API provider.

Uses the org-level Usage & Costs API (requires an admin key,
``sk-admin...``):

- ``/v1/organization/usage/{operation}`` — tokens per project/api_key/model
- ``/v1/organization/costs`` — billed cost per project/line_item
- ``/v1/organization/projects`` and per-project api_keys — name lookups

Docs: https://platform.openai.com/docs/api-reference/usage
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import requests

from llm_stats_exporter.providers.base import UNKNOWN, Provider, epoch_to_date
from llm_stats_exporter.records import CostRecord, Snapshot, UsageRecord

log = logging.getLogger(__name__)

# Usage endpoints that report token counts.
TOKEN_OPERATIONS = ("completions", "embeddings", "moderations")

TOKEN_FIELD_MAP = {
    "output_tokens": "output",
    "input_cached_tokens": "cache_read",
    "input_audio_tokens": "input_audio",
    "output_audio_tokens": "output_audio",
}


def extract_tokens(result: dict[str, Any]) -> dict[str, float]:
    totals: dict[str, float] = {}
    # input_tokens includes cached tokens; normalize "input" to uncached-only
    # so semantics match the Anthropic provider (and pricing categories).
    input_tokens = float(result.get("input_tokens") or 0)
    cached = float(result.get("input_cached_tokens") or 0)
    uncached = max(input_tokens - cached, 0.0)
    if uncached:
        totals["input"] = uncached
    for field, token_type in TOKEN_FIELD_MAP.items():
        value = float(result.get(field) or 0)
        if value:
            totals[token_type] = totals.get(token_type, 0.0) + value
    return totals


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(
        self,
        admin_key: str,
        api_base: str = "https://api.openai.com",
        account: str = "default",
    ) -> None:
        super().__init__(account)
        self.api_base = api_base.rstrip("/")
        self.session.headers.update({"Authorization": f"Bearer {admin_key}"})
        self._api_key_names: dict[str, str] = {}

    def fetch(self, start: datetime, end: datetime) -> Snapshot:
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        project_names = self._fetch_project_names()
        usage: list[UsageRecord] = []
        for operation in TOKEN_OPERATIONS:
            usage.extend(self._fetch_usage(operation, start_ts, end_ts, project_names))
        return Snapshot(usage=usage, costs=self._fetch_costs(start_ts, end_ts, project_names))

    def _fetch_project_names(self) -> dict[str, str]:
        names: dict[str, str] = {}
        try:
            for project in self._get_paginated(
                f"{self.api_base}/v1/organization/projects",
                [("limit", "100"), ("include_archived", "true")],
                page_param="after",
            ):
                if project.get("id"):
                    names[project["id"]] = project.get("name") or project["id"]
        except requests.RequestException as exc:
            log.warning("Could not list OpenAI projects (falling back to IDs): %s", exc)
        return names

    def _api_key_name(self, project_id: str, api_key_id: str) -> str:
        if not api_key_id or api_key_id == UNKNOWN:
            return UNKNOWN
        if api_key_id in self._api_key_names:
            return self._api_key_names[api_key_id]
        urls = []
        if project_id and project_id != UNKNOWN:
            urls.append(
                f"{self.api_base}/v1/organization/projects/{project_id}/api_keys/{api_key_id}"
            )
        urls.append(f"{self.api_base}/v1/organization/admin_api_keys/{api_key_id}")
        for url in urls:
            try:
                resp = self.session.get(url, timeout=60)
                if resp.ok and (name := resp.json().get("name")):
                    self._api_key_names[api_key_id] = name
                    return str(name)
            except requests.RequestException:
                continue
        self._api_key_names[api_key_id] = api_key_id
        return api_key_id

    def _fetch_usage(
        self, operation: str, start_ts: int, end_ts: int, project_names: dict[str, str]
    ) -> list[UsageRecord]:
        params: list[tuple[str, Any]] = [
            ("start_time", start_ts),
            ("end_time", end_ts),
            ("bucket_width", "1d"),
            ("limit", "31"),
            ("group_by", "project_id"),
            ("group_by", "api_key_id"),
            ("group_by", "model"),
        ]
        buckets = self._get_paginated(f"{self.api_base}/v1/organization/usage/{operation}", params)
        records: list[UsageRecord] = []
        for bucket in buckets:
            date = epoch_to_date(int(bucket.get("start_time", 0)))
            for result in bucket.get("results", []):
                tokens = extract_tokens(result)
                requests_count = float(result.get("num_model_requests") or 0)
                if not tokens and not requests_count:
                    continue
                project_id = result.get("project_id") or UNKNOWN
                api_key_id = result.get("api_key_id") or UNKNOWN
                records.append(
                    UsageRecord(
                        date=date,
                        operation=operation,
                        project_id=project_id,
                        project_name=project_names.get(project_id, project_id),
                        api_key_id=api_key_id,
                        api_key_name=self._api_key_name(project_id, api_key_id),
                        model=result.get("model") or UNKNOWN,
                        tokens=tokens,
                        requests=requests_count,
                    )
                )
        return records

    def _fetch_costs(
        self, start_ts: int, end_ts: int, project_names: dict[str, str]
    ) -> list[CostRecord]:
        params: list[tuple[str, Any]] = [
            ("start_time", start_ts),
            ("end_time", end_ts),
            ("bucket_width", "1d"),
            ("limit", "31"),
            ("group_by", "project_id"),
            ("group_by", "line_item"),
        ]
        records: list[CostRecord] = []
        try:
            buckets = self._get_paginated(f"{self.api_base}/v1/organization/costs", params)
        except requests.RequestException as exc:
            log.warning("OpenAI costs fetch failed (continuing): %s", exc)
            return records
        for bucket in buckets:
            date = epoch_to_date(int(bucket.get("start_time", 0)))
            for result in bucket.get("results", []):
                amount = result.get("amount") or {}
                try:
                    amount_usd = float(amount.get("value", 0))
                except (TypeError, ValueError):
                    continue
                project_id = result.get("project_id") or UNKNOWN
                records.append(
                    CostRecord(
                        date=date,
                        project_id=project_id,
                        project_name=project_names.get(project_id, project_id),
                        line_item=result.get("line_item") or UNKNOWN,
                        amount_usd=amount_usd,
                    )
                )
        return records
