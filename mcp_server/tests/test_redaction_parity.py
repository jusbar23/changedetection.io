"""Keep MCP redaction aligned with the application without importing it from the server."""

from changedetectionio.notification_groups import NOTIFICATION_EVENTS as APP_EVENTS
from changedetectionio.notification_groups import redact_destination as app_redact
from changedetectionio.notification_groups import redact_headers as app_headers
from changedetectionio.notification_groups import redact_userinfo as app_userinfo
from mcp_server.events import NOTIFICATION_EVENTS as MCP_EVENTS
from mcp_server.present import redact_destination, redact_headers, redact_userinfo

SAMPLES = [
    "discord://hook/secret-token",
    "mailto://user:pass@example.com",
    "json://localhost/hook/secret?token=abc",
    "https://alerts.example.com/api/webhooks/v1/secret",
    "# a comment",
    "not a url",
    "discord://***/***",
]


def test_event_names_match():
    assert tuple(APP_EVENTS) == tuple(MCP_EVENTS)


def test_redaction_matches_and_is_idempotent():
    for sample in SAMPLES:
        app_value = app_redact(sample)
        mcp_value = redact_destination(sample)
        assert app_value == mcp_value
        assert app_redact(app_value) == app_value
        assert redact_destination(mcp_value) == mcp_value
        assert "secret-token" not in mcp_value
        assert "pass@" not in mcp_value

    page = "https://user:s3cret@example.com/pricing"
    assert app_userinfo(page) == redact_userinfo(page) == "https://user:***@example.com/pricing"

    headers = {"Authorization": "Bearer secret", "Accept": "text/html", "X-Api-Key": "abc"}
    assert app_headers(headers) == redact_headers(headers)
    assert redact_headers(headers)["Authorization"] == "***"
    assert redact_headers(headers)["Accept"] == "text/html"
