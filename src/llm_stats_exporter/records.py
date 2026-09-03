"""Provider-agnostic usage and cost records.

Both providers normalize their API responses into these shapes so metrics
carry identical label sets regardless of provider. Anthropic workspaces map
onto the ``project`` labels.

Token type semantics: ``input`` is always *uncached* input tokens, so
``input + cache_read`` equals total prompt tokens for both providers.

``service_tier`` is normalized across providers: OpenAI's Batch API flag and
``default`` tier map to ``batch``/``standard``; Anthropic's tier is passed
through (``standard``, ``batch``, ``priority``, ...).
"""

from __future__ import annotations

from dataclasses import dataclass, field

TOKEN_TYPES = ("input", "output", "cache_read", "cache_write", "input_audio", "output_audio")


@dataclass(frozen=True)
class UsageRecord:
    date: str
    operation: str
    project_id: str
    project_name: str
    api_key_id: str
    api_key_name: str
    model: str
    service_tier: str = "standard"
    tokens: dict[str, float] = field(default_factory=dict)
    requests: float = 0.0


@dataclass(frozen=True)
class CostRecord:
    date: str
    project_id: str
    project_name: str
    line_item: str
    amount_usd: float


@dataclass(frozen=True)
class Snapshot:
    usage: list[UsageRecord]
    costs: list[CostRecord]
