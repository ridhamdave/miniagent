"""Tests for miniagent/config/ module.

Covers:
- MiniAgentConfig defaults
- load_config() with no yaml / no env
- load_config() merges yaml on top of defaults
- load_config() lets env vars override yaml
- get_config() singleton behaviour
- clear_config_cache() resets singleton
- EnvSettings reads MINIAGENT_PORT from env
"""

import pytest

from miniagent.config import (
    EnvSettings,
    MiniAgentConfig,
    clear_config_cache,
    get_config,
    load_config,
)


# ---------------------------------------------------------------------------
# Fixture: always reset the config singleton before/after each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_config():
    """Ensure a clean config singleton for every test."""
    clear_config_cache()
    yield
    clear_config_cache()


# ---------------------------------------------------------------------------
# MiniAgentConfig defaults
# ---------------------------------------------------------------------------


def test_miniagentconfig_default_gateway_port():
    cfg = MiniAgentConfig()
    assert cfg.gateway.port == 18789


def test_miniagentconfig_default_gateway_host():
    cfg = MiniAgentConfig()
    assert cfg.gateway.host == "127.0.0.1"


def test_miniagentconfig_default_browser_control_port():
    cfg = MiniAgentConfig()
    assert cfg.browser.control_port == 18790


def test_miniagentconfig_default_browser_enabled():
    cfg = MiniAgentConfig()
    assert cfg.browser.enabled is True


def test_miniagentconfig_default_browser_headless():
    cfg = MiniAgentConfig()
    assert cfg.browser.headless is False


def test_miniagentconfig_default_browser_timeout_ms():
    cfg = MiniAgentConfig()
    assert cfg.browser.timeout_ms == 8000


def test_miniagentconfig_default_agent_model():
    cfg = MiniAgentConfig()
    assert cfg.agent.model == "claude-opus-4-6"


def test_miniagentconfig_default_agent_max_tokens():
    cfg = MiniAgentConfig()
    assert cfg.agent.max_tokens == 8096


def test_miniagentconfig_default_sessions_dir():
    cfg = MiniAgentConfig()
    assert cfg.sessions_dir == "~/.miniagent/sessions"


def test_miniagentconfig_default_log_level():
    cfg = MiniAgentConfig()
    assert cfg.log_level == "info"


def test_miniagentconfig_default_gateway_auth_token_is_none():
    cfg = MiniAgentConfig()
    assert cfg.gateway.auth_token is None


# ---------------------------------------------------------------------------
# load_config() with nonexistent yaml and no env vars
# ---------------------------------------------------------------------------


def test_load_config_nonexistent_yaml_returns_defaults(tmp_path, monkeypatch):
    """Passing a path that doesn't exist should silently skip yaml and use defaults."""
    # Remove any env vars that could interfere
    for key in ("MINIAGENT_PORT", "MINIAGENT_HOST", "MINIAGENT_BROWSER_PORT", "MINIAGENT_LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)

    nonexistent = str(tmp_path / "does_not_exist.yaml")
    cfg = load_config(nonexistent)

    assert isinstance(cfg, MiniAgentConfig)
    assert cfg.gateway.port == 18789
    assert cfg.browser.control_port == 18790
    assert cfg.agent.model == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# load_config() merges yaml on top of defaults
# ---------------------------------------------------------------------------


def test_load_config_merges_yaml_port(tmp_path, monkeypatch):
    """A partial yaml that overrides only the port should leave other defaults intact."""
    for key in ("MINIAGENT_PORT", "MINIAGENT_HOST", "MINIAGENT_BROWSER_PORT", "MINIAGENT_LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("gateway:\n  port: 9999\n")

    cfg = load_config(str(config_file))

    assert cfg.gateway.port == 9999
    assert cfg.gateway.host == "127.0.0.1"  # default preserved


def test_load_config_merges_yaml_log_level(tmp_path, monkeypatch):
    for key in ("MINIAGENT_PORT", "MINIAGENT_HOST", "MINIAGENT_BROWSER_PORT", "MINIAGENT_LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("log_level: debug\n")

    cfg = load_config(str(config_file))
    assert cfg.log_level == "debug"


def test_load_config_merges_yaml_browser_headless(tmp_path, monkeypatch):
    for key in ("MINIAGENT_PORT", "MINIAGENT_HOST", "MINIAGENT_BROWSER_PORT", "MINIAGENT_LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("browser:\n  headless: true\n")

    cfg = load_config(str(config_file))
    assert cfg.browser.headless is True
    assert cfg.browser.control_port == 18790  # default preserved


def test_load_config_merges_yaml_agent_model(tmp_path, monkeypatch):
    for key in ("MINIAGENT_PORT", "MINIAGENT_HOST", "MINIAGENT_BROWSER_PORT", "MINIAGENT_LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("agent:\n  model: claude-sonnet-4-6\n")

    cfg = load_config(str(config_file))
    assert cfg.agent.model == "claude-sonnet-4-6"
    assert cfg.agent.max_tokens == 8096  # default preserved


# ---------------------------------------------------------------------------
# load_config() env vars override yaml
# ---------------------------------------------------------------------------


def test_load_config_env_overrides_yaml_port(tmp_path, monkeypatch):
    """Env var MINIAGENT_PORT must win over the yaml value."""
    monkeypatch.setenv("MINIAGENT_PORT", "7777")
    for key in ("MINIAGENT_HOST", "MINIAGENT_BROWSER_PORT", "MINIAGENT_LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("gateway:\n  port: 9999\n")

    cfg = load_config(str(config_file))
    assert cfg.gateway.port == 7777


def test_load_config_env_overrides_yaml_log_level(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIAGENT_LOG_LEVEL", "warning")
    for key in ("MINIAGENT_PORT", "MINIAGENT_HOST", "MINIAGENT_BROWSER_PORT"):
        monkeypatch.delenv(key, raising=False)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("log_level: debug\n")

    cfg = load_config(str(config_file))
    assert cfg.log_level == "warning"


def test_load_config_env_overrides_host(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIAGENT_HOST", "0.0.0.0")
    for key in ("MINIAGENT_PORT", "MINIAGENT_BROWSER_PORT", "MINIAGENT_LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)

    cfg = load_config(str(tmp_path / "nonexistent.yaml"))
    assert cfg.gateway.host == "0.0.0.0"


def test_load_config_env_overrides_browser_port(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIAGENT_BROWSER_PORT", "19999")
    for key in ("MINIAGENT_PORT", "MINIAGENT_HOST", "MINIAGENT_LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)

    cfg = load_config(str(tmp_path / "nonexistent.yaml"))
    assert cfg.browser.control_port == 19999


# ---------------------------------------------------------------------------
# get_config() singleton
# ---------------------------------------------------------------------------


def test_get_config_returns_same_object(monkeypatch):
    """get_config() must return the identical object on repeated calls."""
    for key in ("MINIAGENT_PORT", "MINIAGENT_HOST", "MINIAGENT_BROWSER_PORT", "MINIAGENT_LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)

    first = get_config()
    second = get_config()
    assert first is second


def test_get_config_returns_miniagentconfig(monkeypatch):
    for key in ("MINIAGENT_PORT", "MINIAGENT_HOST", "MINIAGENT_BROWSER_PORT", "MINIAGENT_LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)

    cfg = get_config()
    assert isinstance(cfg, MiniAgentConfig)


# ---------------------------------------------------------------------------
# clear_config_cache() resets singleton
# ---------------------------------------------------------------------------


def test_clear_config_cache_resets_singleton(monkeypatch):
    """After clear_config_cache(), get_config() must return a NEW object."""
    for key in ("MINIAGENT_PORT", "MINIAGENT_HOST", "MINIAGENT_BROWSER_PORT", "MINIAGENT_LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)

    first = get_config()
    clear_config_cache()
    second = get_config()
    assert first is not second


def test_clear_config_cache_allows_env_change(monkeypatch):
    """Clear + new env var should be picked up by a fresh get_config()."""
    for key in ("MINIAGENT_PORT", "MINIAGENT_HOST", "MINIAGENT_BROWSER_PORT", "MINIAGENT_LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)

    first = get_config()
    assert first.gateway.port == 18789

    clear_config_cache()
    monkeypatch.setenv("MINIAGENT_PORT", "5555")

    second = get_config()
    assert second.gateway.port == 5555


# ---------------------------------------------------------------------------
# EnvSettings reads MINIAGENT_PORT from env
# ---------------------------------------------------------------------------


def test_envsettings_reads_port(monkeypatch):
    monkeypatch.setenv("MINIAGENT_PORT", "12345")
    env = EnvSettings()
    assert env.port == 12345


def test_envsettings_port_none_when_unset(monkeypatch):
    monkeypatch.delenv("MINIAGENT_PORT", raising=False)
    env = EnvSettings()
    assert env.port is None


def test_envsettings_anthropic_api_key_optional(monkeypatch):
    """anthropic_api_key must be Optional so tests work without a real key."""
    monkeypatch.delenv("MINIAGENT_ANTHROPIC_API_KEY", raising=False)
    env = EnvSettings()
    assert env.anthropic_api_key is None


def test_envsettings_reads_anthropic_api_key(monkeypatch):
    monkeypatch.setenv("MINIAGENT_ANTHROPIC_API_KEY", "sk-test-key")
    env = EnvSettings()
    assert env.anthropic_api_key == "sk-test-key"


def test_envsettings_reads_log_level(monkeypatch):
    monkeypatch.setenv("MINIAGENT_LOG_LEVEL", "debug")
    env = EnvSettings()
    assert env.log_level == "debug"


def test_envsettings_reads_browser_port(monkeypatch):
    monkeypatch.setenv("MINIAGENT_BROWSER_PORT", "20000")
    env = EnvSettings()
    assert env.browser_port == 20000
