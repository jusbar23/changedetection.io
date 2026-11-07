"""Environment configuration for the MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(ValueError):
    """Raised when MCP server configuration is invalid."""


@dataclass(frozen=True)
class Settings:
    """Runtime settings loaded from the environment."""

    base_url: str
    api_key: str
    timeout: float
    host: str
    port: int
    bearer_token: str | None
    public_url: str
    transport: str


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def load_settings() -> Settings:
    """Load settings from environment variables.

    ``CHANGEDETECTION_BASE_URL``
        Base URL of the changedetection.io app (default ``http://127.0.0.1:5000``).
    ``CHANGEDETECTION_API_KEY``
        Value sent as the ``x-api-key`` header. Empty when API key checks are disabled.
    ``CHANGEDETECTION_TIMEOUT``
        HTTP timeout in seconds for API calls (default ``60``).
    ``MCP_HOST``
        Bind address for streamable HTTP (default ``127.0.0.1``).
    ``MCP_PORT``
        Bind port for streamable HTTP (default ``8000``).
    ``MCP_BEARER_TOKEN``
        When set, streamable HTTP requires ``Authorization: Bearer <token>``.
    ``MCP_PUBLIC_URL``
        Public URL of the MCP endpoint, used in auth metadata. Defaults to
        ``http://127.0.0.1:<MCP_PORT>/mcp`` (or the bind host when it is a specific address).
    ``MCP_TRANSPORT``
        ``stdio`` (default) or ``streamable-http``. Overridden by ``--transport``.
    """
    base_url = _env("CHANGEDETECTION_BASE_URL", "http://127.0.0.1:5000")
    assert base_url is not None
    base_url = base_url.rstrip("/")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ConfigError(
            "CHANGEDETECTION_BASE_URL must start with http:// or https://, "
            f"got {base_url!r}"
        )

    timeout_raw = _env("CHANGEDETECTION_TIMEOUT", "60")
    assert timeout_raw is not None
    try:
        timeout = float(timeout_raw)
    except ValueError as exc:
        raise ConfigError(f"CHANGEDETECTION_TIMEOUT must be a number, got {timeout_raw!r}") from exc
    if timeout <= 0:
        raise ConfigError("CHANGEDETECTION_TIMEOUT must be greater than 0")

    host = _env("MCP_HOST", "127.0.0.1")
    assert host is not None

    port_raw = _env("MCP_PORT", "8000")
    assert port_raw is not None
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ConfigError(f"MCP_PORT must be an integer, got {port_raw!r}") from exc
    if not 1 <= port <= 65535:
        raise ConfigError(f"MCP_PORT must be between 1 and 65535, got {port}")

    transport = (_env("MCP_TRANSPORT", "stdio") or "stdio").lower()
    if transport not in {"stdio", "streamable-http"}:
        raise ConfigError(
            "MCP_TRANSPORT must be 'stdio' or 'streamable-http', "
            f"got {transport!r}"
        )

    bearer = _env("MCP_BEARER_TOKEN")
    public_url = _env("MCP_PUBLIC_URL")
    if public_url is None:
        advertised_host = host if host not in {"0.0.0.0", "::", ""} else "127.0.0.1"
        public_url = f"http://{advertised_host}:{port}/mcp"
    public_url = public_url.rstrip("/")
    if not (public_url.startswith("http://") or public_url.startswith("https://")):
        raise ConfigError("MCP_PUBLIC_URL must start with http:// or https://, " f"got {public_url!r}")

    return Settings(
        base_url=base_url,
        api_key=_env("CHANGEDETECTION_API_KEY", "") or "",
        timeout=timeout,
        host=host,
        port=port,
        bearer_token=bearer,
        public_url=public_url,
        transport=transport,
    )
