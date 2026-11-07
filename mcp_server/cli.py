"""Command-line entry point for stdio and streamable HTTP."""

from __future__ import annotations

import argparse
import logging
import sys

from mcp_server import __version__
from mcp_server.client import ChangeDetectionClient
from mcp_server.config import ConfigError, load_settings
from mcp_server.server import build_server

_LOG = logging.getLogger("mcp_server")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m mcp_server",
        description="MCP server for changedetection.io (wraps the /api/v1 REST API).",
    )
    parser.add_argument("--version", action="version", version=f"changedetection-mcp {__version__}")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=None,
        help="stdio (default) or streamable-http. Defaults to MCP_TRANSPORT when set.",
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"changedetection-mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    transport = args.transport or settings.transport
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    client = ChangeDetectionClient(settings.base_url, settings.api_key, timeout=settings.timeout)
    server = build_server(
        client,
        bearer_token=settings.bearer_token,
        public_url=settings.public_url,
    )

    if not settings.api_key:
        _LOG.warning(
            "CHANGEDETECTION_API_KEY is empty. Requests will fail if the app requires x-api-key "
            "(Settings → API in the changedetection.io UI)."
        )

    if transport == "stdio":
        _LOG.info("changedetection.io MCP server on stdio, API %s", settings.base_url)
        server.run(transport="stdio")
        return

    _LOG.info(
        "changedetection.io MCP server at http://%s:%s/mcp (API %s, bearer %s)",
        settings.host,
        settings.port,
        settings.base_url,
        "on" if settings.bearer_token else "off",
    )
    # Stateless JSON responses: each call is an independent API request, and clients
    # do not need a sticky session. Streamable HTTP still accepts the initialize handshake.
    server.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )


if __name__ == "__main__":
    main()
