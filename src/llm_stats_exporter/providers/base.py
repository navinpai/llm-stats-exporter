from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

import requests

from llm_stats_exporter.records import Snapshot

DEFAULT_TIMEOUT = 60
UNKNOWN = "unknown"


def to_date(iso_ts: str) -> str:
    """YYYY-MM-DD from an ISO timestamp, falling back to the raw prefix."""
    try:
        return datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return (iso_ts or UNKNOWN)[:10]


def epoch_to_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


class Provider(ABC):
    """A usage/cost source that produces a normalized Snapshot for a time window."""

    name: str

    def __init__(self, account: str = "default") -> None:
        self.account = account
        self.session = requests.Session()

    @abstractmethod
    def fetch(self, start: datetime, end: datetime) -> Snapshot:
        """Fetch usage and cost records for [start, end) in daily buckets."""

    def _get_paginated(
        self, url: str, params: list[tuple[str, Any]], page_param: str = "page"
    ) -> list[dict[str, Any]]:
        """GET following the has_more/next_page pagination both providers use."""
        out: list[dict[str, Any]] = []
        page: str | None = None
        while True:
            query = list(params)
            if page:
                query.append((page_param, page))
            resp = self.session.get(url, params=query, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            body = resp.json()
            out.extend(body.get("data", []))
            if body.get("has_more") and body.get("next_page"):
                page = body["next_page"]
            else:
                return out
