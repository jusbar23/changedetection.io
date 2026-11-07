"""Response shaping, redaction, and input checks for MCP tools.

Redaction is duplicated from ``changedetectionio.notification_groups`` so this
process does not import the Flask application. A parity test keeps them aligned.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from mcp.server.mcpserver.exceptions import ToolError

from mcp_server.client import ChangeDetectionError
from mcp_server.events import NOTIFICATION_EVENTS

_KEEP_PATH_SEGMENTS = {"api", "webhooks", "webhook", "notify", "v1", "v2", "hooks", "services"}
_MAX_BULK = 50
_MAX_PAGE = 200


def redact_destination(url: str) -> str:
    """Hide credentials and path tokens while keeping the scheme and hostname."""
    text = (url or "").strip()
    if not text or text.startswith("#"):
        return text
    if "://" not in text:
        return "***"
    scheme, rest = text.split("://", 1)
    rest = re.sub(r"(?<=:)[^/@\s]+(?=@)", "***", rest)

    def mask(piece: str, *, structural: bool) -> str:
        if not piece or set(piece) <= {"*"}:
            return "***" if piece else piece
        if structural and piece.lower() in _KEEP_PATH_SEGMENTS:
            return piece
        return "***"

    pieces = re.split(r"([/?&=#])", rest)
    masked: list[str] = []
    in_path = False
    after_equals = False
    for piece in pieces:
        if piece in {"/", "?", "&", "=", "#"}:
            if piece in {"/", "?"}:
                in_path = True
            after_equals = piece == "="
            masked.append(piece)
            continue
        if not in_path:
            if "@" not in piece and "." not in piece:
                masked.append(mask(piece, structural=False))
            else:
                masked.append(piece)
            continue
        if after_equals:
            masked.append(mask(piece, structural=False))
            after_equals = False
            continue
        masked.append(mask(piece, structural=True))
    return f"{scheme}://{''.join(masked)}"


def redact_userinfo(url: str) -> str:
    text = (url or "").strip()
    if "://" not in text:
        return text
    scheme, rest = text.split("://", 1)
    rest = re.sub(r"(?<=:)[^/@\s]+(?=@)", "***", rest)
    return f"{scheme}://{rest}"


def redact_headers(headers: dict[str, Any] | None) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in (headers or {}).items():
        name = str(key).lower()
        if name in {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token"} or any(
            word in name for word in ("token", "secret", "password", "api-key")
        ):
            redacted[str(key)] = "***"
        else:
            redacted[str(key)] = value
    return redacted


def present_destinations(urls: list[Any] | None) -> list[str]:
    return [url if str(url).startswith("#") else redact_destination(str(url)) for url in (urls or [])]


def present_watch(watch: dict[str, Any]) -> dict[str, Any]:
    data = dict(watch)
    if isinstance(data.get("url"), str):
        data["url"] = redact_userinfo(data["url"])
    if isinstance(data.get("notification_urls"), list):
        data["notification_urls"] = present_destinations(data["notification_urls"])
    if isinstance(data.get("headers"), dict):
        data["headers"] = redact_headers(data["headers"])
    return data


def present_summary(watch_uuid: str, summary: dict[str, Any]) -> dict[str, Any]:
    row = dict(summary)
    row["uuid"] = watch_uuid
    if isinstance(row.get("url"), str):
        row["url"] = redact_userinfo(row["url"])
    return row


def call_api(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ChangeDetectionError as exc:
        raise ToolError(_friendly_error(exc)) from exc


def require_confirm(confirm: bool, action: str) -> None:
    if confirm is not True:
        raise ToolError(f"Refused to {action}. Pass confirm=true to run it.")


def require_id(value: str, label: str) -> str:
    text = (value or "").strip()
    if not text or any(character in text for character in "/?#\\"):
        raise ToolError(f"{label} must be the id itself, not a path or URL.")
    if len(text) > 80:
        raise ToolError(f"{label} is too long.")
    return text


def require_ids(values: list[str], label: str) -> list[str]:
    if not values:
        raise ToolError(f"Pass at least one {label}.")
    if len(values) > _MAX_BULK:
        raise ToolError(f"Refusing to change more than {_MAX_BULK} items at once. Split the list.")
    return [require_id(value, label) for value in values]


def quote_id(value: str) -> str:
    return quote(require_id(value, "id"), safe="")


def page(items: list[Any], *, offset: int = 0, limit: int | None = None) -> tuple[list[Any], dict[str, Any]]:
    if offset < 0:
        raise ToolError("offset must be 0 or greater.")
    if limit is not None and (limit < 1 or limit > _MAX_PAGE):
        raise ToolError(f"limit must be from 1 to {_MAX_PAGE}, or omit it to return every row.")
    total = len(items)
    window = items[offset:] if limit is None else items[offset:offset + limit]
    return window, {
        "total": total,
        "offset": offset,
        "limit": limit,
        "returned": len(window),
        "has_more": offset + len(window) < total,
    }


def validate_watch_url(url: str) -> str:
    text = (url or "").strip()
    lowered = text.lower()
    if any(lowered.startswith(prefix) for prefix in ("file:", "javascript:", "data:", "vbscript:")):
        raise ToolError("Only http:// and https:// URLs can be watched.")
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise ToolError("Watch URL must start with http:// or https://. Example: https://example.com/pricing")
    if any(character in text for character in "\r\n\t "):
        raise ToolError("Watch URL must be a single line without spaces.")
    return text


def validate_destinations(urls: list[str]) -> list[str]:
    cleaned = []
    for url in urls:
        text = (url or "").strip()
        if not text:
            raise ToolError("A notification destination cannot be empty.")
        if text.startswith("#"):
            cleaned.append(text)
            continue
        lowered = text.lower()
        if any(lowered.startswith(prefix) for prefix in ("file:", "javascript:", "data:", "vbscript:")):
            raise ToolError("file, javascript, and data notification URLs are not allowed.")
        if "://" not in text or any(character in text for character in "\r\n\t "):
            raise ToolError(
                "Each notification destination must be one Apprise URL, for example mailto://alerts@example.com."
            )
        cleaned.append(text)
    return cleaned


def validate_routes(routes: dict[str, Any]) -> dict[str, str | None]:
    if not isinstance(routes, dict) or not routes:
        raise ToolError(
            "Pass routes with one or more of content_changed, filter_failure, and step_failure. "
            "Use null or an empty string to inherit again."
        )
    cleaned: dict[str, str | None] = {}
    for event, group_uuid in routes.items():
        if event not in NOTIFICATION_EVENTS:
            raise ToolError(
                f"Unknown alert type {event!r}. Use content_changed, filter_failure, or step_failure. "
                "Restock and price changes use content_changed."
            )
        if group_uuid is None or str(group_uuid).strip() == "":
            cleaned[event] = None
        else:
            cleaned[event] = require_id(str(group_uuid), "notification group id")
    return cleaned


def watch_summary(watch: dict[str, Any]) -> str:
    title = watch.get("title") or watch.get("url") or "Watch"
    bits = [str(title)]
    if watch.get("paused"):
        bits.append("paused")
    if watch.get("notification_muted"):
        bits.append("notifications muted")
    if watch.get("last_error"):
        bits.append("last check failed")
    return ". ".join(bits) if len(bits) == 1 else f"{bits[0]} ({', '.join(bits[1:])})"


def _friendly_error(exc: ChangeDetectionError) -> str:
    detail = str(exc)
    hint = {
        400: "Check the values you sent. Group and watch ids come from the list tools.",
        403: "The API key was rejected. Set CHANGEDETECTION_API_KEY to the key in Settings → API.",
        404: "That id was not found. List watches, tags, or notification groups and use one of those ids.",
    }.get(exc.status_code or 0)
    if hint and hint not in detail:
        return f"{detail} {hint}"
    return detail
