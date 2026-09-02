from __future__ import annotations

import logging
import signal
import sys
import threading
from types import FrameType

from prometheus_client import start_http_server

from llm_stats_exporter import __version__
from llm_stats_exporter.collector import Collector
from llm_stats_exporter.config import Config, ConfigError
from llm_stats_exporter.pricing import Pricing
from llm_stats_exporter.providers import AnthropicProvider, OpenAIProvider
from llm_stats_exporter.providers.base import Provider

log = logging.getLogger("llm-stats-exporter")

_stop = threading.Event()


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    log.info("Received signal %s, shutting down.", signum)
    _stop.set()


def build_providers(config: Config) -> list[Provider]:
    providers: list[Provider] = []
    if config.openai_admin_key:
        providers.append(OpenAIProvider(config.openai_admin_key, config.openai_api_base))
    if config.anthropic_admin_key:
        providers.append(AnthropicProvider(config.anthropic_admin_key, config.anthropic_api_base))
    return providers


def main() -> None:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    providers = build_providers(config)
    pricing = Pricing.load(config.pricing_file)
    collector = Collector(providers, pricing, config.lookback_days)

    start_http_server(config.port)
    log.info(
        "llm-stats-exporter %s listening on :%d/metrics (providers: %s, poll every %ds, "
        "lookback %dd).",
        __version__,
        config.port,
        ", ".join(p.name for p in providers),
        config.poll_interval_seconds,
        config.lookback_days,
    )

    while not _stop.is_set():
        collector.poll()
        _stop.wait(config.poll_interval_seconds)

    log.info("Stopped.")


if __name__ == "__main__":
    main()
