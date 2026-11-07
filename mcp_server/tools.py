"""MCP tool registration.

Existing tool names stay the same. New tools cover actions the REST API already
supports, including notification groups. This module does not import the Flask app.
"""

from __future__ import annotations

import difflib
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from mcp_server.client import ChangeDetectionClient
from mcp_server.present import (
    call_api,
    page,
    present_destinations,
    present_summary,
    present_watch,
    redact_headers,
    redact_userinfo,
    require_confirm,
    require_id,
    require_ids,
    validate_destinations,
    validate_routes,
    validate_watch_url,
    watch_summary,
)

_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
_CREATE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
_DELETE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)
_BULK_DELETE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)

_METHODS = ("GET", "POST", "PUT", "DELETE")
_BACKENDS = ("html_requests", "html_webdriver")
_PROCESSORS = ("text_json_diff", "restock_diff")
_FORMATS = ("Text", "Markdown", "HTML", "HTML Color", "System default")
_SORTS = ("title", "url", "last_changed")


def register_tools(mcp: MCPServer, client: ChangeDetectionClient) -> None:
    @mcp.tool(annotations=_READ)
    def list_watches(
        tag: str | None = None,
        only_errors: bool = False,
        only_unviewed: bool = False,
        url_contains: str | None = None,
        sort: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """List watches, optionally limited to a tag name.

        Returns a summary (url, title, last_checked, last_changed, last_error, viewed)
        keyed by watch UUID. `tag` is a tag title, not a UUID. The list payload does
        not include paused state; use get_watch for that. `count` is the number of
        matches before paging. Omit `limit` to return every match (at most the API
        returns). `limit` is 1–200.
        """
        watches = call_api(client.list_watches, tag)
        items = list(watches.items())
        if only_errors:
            items = [(watch_uuid, row) for watch_uuid, row in items if row.get("last_error")]
        if only_unviewed:
            items = [(watch_uuid, row) for watch_uuid, row in items if not row.get("viewed")]
        if url_contains:
            needle = url_contains.lower()
            items = [(watch_uuid, row) for watch_uuid, row in items if needle in str(row.get("url") or "").lower()]
        if sort:
            if sort not in _SORTS:
                raise ToolError("sort must be title, url, or last_changed.")
            items.sort(key=lambda item: str(item[1].get(sort) or "").lower())
        window, meta = page(items, offset=offset, limit=limit)
        return {
            "count": meta["total"],
            "page": meta,
            "watches": {watch_uuid: present_summary(watch_uuid, row) for watch_uuid, row in window},
        }

    @mcp.tool(annotations=_READ)
    def search_watches(
        query: str,
        tag: str | None = None,
        partial: bool = False,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Search watches by URL or title.

        Without `partial`, the query must match the whole URL or title (case-insensitive).
        With `partial`, it matches a substring of the URL, title, or last error.
        `tag` is an optional tag title that limits the search.
        """
        watches = call_api(client.search_watches, query, tag, partial)
        items = list(watches.items())
        window, meta = page(items, offset=offset, limit=limit)
        return {
            "count": meta["total"],
            "query": query,
            "partial": partial,
            "page": meta,
            "watches": {watch_uuid: present_summary(watch_uuid, row) for watch_uuid, row in window},
        }

    @mcp.tool(annotations=_READ)
    def get_watch(uuid: str) -> dict[str, Any]:
        """Get one watch, including filters, tags, interval, and notification settings.

        Notification destinations and secret headers are redacted. History bodies are
        not included; use get_watch_history, get_watch_snapshot, or get_watch_diff.
        Passwords in the watched page URL are redacted; the host and path stay.
        """
        watch = present_watch(call_api(client.get_watch, require_id(uuid, "watch id")))
        watch["summary"] = watch_summary(watch)
        return watch

    @mcp.tool(annotations=_READ)
    def get_watch_history(uuid: str, limit: int | None = None) -> dict[str, Any]:
        """List stored text snapshots for a watch.

        Timestamps are Unix seconds as strings, oldest first. `latest` and `previous`
        are set when those snapshots exist. `limit` keeps the newest 1–200 timestamps.
        Paths are files on the changedetection server.
        """
        watch_uuid = require_id(uuid, "watch id")
        history = call_api(client.get_history, watch_uuid)
        timestamps = _sorted_timestamps(history)
        if limit is not None:
            if limit < 1 or limit > 200:
                raise ToolError("limit must be from 1 to 200, or omit it to return every snapshot.")
            timestamps = timestamps[-limit:]
        return {
            "uuid": watch_uuid,
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
        watch_uuid = require_id(uuid, "watch id")
        resolved = _resolve_timestamp(client, watch_uuid, timestamp)
        content = call_api(client.get_snapshot, watch_uuid, resolved, html=html)
        text, truncated = _limit(content, max_chars)
        return {
            "uuid": watch_uuid,
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
        watch_uuid = require_id(uuid, "watch id")
        history = call_api(client.get_history, watch_uuid)
        timestamps = _sorted_timestamps(history)
        if from_timestamp is None:
            if len(timestamps) < 2:
                return {
                    "uuid": watch_uuid,
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
            older = _resolve_timestamp(client, watch_uuid, from_timestamp, history=history)
            newer = _resolve_timestamp(client, watch_uuid, to_timestamp, history=history)
        before = call_api(client.get_snapshot, watch_uuid, older)
        after = call_api(client.get_snapshot, watch_uuid, newer)
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
            "uuid": watch_uuid,
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
        use_default_check_interval: bool | None = None,
        include_filters: list[str] | None = None,
        subtractive_selectors: list[str] | None = None,
        ignore_text: list[str] | None = None,
        trigger_text: list[str] | None = None,
        notification_urls: list[str] | None = None,
        paused: bool | None = None,
        method: str | None = None,
        fetch_backend: str | None = None,
        processor: str | None = None,
        headers: dict[str, str] | None = None,
        body: str | None = None,
        proxy: str | None = None,
        notification_title: str | None = None,
        notification_body: str | None = None,
        notification_format: str | None = None,
        notification_screenshot: bool | None = None,
        notification_muted: bool | None = None,
        extract_text: list[str] | None = None,
        text_should_not_be_present: list[str] | None = None,
        filter_text_added: bool | None = None,
        filter_text_replaced: bool | None = None,
        filter_text_removed: bool | None = None,
        check_unique_lines: bool | None = None,
        remove_duplicate_lines: bool | None = None,
        sort_text_alphabetically: bool | None = None,
        trim_text_whitespace: bool | None = None,
        extract_title_as_title: bool | None = None,
        filter_failure_notification_send: bool | None = None,
        track_ldjson_price_data: bool | None = None,
        follow_price_changes: bool | None = None,
        in_stock_only: bool | None = None,
        webdriver_delay: int | None = None,
        browser_steps: list[dict[str, Any]] | None = None,
        webdriver_js_execute_code: str | None = None,
    ) -> dict[str, Any]:
        """Create a watch and queue its first check.

        `tag` is a comma-separated list of tag names. Names that do not exist are created.
        `tags` is a list of existing tag UUIDs.
        Any `check_*` value sets a custom interval and turns off the system default interval.
        Do not pass `check_*` together with `use_default_check_interval`.
        `include_filters` selects content (CSS, XPath, `json:` JSONPath, or `jq:`).
        `notification_urls` are existing Apprise destinations for this watch.
        `browser_steps` and `webdriver_js_execute_code` are stored for the fetcher.
        This tool does not run them. Assign notification groups with set_watch_notification_routes.
        The returned watch redacts destinations and secret headers.
        """
        payload = _watch_payload(
            url=url,
            title=title,
            tag=tag,
            tags=tags,
            check_weeks=check_weeks,
            check_days=check_days,
            check_hours=check_hours,
            check_minutes=check_minutes,
            check_seconds=check_seconds,
            use_default_check_interval=use_default_check_interval,
            include_filters=include_filters,
            subtractive_selectors=subtractive_selectors,
            ignore_text=ignore_text,
            trigger_text=trigger_text,
            notification_urls=notification_urls,
            paused=paused,
            method=method,
            fetch_backend=fetch_backend,
            processor=processor,
            headers=headers,
            body=body,
            proxy=proxy,
            notification_title=notification_title,
            notification_body=notification_body,
            notification_format=notification_format,
            notification_screenshot=notification_screenshot,
            notification_muted=notification_muted,
            extract_text=extract_text,
            text_should_not_be_present=text_should_not_be_present,
            filter_text_added=filter_text_added,
            filter_text_replaced=filter_text_replaced,
            filter_text_removed=filter_text_removed,
            check_unique_lines=check_unique_lines,
            remove_duplicate_lines=remove_duplicate_lines,
            sort_text_alphabetically=sort_text_alphabetically,
            trim_text_whitespace=trim_text_whitespace,
            extract_title_as_title=extract_title_as_title,
            filter_failure_notification_send=filter_failure_notification_send,
            track_ldjson_price_data=track_ldjson_price_data,
            follow_price_changes=follow_price_changes,
            in_stock_only=in_stock_only,
            webdriver_delay=webdriver_delay,
            browser_steps=browser_steps,
            webdriver_js_execute_code=webdriver_js_execute_code,
            require_url=True,
        )
        created = call_api(client.create_watch, payload)
        watch_uuid = str(created.get("uuid", ""))
        watch = present_watch(call_api(client.get_watch, watch_uuid)) if watch_uuid else None
        return {"uuid": watch_uuid, "watch": watch, "summary": watch_summary(watch or {})}

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
        use_default_check_interval: bool | None = None,
        include_filters: list[str] | None = None,
        subtractive_selectors: list[str] | None = None,
        ignore_text: list[str] | None = None,
        trigger_text: list[str] | None = None,
        notification_urls: list[str] | None = None,
        paused: bool | None = None,
        method: str | None = None,
        fetch_backend: str | None = None,
        processor: str | None = None,
        headers: dict[str, str] | None = None,
        body: str | None = None,
        proxy: str | None = None,
        notification_title: str | None = None,
        notification_body: str | None = None,
        notification_format: str | None = None,
        notification_screenshot: bool | None = None,
        notification_muted: bool | None = None,
        extract_text: list[str] | None = None,
        text_should_not_be_present: list[str] | None = None,
        filter_text_added: bool | None = None,
        filter_text_replaced: bool | None = None,
        filter_text_removed: bool | None = None,
        check_unique_lines: bool | None = None,
        remove_duplicate_lines: bool | None = None,
        sort_text_alphabetically: bool | None = None,
        trim_text_whitespace: bool | None = None,
        extract_title_as_title: bool | None = None,
        filter_failure_notification_send: bool | None = None,
        track_ldjson_price_data: bool | None = None,
        follow_price_changes: bool | None = None,
        in_stock_only: bool | None = None,
        webdriver_delay: int | None = None,
        browser_steps: list[dict[str, Any]] | None = None,
        webdriver_js_execute_code: str | None = None,
    ) -> dict[str, Any]:
        """Update fields on an existing watch. Only arguments you pass are changed.

        `tag` replaces the watch's tags using comma-separated names (missing names are created).
        `tags` replaces them with these UUIDs. Passing both unions the two sets.
        An empty `tag` together with an empty `tags` list clears tags.
        Any `check_*` value replaces the whole interval and disables the system default.
        `use_default_check_interval` turns the system interval back on. Do not combine it with `check_*`.
        Pass an empty list to clear filters, selectors, ignore/trigger text, or notification URLs.
        `browser_steps` and `webdriver_js_execute_code` are stored only; this tool does not run them.
        Notification group assignment is a separate tool, set_watch_notification_routes.
        """
        watch_uuid = require_id(uuid, "watch id")
        fields = _watch_payload(
            url=url,
            title=title,
            tag=None,
            tags=None,
            check_weeks=check_weeks,
            check_days=check_days,
            check_hours=check_hours,
            check_minutes=check_minutes,
            check_seconds=check_seconds,
            use_default_check_interval=use_default_check_interval,
            include_filters=include_filters,
            subtractive_selectors=subtractive_selectors,
            ignore_text=ignore_text,
            trigger_text=trigger_text,
            notification_urls=notification_urls,
            paused=paused,
            method=method,
            fetch_backend=fetch_backend,
            processor=processor,
            headers=headers,
            body=body,
            proxy=proxy,
            notification_title=notification_title,
            notification_body=notification_body,
            notification_format=notification_format,
            notification_screenshot=notification_screenshot,
            notification_muted=notification_muted,
            extract_text=extract_text,
            text_should_not_be_present=text_should_not_be_present,
            filter_text_added=filter_text_added,
            filter_text_replaced=filter_text_replaced,
            filter_text_removed=filter_text_removed,
            check_unique_lines=check_unique_lines,
            remove_duplicate_lines=remove_duplicate_lines,
            sort_text_alphabetically=sort_text_alphabetically,
            trim_text_whitespace=trim_text_whitespace,
            extract_title_as_title=extract_title_as_title,
            filter_failure_notification_send=filter_failure_notification_send,
            track_ldjson_price_data=track_ldjson_price_data,
            follow_price_changes=follow_price_changes,
            in_stock_only=in_stock_only,
            webdriver_delay=webdriver_delay,
            browser_steps=browser_steps,
            webdriver_js_execute_code=webdriver_js_execute_code,
            require_url=False,
        )
        if tag is not None or tags is not None:
            names = [part.strip() for part in (tag or "").split(",") if part.strip()]
            resolved = call_api(client.ensure_tag_uuids, names) if names else []
            fields["tags"] = list(dict.fromkeys([*(tags or []), *resolved]))
        if not fields:
            raise ToolError(
                "No fields to update. Pass at least one of url, title, tag, tags, interval, filters, or notification settings."
            )
        call_api(client.update_watch, watch_uuid, fields)
        watch = present_watch(call_api(client.get_watch, watch_uuid))
        return {"uuid": watch_uuid, "updated": sorted(fields), "watch": watch, "summary": watch_summary(watch)}

    @mcp.tool(annotations=_DELETE)
    def delete_watch(uuid: str) -> dict[str, Any]:
        """Delete one watch and its stored history.

        Kept without a confirm flag so existing clients keep working. The tool is
        still marked destructive. Use delete_watches, which requires confirm, for more than one.
        """
        watch_uuid = require_id(uuid, "watch id")
        call_api(client.delete_watch, watch_uuid)
        return {"uuid": watch_uuid, "deleted": True}

    @mcp.tool(annotations=_WRITE)
    def pause_watch(uuid: str) -> dict[str, Any]:
        """Pause checks for a watch."""
        watch_uuid = require_id(uuid, "watch id")
        call_api(client.set_watch_state, watch_uuid, paused="paused")
        return {"uuid": watch_uuid, "paused": True}

    @mcp.tool(annotations=_WRITE)
    def unpause_watch(uuid: str) -> dict[str, Any]:
        """Resume checks for a paused watch."""
        watch_uuid = require_id(uuid, "watch id")
        call_api(client.set_watch_state, watch_uuid, paused="unpaused")
        return {"uuid": watch_uuid, "paused": False}

    @mcp.tool(annotations=_WRITE)
    def mute_watch(uuid: str) -> dict[str, Any]:
        """Mute change notifications for a watch. Checks continue."""
        watch_uuid = require_id(uuid, "watch id")
        call_api(client.set_watch_state, watch_uuid, muted="muted")
        return {"uuid": watch_uuid, "notification_muted": True}

    @mcp.tool(annotations=_WRITE)
    def unmute_watch(uuid: str) -> dict[str, Any]:
        """Unmute change notifications for a watch."""
        watch_uuid = require_id(uuid, "watch id")
        call_api(client.set_watch_state, watch_uuid, muted="unmuted")
        return {"uuid": watch_uuid, "notification_muted": False}

    @mcp.tool(annotations=_WRITE)
    def recheck_watch(uuid: str) -> dict[str, Any]:
        """Queue an immediate recheck of one watch."""
        watch_uuid = require_id(uuid, "watch id")
        call_api(client.set_watch_state, watch_uuid, recheck=True)
        return {"uuid": watch_uuid, "recheck_queued": True}

    @mcp.tool(annotations=_WRITE)
    def pause_watches(uuids: list[str], confirm: bool) -> dict[str, Any]:
        """Pause checks for up to 50 watches. Requires confirm=true."""
        return _each(uuids, confirm, "pause these watches", lambda watch_uuid: call_api(client.set_watch_state, watch_uuid, paused="paused"))

    @mcp.tool(annotations=_WRITE)
    def unpause_watches(uuids: list[str], confirm: bool) -> dict[str, Any]:
        """Resume checks for up to 50 watches. Requires confirm=true."""
        return _each(uuids, confirm, "resume these watches", lambda watch_uuid: call_api(client.set_watch_state, watch_uuid, paused="unpaused"))

    @mcp.tool(annotations=_WRITE)
    def mute_watches(uuids: list[str], confirm: bool) -> dict[str, Any]:
        """Mute notifications for up to 50 watches. Requires confirm=true."""
        return _each(uuids, confirm, "mute these watches", lambda watch_uuid: call_api(client.set_watch_state, watch_uuid, muted="muted"))

    @mcp.tool(annotations=_WRITE)
    def unmute_watches(uuids: list[str], confirm: bool) -> dict[str, Any]:
        """Unmute notifications for up to 50 watches. Requires confirm=true."""
        return _each(uuids, confirm, "unmute these watches", lambda watch_uuid: call_api(client.set_watch_state, watch_uuid, muted="unmuted"))

    @mcp.tool(annotations=_WRITE)
    def recheck_watches(uuids: list[str], confirm: bool) -> dict[str, Any]:
        """Queue a recheck for up to 50 watches. Requires confirm=true. Does not delete anything."""
        return _each(uuids, confirm, "recheck these watches", lambda watch_uuid: call_api(client.set_watch_state, watch_uuid, recheck=True))

    @mcp.tool(annotations=_WRITE)
    def recheck_all_watches(confirm: bool) -> dict[str, Any]:
        """Queue a recheck of every watch, including paused watches.

        The REST API queues paused watches too. Requires confirm=true. This does not
        send a notification by itself; a later change check may.
        """
        require_confirm(confirm, "queue a recheck of every watch, including paused watches")
        result = call_api(client.recheck_all_watches)
        return {
            "summary": "Queued a recheck of every watch, including paused watches.",
            "includes_paused": True,
            "result": result,
        }

    @mcp.tool(annotations=_BULK_DELETE)
    def delete_watches(uuids: list[str], confirm: bool) -> dict[str, Any]:
        """Delete up to 50 watches and their history. Requires confirm=true.

        Already-deleted ids are reported in `failed` and do not roll back the ones that succeeded.
        """
        return _each(uuids, confirm, "delete these watches and their history", lambda watch_uuid: call_api(client.delete_watch, watch_uuid))

    @mcp.tool(annotations=_CREATE)
    def import_watches(
        urls: list[str],
        confirm: bool,
        tag: str | None = None,
        dedupe: bool = True,
    ) -> dict[str, Any]:
        """Import http(s) watch URLs. Requires confirm=true.

        Every URL is checked before any watch is created. `tag` is a tag name applied
        to the new watches. `dedupe` skips URLs that are already watched. This is the
        plaintext URL import, not the Excel or Distill.io file import.
        """
        require_confirm(confirm, "import these URLs as watches")
        if not urls:
            raise ToolError("Pass at least one http:// or https:// URL.")
        cleaned = [validate_watch_url(url) for url in urls]
        created = call_api(client.import_watches, cleaned, tag=tag, dedupe=dedupe)
        return {"summary": f"Imported {len(created)} watches.", "uuids": created, "dedupe": dedupe}

    @mcp.tool(annotations=_READ)
    def export_watch_urls(
        tag: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Export watched page URLs as text. Passwords in those URLs are redacted.

        This is not the zip backup. The zip contains secret.txt and is not exposed.
        `tag` limits the export to one tag title.
        """
        watches = call_api(client.list_watches, tag)
        rows = [
            {"uuid": watch_uuid, "title": row.get("title") or "", "url": redact_userinfo(str(row.get("url") or ""))}
            for watch_uuid, row in watches.items()
        ]
        window, meta = page(rows, offset=offset, limit=limit)
        return {**meta, "urls": "\n".join(row["url"] for row in window), "watches": window}

    @mcp.tool(annotations=_READ)
    def list_tags() -> dict[str, Any]:
        """List tags keyed by UUID (title, notification_muted, date_created).

        The main menu calls tags Groups. Notification groups are a different list.
        """
        tags = call_api(client.list_tags)
        presented = {}
        for tag_uuid, tag in tags.items():
            row = dict(tag)
            if isinstance(row.get("notification_urls"), list):
                row["notification_urls"] = present_destinations(row["notification_urls"])
            presented[tag_uuid] = row
        return {"count": len(presented), "tags": presented}

    @mcp.tool(annotations=_READ)
    def get_tag(uuid: str) -> dict[str, Any]:
        """Get one tag by UUID. Notification destinations on the tag are redacted."""
        tag = call_api(client.get_tag, require_id(uuid, "tag id"))
        if isinstance(tag.get("notification_urls"), list):
            tag["notification_urls"] = present_destinations(tag["notification_urls"])
        return tag

    @mcp.tool(annotations=_CREATE)
    def create_tag(title: str) -> dict[str, Any]:
        """Create a tag. If a tag with this title already exists, its UUID is returned."""
        created = call_api(client.create_tag, title)
        return {"uuid": created.get("uuid"), "title": title}

    @mcp.tool(annotations=_WRITE)
    def update_tag(uuid: str, title: str | None = None, notification_muted: bool | None = None) -> dict[str, Any]:
        """Rename a tag and/or set whether its notifications are muted."""
        tag_uuid = require_id(uuid, "tag id")
        fields: dict[str, Any] = {}
        _assign(fields, "title", title)
        _assign(fields, "notification_muted", notification_muted)
        if not fields:
            raise ToolError("Pass title and/or notification_muted to update a tag.")
        call_api(client.update_tag, tag_uuid, fields)
        return {"uuid": tag_uuid, "tag": call_api(client.get_tag, tag_uuid)}

    @mcp.tool(annotations=_DELETE)
    def delete_tag(uuid: str) -> dict[str, Any]:
        """Delete a tag and remove it from every watch."""
        tag_uuid = require_id(uuid, "tag id")
        call_api(client.delete_tag, tag_uuid)
        return {"uuid": tag_uuid, "deleted": True}

    @mcp.tool(annotations=_WRITE)
    def mute_tag(uuid: str) -> dict[str, Any]:
        """Mute notifications for every watch that has this tag."""
        tag_uuid = require_id(uuid, "tag id")
        call_api(client.set_tag_muted, tag_uuid, True)
        return {"uuid": tag_uuid, "notification_muted": True}

    @mcp.tool(annotations=_WRITE)
    def unmute_tag(uuid: str) -> dict[str, Any]:
        """Unmute notifications for a tag."""
        tag_uuid = require_id(uuid, "tag id")
        call_api(client.set_tag_muted, tag_uuid, False)
        return {"uuid": tag_uuid, "notification_muted": False}

    @mcp.tool(annotations=_READ)
    def get_system_info() -> dict[str, Any]:
        """Return version, uptime, watch count, queue size, and overdue watch UUIDs."""
        return call_api(client.get_system_info)

    @mcp.tool(annotations=_READ)
    def list_notification_urls() -> dict[str, Any]:
        """List the system notification URL fallback. Destinations are redacted.

        This list is used when no notification group is selected for an alert.
        """
        urls = present_destinations(call_api(client.list_notification_urls))
        return {"count": len(urls), "notification_urls": urls}

    @mcp.tool(annotations=_WRITE)
    def add_notification_urls(notification_urls: list[str]) -> dict[str, Any]:
        """Add system notification destinations. The response redacts them.

        These are the existing Apprise URLs. This does not add a new delivery service
        and does not send a test notification.
        """
        cleaned = validate_destinations(notification_urls)
        added = present_destinations(call_api(client.add_notification_urls, cleaned))
        return {"summary": f"Added {len(added)} system notification destinations.", "notification_urls": added}

    @mcp.tool(annotations=_DELETE)
    def remove_notification_urls(notification_urls: list[str], confirm: bool) -> dict[str, Any]:
        """Remove system notification destinations. Requires confirm=true.

        Pass the original URLs, not the redacted copies from list_notification_urls.
        """
        require_confirm(confirm, "remove these system notification destinations")
        cleaned = validate_destinations(notification_urls)
        call_api(client.remove_notification_urls, cleaned)
        return {"summary": f"Removed {len(cleaned)} system notification destinations.", "removed": len(cleaned)}

    @mcp.tool(annotations=_DELETE)
    def replace_notification_urls(notification_urls: list[str], confirm: bool) -> dict[str, Any]:
        """Replace the whole system notification URL list. Requires confirm=true.

        An empty list clears it. Pass original URLs. The response is redacted.
        Watches and tags that set their own URLs or groups are unchanged.
        """
        require_confirm(confirm, "replace the system notification URL list")
        cleaned = validate_destinations(notification_urls) if notification_urls else []
        saved = present_destinations(call_api(client.replace_notification_urls, cleaned))
        return {"summary": f"System notification list now has {len(saved)} destinations.", "notification_urls": saved}

    @mcp.tool(annotations=_READ)
    def list_notification_groups() -> dict[str, Any]:
        """List reusable notification groups, the three real alert types, and how inheritance works.

        A group is a named list of existing Apprise destinations. It is not a watch tag.
        Alert types are content_changed (this includes restock and price changes),
        filter_failure, and step_failure. Destinations are redacted.
        """
        payload = call_api(client.list_notification_groups)
        groups = {group_uuid: _present_group(group) for group_uuid, group in (payload.get("groups") or {}).items()}
        return {
            "count": len(groups),
            "events": payload.get("events") or [],
            "inheritance": payload.get("inheritance") or "",
            "groups": groups,
        }

    @mcp.tool(annotations=_READ)
    def get_notification_group(uuid: str) -> dict[str, Any]:
        """Get one notification group and how many watches, tags, and system alerts use it."""
        payload = call_api(client.get_notification_group, require_id(uuid, "notification group id"))
        payload["group"] = _present_group(payload.get("group") or {})
        return payload

    @mcp.tool(annotations=_CREATE)
    def create_notification_group(title: str, notification_urls: list[str]) -> dict[str, Any]:
        """Create a notification group. Titles must be unique, ignoring case.

        An empty destination list is allowed: selecting that group notifies nobody for that alert.
        Destinations are existing Apprise URLs. The response redacts them. A duplicate title
        is rejected; it does not silently reuse another group.
        """
        if not title.strip():
            raise ToolError("Give the notification group a title.")
        cleaned = validate_destinations(notification_urls) if notification_urls else []
        created = call_api(client.create_notification_group, title.strip(), cleaned)
        created["group"] = _present_group(created.get("group") or {})
        created["summary"] = f"Created notification group {title.strip()}."
        return created

    @mcp.tool(annotations=_WRITE)
    def update_notification_group(uuid: str, title: str) -> dict[str, Any]:
        """Rename a notification group. Destinations are changed with the destination tools."""
        group_uuid = require_id(uuid, "notification group id")
        if not title.strip():
            raise ToolError("Give the notification group a title.")
        updated = call_api(client.update_notification_group, group_uuid, {"title": title.strip()})
        updated["group"] = _present_group(updated.get("group") or {})
        return updated

    @mcp.tool(annotations=_WRITE)
    def add_notification_group_destinations(uuid: str, notification_urls: list[str]) -> dict[str, Any]:
        """Add destinations to a notification group. The response is redacted.

        Pass original Apprise URLs. This does not send a notification.
        """
        group_uuid = require_id(uuid, "notification group id")
        cleaned = validate_destinations(notification_urls)
        updated = call_api(client.add_notification_group_destinations, group_uuid, cleaned)
        updated["group"] = _present_group(updated.get("group") or {})
        return updated

    @mcp.tool(annotations=_DELETE)
    def remove_notification_group_destinations(uuid: str, notification_urls: list[str], confirm: bool) -> dict[str, Any]:
        """Remove destinations from a notification group. Requires confirm=true.

        Pass the original URLs, not the redacted copies returned by the list tools.
        """
        require_confirm(confirm, "remove these destinations from the notification group")
        group_uuid = require_id(uuid, "notification group id")
        cleaned = validate_destinations(notification_urls)
        updated = call_api(client.remove_notification_group_destinations, group_uuid, cleaned)
        updated["group"] = _present_group(updated.get("group") or {})
        return updated

    @mcp.tool(annotations=_DELETE)
    def replace_notification_group_destinations(uuid: str, notification_urls: list[str], confirm: bool) -> dict[str, Any]:
        """Replace every destination in a notification group. Requires confirm=true.

        An empty list makes the group notify nobody when it is selected. It does not
        fall through to another list. Pass original URLs. Redacted URLs cannot be saved back.
        """
        require_confirm(confirm, "replace this notification group's destinations")
        group_uuid = require_id(uuid, "notification group id")
        cleaned = validate_destinations(notification_urls) if notification_urls else []
        updated = call_api(
            client.update_notification_group,
            group_uuid,
            {"notification_urls": cleaned, "confirm": True},
        )
        updated["group"] = _present_group(updated.get("group") or {})
        return updated

    @mcp.tool(annotations=_DELETE)
    def delete_notification_group(uuid: str, confirm: bool) -> dict[str, Any]:
        """Delete a notification group and clear it from watches, tags, and system routes.

        Requires confirm=true. Alerts that used it inherit the next level. Other groups
        and existing notification URL lists are kept.
        """
        require_confirm(confirm, "delete this notification group and clear its assignments")
        group_uuid = require_id(uuid, "notification group id")
        return call_api(client.delete_notification_group, group_uuid)

    @mcp.tool(annotations=_READ)
    def get_global_notification_routes() -> dict[str, Any]:
        """Show which notification group the system uses for each alert, plus inheritance text."""
        return call_api(client.get_global_notification_routes)

    @mcp.tool(annotations=_WRITE)
    def set_global_notification_routes(routes: dict[str, Any]) -> dict[str, Any]:
        """Assign system notification groups. Omitted alerts stay as they are.

        Keys are content_changed, filter_failure, and step_failure. A value is a group
        UUID. null or "" makes that alert inherit the system notification URL list.
        Restock and price changes are content_changed. A group replaces the URL list
        for that alert, so the same destination is not notified twice.
        """
        saved = call_api(client.set_global_notification_routes, validate_routes(routes))
        return {"summary": "Updated system notification groups.", "routes": saved.get("routes", saved)}

    @mcp.tool(annotations=_READ)
    def get_watch_notification_routes(uuid: str) -> dict[str, Any]:
        """Explain which group or URL list each alert on this watch will use.

        Includes the saved dropdown values, the resolved destinations (redacted),
        skipped missing groups, and muted levels.
        """
        return _redact_explain(call_api(client.get_watch_notification_routes, require_id(uuid, "watch id")))

    @mcp.tool(annotations=_WRITE)
    def set_watch_notification_routes(uuid: str, routes: dict[str, Any]) -> dict[str, Any]:
        """Choose a notification group for each alert on one watch.

        A muted watch skips its own group and uses the next level. null or "" clears
        one alert back to inheritance. Omitted alerts stay unchanged.
        """
        watch_uuid = require_id(uuid, "watch id")
        saved = call_api(client.set_watch_notification_routes, watch_uuid, validate_routes(routes))
        return {"uuid": watch_uuid, "summary": "Updated watch notification groups.", "routes": saved.get("routes", saved)}

    @mcp.tool(annotations=_READ)
    def get_tag_notification_routes(uuid: str) -> dict[str, Any]:
        """Show the notification group assigned to each alert on a tag."""
        return call_api(client.get_tag_notification_routes, require_id(uuid, "tag id"))

    @mcp.tool(annotations=_WRITE)
    def set_tag_notification_routes(uuid: str, routes: dict[str, Any]) -> dict[str, Any]:
        """Choose a notification group for each alert on a tag.

        Watches with this tag use it when the watch itself does not set a group for
        that alert and the tag is not muted. null or "" clears one alert.
        """
        tag_uuid = require_id(uuid, "tag id")
        saved = call_api(client.set_tag_notification_routes, tag_uuid, validate_routes(routes))
        return {"uuid": tag_uuid, "summary": "Updated tag notification groups.", "routes": saved.get("routes", saved)}

    @mcp.tool(annotations=_READ)
    def explain_watch_notifications(uuid: str) -> dict[str, Any]:
        """Same explanation as get_watch_notification_routes, for a single watch.

        Use this to see which group or legacy URL list would be used for
        content_changed, filter_failure, and step_failure without sending anything.
        """
        return _redact_explain(call_api(client.get_watch_notification_routes, require_id(uuid, "watch id")))


def _each(uuids: list[str], confirm: bool, verb: str, action) -> dict[str, Any]:
    require_confirm(confirm, verb)
    succeeded = []
    failed = []
    for watch_uuid in require_ids(uuids, "watch id"):
        try:
            action(watch_uuid)
            succeeded.append(watch_uuid)
        except ToolError as exc:
            failed.append({"uuid": watch_uuid, "error": str(exc)})
    return {
        "summary": f"{verb}: {len(succeeded)} succeeded, {len(failed)} failed.",
        "succeeded": succeeded,
        "failed": failed,
    }


def _watch_payload(**values: Any) -> dict[str, Any]:
    require_url = values.pop("require_url")
    payload: dict[str, Any] = {}
    url = values.get("url")
    if require_url or url is not None:
        payload["url"] = validate_watch_url(url or "")
    interval_names = ("check_weeks", "check_days", "check_hours", "check_minutes", "check_seconds")
    interval = _interval(*(values.get(name) for name in interval_names))
    use_default = values.get("use_default_check_interval")
    if use_default and interval is not None:
        raise ToolError("Pass either check interval fields or use_default_check_interval, not both.")
    if use_default:
        payload["time_between_check_use_default"] = True
    elif interval is not None:
        for name, value in interval.items():
            if value is not None and value < 0:
                raise ToolError(f"check_{name} cannot be negative.")
        payload["time_between_check"] = interval
        payload["time_between_check_use_default"] = False

    if values.get("method") is not None:
        payload["method"] = _choice(values["method"], _METHODS, "method")
    if values.get("fetch_backend") is not None:
        payload["fetch_backend"] = _choice(values["fetch_backend"], _BACKENDS, "fetch_backend")
    if values.get("processor") is not None:
        payload["processor"] = _choice(values["processor"], _PROCESSORS, "processor")
    if values.get("notification_format") is not None:
        payload["notification_format"] = _choice(values["notification_format"], _FORMATS, "notification_format")
    if values.get("headers") is not None:
        headers = values["headers"]
        if not isinstance(headers, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in headers.items()):
            raise ToolError("headers must be an object of header name to string value.")
        payload["headers"] = headers
    if values.get("browser_steps") is not None:
        payload["browser_steps"] = _browser_steps(values["browser_steps"])
    if values.get("notification_urls") is not None:
        payload["notification_urls"] = validate_destinations(values["notification_urls"]) if values["notification_urls"] else []
    if values.get("webdriver_delay") is not None and values["webdriver_delay"] < 0:
        raise ToolError("webdriver_delay cannot be negative.")
    if values.get("webdriver_js_execute_code") is not None and len(values["webdriver_js_execute_code"]) > 5000:
        raise ToolError("webdriver_js_execute_code is longer than 5000 characters. It is stored, not run by this tool.")

    direct = (
        "title",
        "tag",
        "tags",
        "include_filters",
        "subtractive_selectors",
        "ignore_text",
        "trigger_text",
        "paused",
        "body",
        "proxy",
        "notification_title",
        "notification_body",
        "notification_screenshot",
        "notification_muted",
        "extract_text",
        "text_should_not_be_present",
        "filter_text_added",
        "filter_text_replaced",
        "filter_text_removed",
        "check_unique_lines",
        "remove_duplicate_lines",
        "sort_text_alphabetically",
        "trim_text_whitespace",
        "extract_title_as_title",
        "filter_failure_notification_send",
        "track_ldjson_price_data",
        "follow_price_changes",
        "in_stock_only",
        "webdriver_delay",
        "webdriver_js_execute_code",
    )
    for key in direct:
        _assign(payload, key, values.get(key))
    return payload


def _browser_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(steps, list):
        raise ToolError("browser_steps must be a list. Each step needs operation, selector, and optional_value.")
    cleaned = []
    for step in steps:
        if not isinstance(step, dict) or set(step) - {"operation", "selector", "optional_value"}:
            raise ToolError("Each browser step needs only operation, selector, and optional_value.")
        if any(key not in step for key in ("operation", "selector", "optional_value")):
            raise ToolError("Each browser step needs operation, selector, and optional_value.")
        cleaned.append({key: step[key] for key in ("operation", "selector", "optional_value")})
    return cleaned


def _choice(value: str, allowed: tuple[str, ...], label: str) -> str:
    if value not in allowed:
        raise ToolError(f"{label} must be one of: {', '.join(allowed)}.")
    return value


def _present_group(group: dict[str, Any]) -> dict[str, Any]:
    data = dict(group)
    if isinstance(data.get("destinations"), list):
        data["destinations"] = present_destinations(data["destinations"])
    if isinstance(data.get("notification_urls"), list):
        data["notification_urls"] = present_destinations(data["notification_urls"])
    return data


def _redact_explain(document: dict[str, Any]) -> dict[str, Any]:
    resolution = document.get("resolution") or {}
    for row in resolution.values():
        if isinstance(row, dict) and isinstance(row.get("destinations"), list):
            row["destinations"] = present_destinations(row["destinations"])
    if isinstance(document.get("headers"), dict):
        document["headers"] = redact_headers(document["headers"])
    return document


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
        return require_id(timestamp, "snapshot timestamp")
    snapshots = history if history is not None else call_api(client.get_history, uuid)
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
