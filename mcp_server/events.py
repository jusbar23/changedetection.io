"""Alert types the changedetection.io workers already send.

Keep this tuple aligned with ``changedetectionio.notification_groups.NOTIFICATION_EVENTS``.
The MCP server does not import the web app.
"""

NOTIFICATION_EVENTS = ("content_changed", "filter_failure", "step_failure")

EVENT_LABELS = {
    "content_changed": "Content change, including restock and price changes",
    "filter_failure": "CSS, XPath, JSONPath, or jq filter was not found",
    "step_failure": "A configured browser step could not be run",
}
