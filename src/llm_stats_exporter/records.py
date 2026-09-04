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
class ClaudeCodeModelUsage:
    model: str
    tokens: dict[str, float] = field(default_factory=dict)
    estimated_cost_usd: float = 0.0


@dataclass(frozen=True)
class ClaudeCodeRecord:
    """One member (or API key) x day from Anthropic's Claude Code analytics API.

    ``actor`` is the member email or API key name; costs are Anthropic's
    estimates (subscription usage has no billed line items)."""

    date: str
    actor: str
    actor_type: str  # "user" | "api_key"
    sessions: float = 0.0
    lines_added: float = 0.0
    lines_removed: float = 0.0
    commits: float = 0.0
    pull_requests: float = 0.0
    models: list[ClaudeCodeModelUsage] = field(default_factory=list)


@dataclass(frozen=True)
class Snapshot:
    """``costs=None`` / ``claude_code=None`` mean that fetch failed (unknown),
    as opposed to ``[]`` (fetched fine, genuinely no data). The collector
    keeps the previous records when a section is unknown."""

    usage: list[UsageRecord]
    costs: list[CostRecord] | None
    claude_code: list[ClaudeCodeRecord] | None = field(default_factory=list)
