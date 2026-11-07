import pytest

from mcp_server.config import ConfigError, load_settings


def test_defaults(monkeypatch):
    for name in (
        "CHANGEDETECTION_BASE_URL",
        "CHANGEDETECTION_API_KEY",
        "CHANGEDETECTION_TIMEOUT",
        "MCP_HOST",
        "MCP_PORT",
        "MCP_BEARER_TOKEN",
        "MCP_PUBLIC_URL",
        "MCP_TRANSPORT",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings()
    assert settings.base_url == "http://127.0.0.1:5000"
    assert settings.api_key == ""
    assert settings.port == 8000
    assert settings.host == "127.0.0.1"
    assert settings.transport == "stdio"
    assert settings.bearer_token is None
    assert settings.public_url == "http://127.0.0.1:8000/mcp"


def test_docker_style_env(monkeypatch):
    monkeypatch.setenv("CHANGEDETECTION_BASE_URL", "http://changedetection:5000/")
    monkeypatch.setenv("CHANGEDETECTION_API_KEY", "abc")
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_PORT", "9000")
    monkeypatch.setenv("MCP_BEARER_TOKEN", "secret")
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    settings = load_settings()
    assert settings.base_url == "http://changedetection:5000"
    assert settings.api_key == "abc"
    assert settings.host == "0.0.0.0"
    assert settings.port == 9000
    assert settings.bearer_token == "secret"
    assert settings.transport == "streamable-http"
    assert settings.public_url == "http://127.0.0.1:9000/mcp"


def test_invalid_values(monkeypatch):
    monkeypatch.setenv("CHANGEDETECTION_BASE_URL", "changedetection:5000")
    with pytest.raises(ConfigError):
        load_settings()
    monkeypatch.setenv("CHANGEDETECTION_BASE_URL", "http://changedetection:5000")
    monkeypatch.setenv("MCP_PORT", "nope")
    with pytest.raises(ConfigError):
        load_settings()
    monkeypatch.setenv("MCP_PORT", "8000")
    monkeypatch.setenv("MCP_TRANSPORT", "sse")
    with pytest.raises(ConfigError):
        load_settings()
