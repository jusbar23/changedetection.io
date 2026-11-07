"""MCP tools that wrap the changedetection.io REST API."""

from __future__ import annotations

import difflib
import hmac
from typing import Any

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl

from mcp_server import __version__
from mcp_server.client import ChangeDetectionClient, ChangeDetectionError

_INSTRUCTIONS = (
    "Tools for one changedetection.io instance. "
    "List, search, create, update, and delete watches; read a watch, its snapshot history, "
    "the latest or previous snapshot, and a unified diff of two snapshots. "
    "Pause, unpause, mute, unmute, and queue a recheck. "
    "List and manage tags. Read system info (version, uptime, queue, overdue watches). "
    "Watches and tags are identified by UUID. "
    "On create, `tag` is a comma-separated list of tag names (tags are created if needed). "
    "On update, `tag` replaces the watch's tags by name and `tags` replaces them by UUID. "
    "Selectors go in `include_filters` (CSS, XPath, JSONPath `json:`, or jq `jq:`) and "
    "`subtractive_selectors` (CSS/XPath removed before comparison). "
    "There is no diff endpoint in the REST API: diffs are computed from the text snapshots "
    "the history API returns, which is the filtered text changedetection.io compares."
)

_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
_CREATE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
_DELETE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)


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
    _register_tools(mcp, client)
    return mcp


def _register_tools(mcp: MCPServer, client: ChangeDetectionClient) -> None:  # noqa: C901
    @mcp.tool(annotations=_READ)
    def list_watches(tag: str | None = None) -> dict[str, Any]:
        """List watches, optionally limited to a tag name.

        Returns a summary (url, title, last_checked, last_changed, last_error, viewed)
        keyed by watch UUID. `tag` is a tag title, not a UUID.
        """
        watches = _call(client.list_watches, tag)
        return {"count": len(watches), "watches": watches}

    @mcp.tool(annotations=_READ)
    def search_watches(query: str, tag: str | None = None, partial: bool = False) -> dict[str, Any]:
        """Search watches by URL or title.

        Without `partial`, the query must match the whole URL or title (case-insensitive).
        With `partial`, it matches a substring of the URL, title, or last error.
        `tag` is an optional tag title that limits the search.
        """
        watches = _call(client.search_watches, query, tag, partial)
        return {"count": len(watches), "query": query, "partial": partial, "watches": watches}

    @mcp.tool(annotations=_READ)
    def get_watch(uuid: str) -> dict[str, Any]:
        """Get one watch, including filters, tags, interval, and notification URLs.

        History bodies are not included; use get_watch_history, get_watch_snapshot, or get_watch_diff.
        """
        return _call(client.get_watch, uuid)

    @mcp.tool(annotations=_READ)
    def get_watch_history(uuid: str) -> dict[str, Any]:
        """List stored text snapshots for a watch.

        Timestamps are Unix seconds as strings, oldest first. `latest` and `previous`
        are set when those snapshots exist. Paths are files on the changedetection server.
        """
        history = _call(client.get_history, uuid)
        timestamps = _sorted_timestamps(history)
        return {
            "uuid": uuid,
            "count": len(timestamps),
            "timestamps": timestamps,
            "latest": timestamps[-1] if timestamps else None,
            "previous": timestamps[-2] if len(timestamps) >= 2 else None,
            "entries": [{"timestamp": ts, "path": history[ts]} for ts in timestamps],
        }

    @mcp.tool(annotations=_READ)
    def get_watch_snapshot(
        uuid: str,
        timestamp: str = "latest",
        html: bool = False,
        max_chars: int = 80000,
    ) -> dict[str, Any]:
        """Get one snapshot body.

        `timestamp` is Unix seconds, `latest`, or `previous` (the snapshot before latest).
        `html` returns the fetched HTML instead of the filtered text used for comparison.
        `max_chars` truncates the returned text; ``0`` means no limit.
        """
        resolved = _resolve_timestamp(client, uuid, timestamp)
        content = _call(client.get_snapshot, uuid, resolved, html=html)
        text, truncated = _limit(content, max_chars)
        return {
            "uuid": uuid,
            "timestamp": resolved,
            "html": html,
            "truncated": truncated,
            "content": text,
        }

    @mcp.tool(annotations=_READ)
    def get_watch_diff(
        uuid: str,
        from_timestamp: str | None = None,
        to_timestamp: str | None = None,
        max_chars: int = 80000,
    ) -> dict[str, Any]:
        """Unified diff of two text snapshots.

        Omit both timestamps to compare the previous snapshot with the latest one.
        Pass both to compare specific Unix timestamps (`latest` and `previous` are allowed).
        The diff is computed here from the text snapshots returned by the history API.
        """
        if (from_timestamp is None) ^ (to_timestamp is None):
            raise ToolError("Pass both from_timestamp and to_timestamp, or neither to compare previous and latest.")
        history = _call(client.get_history, uuid)
        timestamps = _sorted_timestamps(history)
        if from_timestamp is None:
            if len(timestamps) < 2:
                return {
                    "uuid": uuid,
                    "from_timestamp": timestamps[-1] if timestamps else None,
                    "to_timestamp": None,
                    "changed": False,
                    "truncated": False,
                    "additions": 0,
                    "deletions": 0,
                    "message": "Need at least two snapshots to build a diff.",
                    "diff": "",
                }
            older, newer = timestamps[-2], timestamps[-1]
        else:
            assert to_timestamp is not None
            older = _resolve_timestamp(client, uuid, from_timestamp, history=history)
            newer = _resolve_timestamp(client, uuid, to_timestamp, history=history)
        before = _call(client.get_snapshot, uuid, older)
        after = _call(client.get_snapshot, uuid, newer)
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"snapshot-{older}",
                tofile=f"snapshot-{newer}",
            )
        )
        additions, deletions = _diff_counts(diff)
        text, truncated = _limit(diff, max_chars)
        return {
            "uuid": uuid,
            "from_timestamp": older,
            "to_timestamp": newer,
            "changed": bool(diff),
            "truncated": truncated,
            "additions": additions,
            "deletions": deletions,
            "diff": text,
        }

    @mcp.tool(annotations=_CREATE)
    def create_watch(
        url: str,
        title: str | None = None,
        tag: str | None = None,
        tags: list[str] | None = None,
        check_weeks: int | None = None,
        check_days: int | None = None,
        check_hours: int | None = None,
        check_minutes: int | None = None,
        check_seconds: int | None = None,
        include_filters: list[str] | None = None,
        subtractive_selectors: list[str] | None = None,
        ignore_text: list[str] | None = None,
        trigger_text: list[str] | None = None,
        notification_urls: list[str] | None = None,
        paused: bool | None = None,
    ) -> dict[str, Any]:
        """Create a watch and queue its first check.

        `tag` is a comma-separated list of tag names. Names that do not exist are created.
        `tags` is a list of existing tag UUIDs.
        Any `check_*` value sets a custom interval and turns off the system default interval.
        `include_filters` selects content (CSS, XPath, `json:` JSONPath, or `jq:`).
        `subtractive_selectors` removes matching elements before comparison.
        `notification_urls` are Apprise URLs for this watch (for example `discord://...` or `mailto://...`).
        """
        payload: dict[str, Any] = {"url": url}
        _assign(payload, "title", title)
        _assign(payload, "tag", tag)
        _assign(payload, "tags", tags)
        _assign(payload, "include_filters", include_filters)
        _assign(payload, "subtractive_selectors", subtractive_selectors)
        _assign(payload, "ignore_text", ignore_text)
        _assign(payload, "trigger_text", trigger_text)
        _assign(payload, "notification_urls", notification_urls)
        _assign(payload, "paused", paused)
        interval = _interval(check_weeks, check_days, check_hours, check_minutes, check_seconds)
        if interval is not None:
            payload["time_between_check"] = interval
            payload["time_between_check_use_default"] = False
        created = _call(client.create_watch, payload)
        watch_uuid = str(created.get("uuid", ""))
        watch = _call(client.get_watch, watch_uuid) if watch_uuid else None
        return {"uuid": watch_uuid, "watch": watch}

    @mcp.tool(annotations=_WRITE)
    def update_watch(
        uuid: str,
        url: str | None = None,
        title: str | None = None,
        tag: str | None = None,
        tags: list[str] | None = None,
        check_weeks: int | None = None,
        check_days: int | None = None,
        check_hours: int | None = None,
        check_minutes: int | None = None,
        check_seconds: int | None = None,
        include_filters: list[str] | None = None,
        subtractive_selectors: list[str] | None = None,
        ignore_text: list[str] | None = None,
        trigger_text: list[str] | None = None,
        notification_urls: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update fields on an existing watch. Only arguments you pass are changed.

        `tag` replaces the watch's tags using comma-separated names (missing names are created).
        `tags` replaces them with these UUIDs. Passing both unions the two sets.
        An empty `tag` together with an empty `tags` list clears tags.
        Any `check_*` value replaces the whole interval and disables the system default.
        Pass an empty list to clear filters, selectors, ignore/trigger text, or notification URLs.
        """
        fields: dict[str, Any] = {}
        _assign(fields, "url", url)
        _assign(fields, "title", title)
        _assign(fields, "include_filters", include_filters)
        _assign(fields, "subtractive_selectors", subtractive_selectors)
        _assign(fields, "ignore_text", ignore_text)
        _assign(fields, "trigger_text", trigger_text)
        _assign(fields, "notification_urls", notification_urls)
        if tag is not None or tags is not None:
            names = [part.strip() for part in (tag or "").split(",") if part.strip()]
            resolved = _call(client.ensure_tag_uuids, names) if names else []
            fields["tags"] = list(dict.fromkeys([*(tags or []), *resolved]))
        interval = _interval(check_weeks, check_days, check_hours, check_minutes, check_seconds)
        if interval is not None:
            fields["time_between_check"] = interval
            fields["time_between_check_use_default"] = False
        if not fields:
            raise ToolError("No fields to update. Pass at least one of url, title, tag, tags, interval, filters, or notification_urls.")
        _call(client.update_watch, uuid, fields)
        return {"uuid": uuid, "updated": sorted(fields), "watch": _call(client.get_watch, uuid)}

    @mcp.tool(annotations=_DELETE)
    def delete_watch(uuid: str) -> dict[str, Any]:
        """Delete a watch and its stored history."""
        _call(client.delete_watch, uuid)
        return {"uuid": uuid, "deleted": True}

    @mcp.tool(annotations=_WRITE)
    def pause_watch(uuid: str) -> dict[str, Any]:
        """Pause checks for a watch."""
        _call(client.set_watch_state, uuid, paused="paused")
        return {"uuid": uuid, "paused": True}

    @mcp.tool(annotations=_WRITE)
    def unpause_watch(uuid: str) -> dict[str, Any]:
        """Resume checks for a paused watch."""
        _call(client.set_watch_state, uuid, paused="unpaused")
        return {"uuid": uuid, "paused": False}

    @mcp.tool(annotations=_WRITE)
    def mute_watch(uuid: str) -> dict[str, Any]:
        """Mute change notifications for a watch. Checks continue."""
        _call(client.set_watch_state, uuid, muted="muted")
        return {"uuid": uuid, "notification_muted": True}

    @mcp.tool(annotations=_WRITE)
    def unmute_watch(uuid: str) -> dict[str, Any]:
        """Unmute change notifications for a watch."""
        _call(client.set_watch_state, uuid, muted="unmuted")
        return {"uuid": uuid, "notification_muted": False}

    @mcp.tool(annotations=_WRITE)
    def recheck_watch(uuid: str) -> dict[str, Any]:
        """Queue an immediate recheck of one watch."""
        _call(client.set_watch_state, uuid, recheck=True)
        return {"uuid": uuid, "recheck_queued": True}

    @mcp.tool(annotations=_READ)
    def list_tags() -> dict[str, Any]:
        """List tags keyed by UUID (title, notification_muted, date_created)."""
        tags = _call(client.list_tags)
        return {"count": len(tags), "tags": tags}

    @mcp.tool(annotations=_READ)
    def get_tag(uuid: str) -> dict[str, Any]:
        """Get one tag by UUID."""
        return _call(client.get_tag, uuid)

    @mcp.tool(annotations=_CREATE)
    def create_tag(title: str) -> dict[str, Any]:
        """Create a tag. If a tag with this title already exists, its UUID is returned."""
        created = _call(client.create_tag, title)
        return {"uuid": created.get("uuid"), "title": title}

    @mcp.tool(annotations=_WRITE)
    def update_tag(uuid: str, title: str | None = None, notification_muted: bool | None = None) -> dict[str, Any]:
        """Rename a tag and/or set whether its notifications are muted."""
        fields: dict[str, Any] = {}
        _assign(fields, "title", title)
        _assign(fields, "notification_muted", notification_muted)
        if not fields:
            raise ToolError("Pass title and/or notification_muted to update a tag.")
        _call(client.update_tag, uuid, fields)
        return {"uuid": uuid, "tag": _call(client.get_tag, uuid)}

    @mcp.tool(annotations=_DELETE)
    def delete_tag(uuid: str) -> dict[str, Any]:
        """Delete a tag and remove it from every watch."""
        _call(client.delete_tag, uuid)
        return {"uuid": uuid, "deleted": True}

    @mcp.tool(annotations=_WRITE)
    def mute_tag(uuid: str) -> dict[str, Any]:
        """Mute notifications for every watch that has this tag."""
        _call(client.set_tag_muted, uuid, True)
        return {"uuid": uuid, "notification_muted": True}

    @mcp.tool(annotations=_WRITE)
    def unmute_tag(uuid: str) -> dict[str, Any]:
        """Unmute notifications for a tag."""
        _call(client.set_tag_muted, uuid, False)
        return {"uuid": uuid, "notification_muted": False}

    @mcp.tool(annotations=_READ)
    def get_system_info() -> dict[str, Any]:
        """Return version, uptime, watch count, queue size, and overdue watch UUIDs."""
        return _call(client.get_system_info)


def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except ChangeDetectionError as exc:
        raise ToolError(str(exc)) from exc


def _assign(payload: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        payload[key] = value


def _interval(
    weeks: int | None,
    days: int | None,
    hours: int | None,
    minutes: int | None,
    seconds: int | None,
) -> dict[str, int | None] | None:
    values: dict[str, int | None] = {
        "weeks": weeks,
        "days": days,
        "hours": hours,
        "minutes": minutes,
        "seconds": seconds,
    }
    if all(value is None for value in values.values()):
        return None
    return values


def _sorted_timestamps(history: dict[str, str]) -> list[str]:
    def sort_key(timestamp: str) -> tuple[int, int | str]:
        try:
            return (0, int(timestamp))
        except ValueError:
            return (1, timestamp)

    return sorted(history, key=sort_key)


def _resolve_timestamp(
    client: ChangeDetectionClient,
    uuid: str,
    timestamp: str,
    history: dict[str, str] | None = None,
) -> str:
    if timestamp not in {"latest", "previous"}:
        return timestamp
    snapshots = history if history is not None else _call(client.get_history, uuid)
    timestamps = _sorted_timestamps(snapshots)
    if not timestamps:
        raise ToolError(f"Watch {uuid} has no snapshots yet.")
    if timestamp == "latest":
        return timestamps[-1]
    if len(timestamps) < 2:
        raise ToolError(f"Watch {uuid} has no previous snapshot yet.")
    return timestamps[-2]


def _limit(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _diff_counts(diff: str) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions
