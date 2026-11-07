"""Named notification groups and per-alert routing.

A notification group is a reusable list of the Apprise destination URLs this
app already knows how to send. Groups do not add delivery providers.

The workers already emit three alerts. Restock and price changes use the
content-change alert; they are not separate notification types.

Fallback for one alert, first match wins:

1. The watch's group for that alert, unless the watch is muted.
2. The first tag on the watch that sets a group for that alert, unless that tag is muted.
   Tag order matches the order tags are stored in, which is the same order the
   existing notification URL cascade uses.
3. The system group for that alert.
4. The existing destination list. Content-change keeps today's watch, then tag,
   then system URL cascade, including mute. Filter-not-found and browser-step
   alerts keep today's behavior: the watch URL list, then the system URL list,
   without applying mute.

A selected group replaces the destination list for that alert. The list is not
added to the legacy URLs, so a destination is not notified twice. An existing
group with no destinations notifies nobody for that alert. A missing or deleted
group is skipped and the next level is used. Legacy URL lists are left in place
and are used whenever no group applies.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

EVENT_CONTENT_CHANGED = "content_changed"
EVENT_FILTER_FAILURE = "filter_failure"
EVENT_STEP_FAILURE = "step_failure"

NOTIFICATION_EVENTS: dict[str, dict[str, str]] = {
    EVENT_CONTENT_CHANGED: {
        "label": "Content change",
        "description": (
            "The checked page changed. Restock and price alerts use this same notification."
        ),
    },
    EVENT_FILTER_FAILURE: {
        "label": "Filter not found",
        "description": (
            "A CSS, XPath, JSONPath, or jq filter was missing for the configured number of checks."
        ),
    },
    EVENT_STEP_FAILURE: {
        "label": "Browser step failed",
        "description": "A configured browser step could not be run.",
    },
}

INHERITANCE_TEXT = (
    "For each alert, one destination list is chosen. "
    "A watch group for that alert is used first, unless the watch is muted. "
    "Otherwise the first tag that sets a group for that alert is used, unless that tag is muted. "
    "Otherwise the system group for that alert is used. "
    "Otherwise the existing notification URL list is used: for a content change that is the watch, "
    "then its tags, then Settings → Notifications; for filter-not-found and browser-step alerts "
    "that is the watch list, then the system list. "
    "A selected group replaces the URL list for that alert only, so the same destination is not "
    "notified twice. A group with no destinations notifies nobody for that alert. "
    "A missing or deleted group is skipped. Saving a group does not change existing notification URL lists."
)

_BLOCKED_SCHEMES = {"file", "javascript", "data", "vbscript"}
_KEEP_PATH_SEGMENTS = {"api", "webhooks", "webhook", "notify", "v1", "v2", "hooks", "services"}
_ROUTE_FORM_PREFIX = "notification_group_route_"


@dataclass
class NotificationResolution:
    """Where the destinations for one alert came from."""

    event: str
    urls: list[str]
    source: str
    group_uuid: str | None = None
    group_title: str | None = None
    tag_uuid: str | None = None
    skipped_groups: list[dict[str, str]] = field(default_factory=list)
    duplicates_removed: int = 0
    muted_levels: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        group = None
        if self.group_uuid:
            group = {"uuid": self.group_uuid, "title": self.group_title}
        return {
            "event": self.event,
            "source": self.source,
            "group": group,
            "tag_uuid": self.tag_uuid,
            "destination_count": len(self.urls),
            "destinations": [redact_destination(url) for url in self.urls],
            "duplicates_removed": self.duplicates_removed,
            "skipped_groups": list(self.skipped_groups),
            "muted_levels": list(self.muted_levels),
        }


def event_catalog() -> list[dict[str, str]]:
    return [
        {"id": event_id, "label": meta["label"], "description": meta["description"]}
        for event_id, meta in NOTIFICATION_EVENTS.items()
    ]


def redact_destination(url: str) -> str:
    """Hide credentials and path tokens while keeping the scheme and hostname.

    Applying this twice returns the same string.
    """
    text = (url or "").strip()
    if not text or text.startswith("#"):
        return text
    if "://" not in text:
        return "***"
    scheme, rest = text.split("://", 1)
    rest = re.sub(
        r"(?<=:)[^/@\s]+(?=@)",
        lambda match: "***" if set(match.group(0)) <= {"*"} else "***",
        rest,
    )

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
    """Mask a password embedded in a page URL. The host and path stay so the page is recognizable."""
    text = (url or "").strip()
    if "://" not in text:
        return text
    scheme, rest = text.split("://", 1)
    rest = re.sub(r"(?<=:)[^/@\s]+(?=@)", "***", rest)
    return f"{scheme}://{rest}"


def redact_headers(headers: Mapping[str, Any] | None) -> dict[str, Any]:
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


def public_group(group: Mapping[str, Any]) -> dict[str, Any]:
    urls = [str(url) for url in (group.get("notification_urls") or [])]
    destinations = [url if url.startswith("#") else redact_destination(url) for url in urls]
    return {
        "uuid": group.get("uuid"),
        "title": group.get("title") or "",
        "date_created": group.get("date_created") or 0,
        "destination_count": len([url for url in urls if url and not url.startswith("#")]),
        "destinations": destinations,
    }


def validate_destination(url: str) -> str:
    """Accept one existing Apprise-style destination and reject local or script URLs."""
    cleaned = (url or "").strip()
    if not cleaned:
        raise ValueError("A notification destination cannot be empty.")
    if cleaned.startswith("#"):
        if len(cleaned) > 5000:
            raise ValueError("A notification comment is too long.")
        return cleaned
    if any(character in cleaned for character in "\r\n\t "):
        raise ValueError("Put each notification destination on its own line, without spaces.")
    if "://" not in cleaned:
        raise ValueError(
            "A notification destination needs a scheme, for example mailto://alerts@example.com or discord://..."
        )
    scheme = cleaned.split("://", 1)[0].lower()
    if scheme in _BLOCKED_SCHEMES or not re.fullmatch(r"[a-z][a-z0-9+.-]*", scheme):
        raise ValueError(f"The destination scheme {scheme!r} is not allowed.")
    if len(cleaned) > 5000:
        raise ValueError("A notification destination is too long.")
    return cleaned


def validate_destinations(urls: list[str]) -> list[str]:
    cleaned = [validate_destination(url) for url in urls]
    _reject_unknown_apprise(cleaned)
    return cleaned


def _reject_unknown_apprise(urls: list[str]) -> None:
    """Use Apprise's own URL parser when it is installed. Do not contact the destination."""
    try:
        import apprise
        from changedetectionio.notification.apprise_plugin.assets import apprise_asset
        from changedetectionio.notification.apprise_plugin.custom_handlers import apprise_http_custom_handler  # noqa: F401
    except Exception:
        return
    apobj = apprise.Apprise(asset=apprise_asset)
    for url in urls:
        if url.startswith("#"):
            continue
        if not apobj.add(url):
            raise ValueError(f"{redact_destination(url)} is not a valid notification URL.")


def validate_title(title: str, groups: Mapping[str, Mapping[str, Any]], *, ignore_uuid: str | None = None) -> str:
    cleaned = " ".join((title or "").split())
    if not cleaned:
        raise ValueError("A notification group needs a name.")
    if len(cleaned) > 200:
        raise ValueError("A notification group name must be 200 characters or fewer.")
    wanted = cleaned.lower()
    for group_uuid, group in groups.items():
        if ignore_uuid and group_uuid == ignore_uuid:
            continue
        if str(group.get("title") or "").strip().lower() == wanted:
            raise ValueError(f"A notification group named {cleaned!r} already exists.")
    return cleaned


def ensure_storage(application: dict[str, Any]) -> None:
    application.setdefault("notification_groups", {})
    application.setdefault("notification_group_routes", {})


def create_group(application: dict[str, Any], title: str, urls: list[str]) -> dict[str, Any]:
    ensure_storage(application)
    groups = application["notification_groups"]
    cleaned_title = validate_title(title, groups)
    cleaned_urls = validate_destinations(urls)
    group_uuid = str(uuid.uuid4())
    group = {
        "uuid": group_uuid,
        "title": cleaned_title,
        "notification_urls": cleaned_urls,
        "date_created": int(time.time()),
    }
    groups[group_uuid] = group
    return group


def update_group(
    application: dict[str, Any],
    group_uuid: str,
    *,
    title: str | None = None,
    urls: list[str] | None = None,
) -> dict[str, Any]:
    ensure_storage(application)
    group = application["notification_groups"].get(group_uuid)
    if not group:
        raise KeyError(group_uuid)
    if title is not None:
        group["title"] = validate_title(title, application["notification_groups"], ignore_uuid=group_uuid)
    if urls is not None:
        group["notification_urls"] = validate_destinations(urls)
    return group


def add_destinations(application: dict[str, Any], group_uuid: str, urls: list[str]) -> list[str]:
    group = _require_group(application, group_uuid)
    cleaned = validate_destinations(urls)
    current = list(group.get("notification_urls") or [])
    added: list[str] = []
    for url in cleaned:
        if url not in current:
            current.append(url)
            added.append(url)
    group["notification_urls"] = current
    return added


def remove_destinations(application: dict[str, Any], group_uuid: str, urls: list[str]) -> list[str]:
    group = _require_group(application, group_uuid)
    wanted = {validate_destination(url) for url in urls}
    current = list(group.get("notification_urls") or [])
    removed = [url for url in current if url in wanted]
    if not removed:
        raise ValueError("None of those destinations are in this notification group.")
    group["notification_urls"] = [url for url in current if url not in wanted]
    return removed


def group_references(application: Mapping[str, Any], watches: Mapping[str, Any], group_uuid: str) -> dict[str, list[str]]:
    global_events = [
        event
        for event, assigned in (application.get("notification_group_routes") or {}).items()
        if assigned == group_uuid
    ]
    tags = [
        tag_uuid
        for tag_uuid, tag in (application.get("tags") or {}).items()
        if group_uuid in (tag.get("notification_group_routes") or {}).values()
    ]
    watch_ids = [
        watch_uuid
        for watch_uuid, watch in watches.items()
        if group_uuid in (watch.get("notification_group_routes") or {}).values()
    ]
    return {"global_events": global_events, "tags": tags, "watches": watch_ids}


def delete_group(application: dict[str, Any], watches: Mapping[str, Any], group_uuid: str) -> dict[str, list[str]]:
    ensure_storage(application)
    if group_uuid not in application["notification_groups"]:
        raise KeyError(group_uuid)
    references = group_references(application, watches, group_uuid)
    routes = application.get("notification_group_routes") or {}
    for event in list(references["global_events"]):
        routes.pop(event, None)
    for tag_uuid in references["tags"]:
        tag_routes = application["tags"][tag_uuid].get("notification_group_routes") or {}
        for event, assigned in list(tag_routes.items()):
            if assigned == group_uuid:
                tag_routes.pop(event, None)
    for watch_uuid in references["watches"]:
        watch_routes = watches[watch_uuid].get("notification_group_routes") or {}
        for event, assigned in list(watch_routes.items()):
            if assigned == group_uuid:
                watch_routes.pop(event, None)
    del application["notification_groups"][group_uuid]
    return references


def merge_routes(current: Mapping[str, Any] | None, patch: Mapping[str, Any], groups: Mapping[str, Any]) -> dict[str, str]:
    updated = {str(event): str(group_uuid) for event, group_uuid in (current or {}).items() if group_uuid}
    for event, group_uuid in patch.items():
        if event not in NOTIFICATION_EVENTS:
            known = ", ".join(NOTIFICATION_EVENTS)
            raise ValueError(f"Unknown alert type {event!r}. Use one of: {known}.")
        if group_uuid is None or str(group_uuid).strip() == "":
            updated.pop(event, None)
            continue
        assigned = str(group_uuid).strip()
        if assigned not in groups:
            raise ValueError(
                f"No notification group with id {assigned}. Create the group first, then assign its id."
            )
        updated[event] = assigned
    return updated


def routes_from_form(form: Mapping[str, Any], application: Mapping[str, Any]) -> dict[str, str] | None:
    """Read the dropdowns. An empty selection clears that alert.

    Returns None when the form did not include the dropdowns, so a partial
    submit does not wipe assignments.
    """
    if not form_submitted_routes(form):
        return None
    patch = {}
    for event in NOTIFICATION_EVENTS:
        field_name = f"{_ROUTE_FORM_PREFIX}{event}"
        if field_name not in form:
            continue
        value = form.get(field_name)
        if isinstance(value, (list, tuple)):
            value = value[-1] if value else ""
        patch[event] = "" if value is None else str(value)
    return merge_routes({}, patch, application.get("notification_groups") or {})


def form_submitted_routes(form: Mapping[str, Any]) -> bool:
    return any(f"{_ROUTE_FORM_PREFIX}{event}" in form for event in NOTIFICATION_EVENTS)


def resolve_notification_targets(
    application: Mapping[str, Any],
    watch: Mapping[str, Any],
    tags: Mapping[str, Mapping[str, Any]],
    event: str,
) -> NotificationResolution:
    if event not in NOTIFICATION_EVENTS:
        known = ", ".join(NOTIFICATION_EVENTS)
        raise ValueError(f"Unknown alert type {event!r}. Use one of: {known}.")

    groups = application.get("notification_groups") or {}
    skipped: list[dict[str, str]] = []
    muted: list[str] = []

    def take_group(group_uuid: str | None, source: str, tag_uuid: str | None = None) -> NotificationResolution | None:
        if not group_uuid:
            return None
        group = groups.get(group_uuid)
        if not group:
            skipped.append({"uuid": str(group_uuid), "reason": "missing"})
            return None
        urls, removed = _dedupe(group.get("notification_urls") or [])
        return NotificationResolution(
            event=event,
            urls=urls,
            source=source,
            group_uuid=str(group_uuid),
            group_title=str(group.get("title") or ""),
            tag_uuid=tag_uuid,
            skipped_groups=list(skipped),
            duplicates_removed=removed,
            muted_levels=list(muted),
        )

    watch_routes = watch.get("notification_group_routes") or {}
    watch_group = watch_routes.get(event)
    if watch.get("notification_muted") and watch_group:
        muted.append("watch")
    elif not watch.get("notification_muted"):
        chosen = take_group(watch_group, "watch_group")
        if chosen:
            return chosen

    for tag_uuid, tag in tags.items():
        tag_group = (tag.get("notification_group_routes") or {}).get(event)
        if not tag_group:
            continue
        if tag.get("notification_muted"):
            muted.append(f"tag:{tag_uuid}")
            continue
        chosen = take_group(tag_group, "tag_group", tag_uuid=str(tag_uuid))
        if chosen:
            return chosen

    chosen = take_group((application.get("notification_group_routes") or {}).get(event), "global_group")
    if chosen:
        return chosen

    if event == EVENT_CONTENT_CHANGED:
        return _legacy_content_changed(application, watch, tags, event, skipped, muted)
    return _legacy_watch_then_global(application, watch, event, skipped, muted)


def explain_watch(
    application: Mapping[str, Any],
    watch: Mapping[str, Any],
    tags: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    routes = watch.get("notification_group_routes") or {}
    return {
        "uuid": watch.get("uuid"),
        "events": event_catalog(),
        "inheritance": INHERITANCE_TEXT,
        "routes": {event: routes.get(event) for event in NOTIFICATION_EVENTS},
        "resolution": {
            event: resolve_notification_targets(application, watch, tags, event).public()
            for event in NOTIFICATION_EVENTS
        },
    }


def _legacy_content_changed(application, watch, tags, event, skipped, muted) -> NotificationResolution:
    if not watch.get("notification_muted"):
        urls, removed = _dedupe(watch.get("notification_urls") or [])
        if urls:
            return _legacy(event, urls, "watch_urls", removed, skipped, muted)
    else:
        if watch.get("notification_urls"):
            muted.append("watch")
    for tag_uuid, tag in tags.items():
        if tag.get("notification_muted"):
            if tag.get("notification_urls"):
                muted.append(f"tag:{tag_uuid}")
            continue
        urls, removed = _dedupe(tag.get("notification_urls") or [])
        if urls:
            return _legacy(event, urls, "tag_urls", removed, skipped, muted, tag_uuid=str(tag_uuid))
    urls, removed = _dedupe(application.get("notification_urls") or [])
    if urls:
        return _legacy(event, urls, "global_urls", removed, skipped, muted)
    return _legacy(event, [], "none", 0, skipped, muted)


def _legacy_watch_then_global(application, watch, event, skipped, muted) -> NotificationResolution:
    # Filter and browser-step alerts historically ignore mute when choosing URL lists.
    urls, removed = _dedupe(watch.get("notification_urls") or [])
    if urls:
        return _legacy(event, urls, "watch_urls", removed, skipped, muted)
    urls, removed = _dedupe(application.get("notification_urls") or [])
    if urls:
        return _legacy(event, urls, "global_urls", removed, skipped, muted)
    return _legacy(event, [], "none", 0, skipped, muted)


def _legacy(event, urls, source, removed, skipped, muted, tag_uuid=None) -> NotificationResolution:
    return NotificationResolution(
        event=event,
        urls=urls,
        source=source,
        tag_uuid=tag_uuid,
        skipped_groups=list(skipped),
        duplicates_removed=removed,
        muted_levels=list(muted),
    )


def _dedupe(urls: list[Any]) -> tuple[list[str], int]:
    seen: set[str] = set()
    chosen: list[str] = []
    removed = 0
    for url in urls:
        text = str(url).strip()
        if not text or text.startswith("#"):
            continue
        if text in seen:
            removed += 1
            continue
        seen.add(text)
        chosen.append(text)
    return chosen, removed


def _require_group(application: dict[str, Any], group_uuid: str) -> dict[str, Any]:
    ensure_storage(application)
    group = application["notification_groups"].get(group_uuid)
    if not group:
        raise KeyError(group_uuid)
    return group
