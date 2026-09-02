import os

import pytest

from llm_stats_exporter.config import Account, Config, ConfigError, read_accounts, read_secret


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in list(os.environ):
        if var.startswith(("OPENAI_ADMIN_KEY", "ANTHROPIC_ADMIN_KEY")):
            monkeypatch.delenv(var)
    for var in ["EXPORTER_PORT", "POLL_INTERVAL_SECONDS", "LOOKBACK_DAYS"]:
        monkeypatch.delenv(var, raising=False)


def test_read_secret_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_ADMIN_KEY", " sk-admin-test \n")
    assert read_secret("OPENAI_ADMIN_KEY") == "sk-admin-test"


def test_read_secret_from_file(monkeypatch, tmp_path):
    secret_file = tmp_path / "key"
    secret_file.write_text("sk-ant-admin-test\n")
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY_FILE", str(secret_file))
    assert read_secret("ANTHROPIC_ADMIN_KEY") == "sk-ant-admin-test"


def test_read_secret_missing_returns_none():
    assert read_secret("OPENAI_ADMIN_KEY") is None


def test_read_secret_rejects_both(monkeypatch, tmp_path):
    secret_file = tmp_path / "key"
    secret_file.write_text("x")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-test")
    monkeypatch.setenv("OPENAI_ADMIN_KEY_FILE", str(secret_file))
    with pytest.raises(ConfigError, match="only one"):
        read_secret("OPENAI_ADMIN_KEY")


def test_read_secret_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_ADMIN_KEY_FILE", str(tmp_path / "nope"))
    with pytest.raises(ConfigError, match="Could not read"):
        read_secret("OPENAI_ADMIN_KEY")


def test_read_secret_empty_file(monkeypatch, tmp_path):
    secret_file = tmp_path / "key"
    secret_file.write_text("  \n")
    monkeypatch.setenv("OPENAI_ADMIN_KEY_FILE", str(secret_file))
    with pytest.raises(ConfigError, match="empty"):
        read_secret("OPENAI_ADMIN_KEY")


def test_from_env_requires_a_key():
    with pytest.raises(ConfigError, match="At least one provider key"):
        Config.from_env()


def test_from_env_defaults(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-ant-admin-test")
    config = Config.from_env()
    assert config.anthropic_accounts == [Account("default", "sk-ant-admin-test")]
    assert config.openai_accounts == []
    assert config.port == 9184
    assert config.poll_interval_seconds == 300
    assert config.lookback_days == 2
    assert config.pricing_file is None


def test_read_accounts_named_keys(monkeypatch, tmp_path):
    secret_file = tmp_path / "staging-key"
    secret_file.write_text("sk-admin-staging\n")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-default")
    monkeypatch.setenv("OPENAI_ADMIN_KEY_PROD", "sk-admin-prod")
    monkeypatch.setenv("OPENAI_ADMIN_KEY_STAGING_FILE", str(secret_file))
    assert read_accounts("OPENAI_ADMIN_KEY") == [
        Account("default", "sk-admin-default"),
        Account("prod", "sk-admin-prod"),
        Account("staging", "sk-admin-staging"),
    ]


def test_read_accounts_named_only(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY_TEAM_A", "sk-ant-admin-a")
    assert read_accounts("ANTHROPIC_ADMIN_KEY") == [Account("team_a", "sk-ant-admin-a")]


def test_read_accounts_default_file_is_not_an_account(monkeypatch, tmp_path):
    secret_file = tmp_path / "key"
    secret_file.write_text("sk-admin-default")
    monkeypatch.setenv("OPENAI_ADMIN_KEY_FILE", str(secret_file))
    assert read_accounts("OPENAI_ADMIN_KEY") == [Account("default", "sk-admin-default")]


def test_read_accounts_rejects_default_name_clash(monkeypatch):
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-1")
    monkeypatch.setenv("OPENAI_ADMIN_KEY_DEFAULT", "sk-admin-2")
    with pytest.raises(ConfigError, match="clashes"):
        read_accounts("OPENAI_ADMIN_KEY")


def test_from_env_rejects_bad_int(monkeypatch):
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-test")
    monkeypatch.setenv("EXPORTER_PORT", "not-a-port")
    with pytest.raises(ConfigError, match="EXPORTER_PORT"):
        Config.from_env()
