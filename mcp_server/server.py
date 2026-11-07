"""MCP server that wraps the changedetection.io REST API."""

from __future__ import annotations

import hmac
from typing import Any

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from mcp_server import __version__
from mcp_server.client import ChangeDetectionClient
from mcp_server.tools import register_tools

_INSTRUCTIONS = (
    "Tools for one changedetection.io instance. "
    "List, search, create, update, and delete watches; read history, snapshots, and a unified diff. "
    "Pause, resume, mute, and recheck one watch or a confirmed batch. "
    "Import http(s) URLs and export watched URLs with passwords removed. "
    "Manage tags (the UI calls these Groups) and notification groups, which are a different list: "
    "named sets of existing Apprise destinations. "
    "Assign a notification group per alert with the route tools. The alerts are content_changed "
    "(this includes restock and price changes), filter_failure, and step_failure. "
    "A selected group replaces the URL list for that alert, so a destination is not notified twice. "
    "Empty routes inherit: watch, then the first tag, then the system group, then the existing URL lists. "
    "A missing group is skipped. An empty group notifies nobody for that alert. "
    "Destinations and secret headers in tool results are redacted. "
    "Destructive and bulk tools require confirm=true. "
    "This server does not send test notifications, reset the API key, or return the zip backup. "
    "Diffs are computed from the text snapshots the history API returns."
)

class StaticBearerVerifier(TokenVerifier):
    """Accept a single shared bearer token from ``MCP_BEARER_TOKEN``."""

    def __init__(self, expected: str):
        self._expected = expected

    async def verify_token(self, token: str) -> AccessToken | None:
        if len(token) != len(self._expected) or not hmac.compare_digest(token, self._expected):
            return None
        return AccessToken(token=token, client_id="changedetection-mcp", scopes=["mcp"])


def build_server(
    client: ChangeDetectionClient,
    *,
    bearer_token: str | None = None,
    public_url: str = "http://127.0.0.1:8000/mcp",
) -> MCPServer:
    """Build an MCP server whose tools call ``client``.

    When ``bearer_token`` is set, streamable HTTP requires that exact bearer token.
    stdio and the in-memory test client do not see HTTP headers, so the token is not
    checked on those transports.
    """
    auth_kwargs: dict[str, Any] = {}
    if bearer_token:
        resource = public_url if public_url.endswith("/mcp") else public_url.rstrip("/") + "/mcp"
        issuer = resource[: -len("/mcp")] or resource
        auth_kwargs = {
            "token_verifier": StaticBearerVerifier(bearer_token),
            "auth": AuthSettings(
                issuer_url=AnyHttpUrl(issuer if issuer.endswith("/") else issuer + "/"),
                resource_server_url=AnyHttpUrl(resource),
                required_scopes=[],
                # The shared secret is checked in the verifier. The resource URL is only
                # metadata; clients may reach the server through a published port.
                validate_token_resource=False,
            ),
        }

    mcp = MCPServer(
        "changedetection.io",
        title="changedetection.io",
        instructions=_INSTRUCTIONS,
        version=__version__,
        **auth_kwargs,
    )
    register_tools(mcp, client)
    return mcp
