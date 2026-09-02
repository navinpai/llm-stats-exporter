"""Environment-driven configuration.

Secrets can be provided directly via an environment variable (e.g. from a
Kubernetes Secret through ``secretKeyRef``) or via a ``*_FILE`` variant
pointing at a file (e.g. a Secret mounted as a volume).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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


def _read_int(env_name: str, default: int) -> int:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{env_name} must be an integer, got {raw!r}.") from exc


@dataclass(frozen=True)
class Config:
    openai_admin_key: str | None
    anthropic_admin_key: str | None
    port: int
    poll_interval_seconds: int
    lookback_days: int
    pricing_file: str | None
    log_level: str
    openai_api_base: str
    anthropic_api_base: str

    @classmethod
    def from_env(cls) -> Config:
        openai_key = read_secret("OPENAI_ADMIN_KEY")
        anthropic_key = read_secret("ANTHROPIC_ADMIN_KEY")
        if not openai_key and not anthropic_key:
            raise ConfigError(
                "At least one provider key is required: set OPENAI_ADMIN_KEY[_FILE] "
                "and/or ANTHROPIC_ADMIN_KEY[_FILE]."
            )
        return cls(
            openai_admin_key=openai_key,
            anthropic_admin_key=anthropic_key,
            port=_read_int("EXPORTER_PORT", 9184),
            poll_interval_seconds=_read_int("POLL_INTERVAL_SECONDS", 300),
            lookback_days=_read_int("LOOKBACK_DAYS", 2),
            pricing_file=os.environ.get("PRICING_FILE", "").strip() or None,
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            openai_api_base=os.environ.get("OPENAI_API_BASE", "https://api.openai.com").rstrip("/"),
            anthropic_api_base=os.environ.get(
                "ANTHROPIC_API_BASE", "https://api.anthropic.com"
            ).rstrip("/"),
        )
