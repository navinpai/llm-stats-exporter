"""Environment-driven configuration.

Secrets can be provided directly via an environment variable (e.g. from a
Kubernetes Secret through ``secretKeyRef``) or via a ``*_FILE`` variant
pointing at a file (e.g. a Secret mounted as a volume).

Multiple accounts per provider are supported through named key variables:
``OPENAI_ADMIN_KEY`` maps to the ``default`` account, while
``OPENAI_ADMIN_KEY_PROD`` (or ``OPENAI_ADMIN_KEY_PROD_FILE``) maps to a
``prod`` account. The name becomes the ``account`` metric label, lowercased.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from llm_stats_exporter.pricing import DEFAULT_TIER_MULTIPLIERS


class ConfigError(Exception):
    pass


def read_secret(env_name: str) -> str | None:
    value = os.environ.get(env_name, "").strip()
    file_path = os.environ.get(f"{env_name}_FILE", "").strip()
    if value and file_path:
        raise ConfigError(f"Set only one of {env_name} or {env_name}_FILE, not both.")
    if value:
        return value
    if file_path:
        try:
            content = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"Could not read {env_name}_FILE ({file_path}): {exc}") from exc
        if not content:
            raise ConfigError(f"{env_name}_FILE ({file_path}) is empty.")
        return content
    return None


@dataclass(frozen=True)
class Account:
    name: str
    key: str


def read_accounts(prefix: str) -> list[Account]:
    """Resolve all accounts for a provider key prefix (e.g. OPENAI_ADMIN_KEY)."""
    accounts: list[Account] = []
    default_key = read_secret(prefix)
    if default_key:
        accounts.append(Account("default", default_key))

    names: set[str] = set()
    for env_name in os.environ:
        if not env_name.startswith(f"{prefix}_"):
            continue
        suffix = env_name.removeprefix(f"{prefix}_")
        if suffix == "FILE":  # the default account's _FILE variant
            continue
        suffix = suffix.removesuffix("_FILE")
        if suffix:
            names.add(suffix)
    for name in sorted(names):
        key = read_secret(f"{prefix}_{name}")
        if key:
            account_name = name.lower()
            if account_name == "default" and default_key:
                raise ConfigError(
                    f"Account name 'default' in {prefix}_{name} clashes "
                    f"with the unnamed {prefix} key."
                )
            accounts.append(Account(account_name, key))
    return accounts


def _read_int(env_name: str, default: int) -> int:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{env_name} must be an integer, got {raw!r}.") from exc


def _read_tier_multipliers() -> dict[str, float]:
    """Merge PRICING_TIER_MULTIPLIERS (JSON tier -> multiplier) over the defaults."""
    raw = os.environ.get("PRICING_TIER_MULTIPLIERS", "").strip()
    multipliers = dict(DEFAULT_TIER_MULTIPLIERS)
    if not raw:
        return multipliers
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(f"PRICING_TIER_MULTIPLIERS is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(
            'PRICING_TIER_MULTIPLIERS must be a JSON object like {"batch": 0.5, "flex": 0.5}.'
        )
    for tier, value in parsed.items():
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise ConfigError(
                f"PRICING_TIER_MULTIPLIERS[{tier!r}] must be a non-negative number, got {value!r}."
            )
        multipliers[str(tier)] = float(value)
    return multipliers


@dataclass(frozen=True)
class Config:
    openai_accounts: list[Account]
    anthropic_accounts: list[Account]
    port: int
    poll_interval_seconds: int
    lookback_days: int
    pricing_source: str
    pricing_file: str | None
    pricing_url: str | None
    pricing_refresh_seconds: int
    pricing_tier_multipliers: dict[str, float]
    log_level: str
    openai_api_base: str
    anthropic_api_base: str

    @classmethod
    def from_env(cls) -> Config:
        openai_accounts = read_accounts("OPENAI_ADMIN_KEY")
        anthropic_accounts = read_accounts("ANTHROPIC_ADMIN_KEY")
        if not openai_accounts and not anthropic_accounts:
            raise ConfigError(
                "At least one provider key is required: set OPENAI_ADMIN_KEY[_FILE] "
                "and/or ANTHROPIC_ADMIN_KEY[_FILE] (or named variants like "
                "OPENAI_ADMIN_KEY_PROD)."
            )
        pricing_source = os.environ.get("PRICING_SOURCE", "litellm").strip().lower()
        if pricing_source not in ("litellm", "bundled"):
            raise ConfigError(
                f"PRICING_SOURCE must be 'litellm' or 'bundled', got {pricing_source!r}."
            )
        return cls(
            openai_accounts=openai_accounts,
            anthropic_accounts=anthropic_accounts,
            port=_read_int("EXPORTER_PORT", 9184),
            poll_interval_seconds=_read_int("POLL_INTERVAL_SECONDS", 300),
            lookback_days=_read_int("LOOKBACK_DAYS", 2),
            pricing_source=pricing_source,
            pricing_file=os.environ.get("PRICING_FILE", "").strip() or None,
            pricing_url=os.environ.get("PRICING_URL", "").strip() or None,
            pricing_refresh_seconds=_read_int("PRICING_REFRESH_SECONDS", 86400),
            pricing_tier_multipliers=_read_tier_multipliers(),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            openai_api_base=os.environ.get("OPENAI_API_BASE", "https://api.openai.com").rstrip("/"),
            anthropic_api_base=os.environ.get(
                "ANTHROPIC_API_BASE", "https://api.anthropic.com"
            ).rstrip("/"),
        )
