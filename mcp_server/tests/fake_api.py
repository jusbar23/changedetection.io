"""In-process stand-in for changedetection.io ``/api/v1``."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


@dataclass
class APIState:
    api_key: str = "test-key"
    watches: dict[str, dict[str, Any]] = field(default_factory=dict)
    tags: dict[str, dict[str, Any]] = field(default_factory=dict)
    snapshots: dict[str, dict[str, str]] = field(default_factory=dict)
    notification_urls: list[str] = field(default_factory=list)
    notification_groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    notification_routes: dict[str, str] = field(default_factory=dict)


class _APIServer(ThreadingHTTPServer):
    def __init__(self, state: APIState):
        self.api_state = state
        self.api_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _Handler)


class FakeChangeDetectionAPI:
    """Threaded HTTP server implementing the endpoints the MCP server calls."""

    def __init__(self, api_key: str = "test-key"):
        self.state = APIState(api_key=api_key)
        self._httpd = _APIServer(self.state)
        self.port = self._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def api_key(self) -> str:
        return self.state.api_key

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._thread.join(timeout=5)
        self._httpd.server_close()

    def add_snapshot(self, watch_uuid: str, timestamp: str, text: str) -> None:
        with self._httpd.api_lock:
            self.state.snapshots.setdefault(watch_uuid, {})[str(timestamp)] = text


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._route("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._route("DELETE")

    @property
    def _state(self) -> APIState:
        return self.server.api_state  # type: ignore[attr-defined]

    def _route(self, method: str) -> None:
        if not self._authorized():
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        parts = [part for part in path.split("/") if part]
        body, raw_text = self._read_body()
        with self.server.api_lock:  # type: ignore[attr-defined]
            self._dispatch(method, parts, query, body, raw_text)

    def _authorized(self) -> bool:
        if not self._state.api_key:
            return True
        if self.headers.get("x-api-key") == self._state.api_key:
            return True
        self._send(403, {"message": "Invalid access - API key invalid."})
        return False

    def _read_body(self) -> tuple[dict[str, Any], str]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}, ""
        text = raw.decode()
        try:
            data = json.loads(text)
        except ValueError:
            return {}, text
        return (data if isinstance(data, dict) else {}), text

    def _dispatch(self, method: str, parts: list[str], query: dict[str, list[str]], body: dict[str, Any], raw_text: str) -> None:
        if parts == ["api", "v1", "systeminfo"] and method == "GET":
            self._send(200, self._system_info())
            return
        if parts == ["api", "v1", "search"] and method == "GET":
            self._search(query)
            return
        if parts == ["api", "v1", "notifications"] and method == "GET":
            self._send(200, {"notification_urls": list(self._state.notification_urls)})
            return
        if parts == ["api", "v1", "notifications"] and method in {"POST", "PUT", "DELETE"}:
            self._notifications(method, body)
            return
        if parts == ["api", "v1", "notification-groups"] and method == "GET":
            self._send(200, _group_list(self._state))
            return
        if parts == ["api", "v1", "notification-groups"] and method == "POST":
            self._create_group(body)
            return
        if parts == ["api", "v1", "notification-routes"] and method == "GET":
            self._send(200, _route_document(self._state.notification_routes))
            return
        if parts == ["api", "v1", "notification-routes"] and method == "PUT":
            try:
                self._state.notification_routes = _merge_routes(
                    self._state.notification_routes, body.get("routes") or {}, self._state
                )
            except ValueError as exc:
                self._send(400, {"message": str(exc)})
                return
            self._send(200, {"routes": self._state.notification_routes})
            return
        if parts == ["api", "v1", "import"] and method == "POST":
            self._import(query, raw_text)
            return
        if parts == ["api", "v1", "tags"] and method == "GET":
            self._send(200, self._state.tags)
            return
        if parts == ["api", "v1", "tag"] and method == "POST":
            self._create_tag(body)
            return
        if len(parts) == 5 and parts[:3] == ["api", "v1", "tag"] and parts[4] == "notification-routes":
            self._tag_routes(method, parts[3], body)
            return
        if len(parts) == 4 and parts[:3] == ["api", "v1", "tag"]:
            self._one_tag(method, parts[3], query, body)
            return
        if len(parts) >= 4 and parts[:3] == ["api", "v1", "notification-groups"]:
            self._group_routes(method, parts[3:], query, body)
            return
        if parts == ["api", "v1", "watch"] and method == "GET":
            self._list_watches(query)
            return
        if parts == ["api", "v1", "watch"] and method == "POST":
            self._create_watch(body)
            return
        if len(parts) >= 4 and parts[:3] == ["api", "v1", "watch"]:
            self._watch_collection(method, parts[3:], query, body)
            return
        self._send(404, {"message": "No such API route"})

    def _system_info(self) -> dict[str, Any]:
        return {
            "queue_size": 0,
            "overdue_watches": [],
            "uptime": 12.5,
            "watch_count": len(self._state.watches),
            "version": "test",
        }

    def _search(self, query: dict[str, list[str]]) -> None:
        text = _first(query, "q").strip()
        if not text:
            self._send(400, {"message": "Search query 'q' parameter is required"})
            return
        partial = _first(query, "partial").lower() in {"1", "true", "yes", "on"}
        tag_uuid = self._tag_uuid_by_title(_first(query, "tag"))
        if _first(query, "tag").strip() and tag_uuid is None:
            self._send(200, {})
            return
        needle = text.lower()
        results = {}
        for watch_uuid, watch in self._state.watches.items():
            if tag_uuid and tag_uuid not in watch.get("tags", []):
                continue
            if _search_hit(watch, needle, partial):
                results[watch_uuid] = _summary(watch)
        self._send(200, results)

    def _list_watches(self, query: dict[str, list[str]]) -> None:
        if _first(query, "recheck_all"):
            for watch in self._state.watches.values():
                watch["last_checked"] = 1
            self._send(200, {"status": "OK"})
            return
        tag_limit = _first(query, "tag").strip().lower()
        results = {}
        for watch_uuid, watch in self._state.watches.items():
            if tag_limit and tag_limit not in self._tag_titles(watch):
                continue
            results[watch_uuid] = _summary(watch)
        self._send(200, results)

    def _create_watch(self, body: dict[str, Any]) -> None:
        url = str(body.get("url", "")).strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            self._send(400, "Invalid or unsupported URL")
            return
        watch_uuid = str(uuid.uuid4())
        tag_uuids = list(body.get("tags") or [])
        for name in _split_names(body.get("tag")):
            tag_uuids.append(self._ensure_tag(name))
        self._state.watches[watch_uuid] = _new_watch(watch_uuid, url, body, tag_uuids)
        self._send(201, {"uuid": watch_uuid})

    def _watch_collection(self, method: str, rest: list[str], query: dict[str, list[str]], body: dict[str, Any]) -> None:
        watch_uuid = rest[0]
        if len(rest) == 1:
            self._watch_document(method, watch_uuid, query, body)
            return
        if len(rest) >= 2 and rest[1] == "notification-routes":
            self._watch_notification_routes(method, watch_uuid, body)
            return
        if len(rest) >= 2 and rest[1] == "history":
            self._watch_history(method, watch_uuid, rest[2:], query)
            return
        self._send(404, {"message": "No such API route"})

    def _watch_document(self, method: str, watch_uuid: str, query: dict[str, list[str]], body: dict[str, Any]) -> None:
        watch = self._state.watches.get(watch_uuid)
        if method == "GET":
            self._get_watch(watch, watch_uuid, query)
            return
        if method == "PUT":
            if watch is None:
                self._send(404, {"message": f"No watch exists with the UUID of {watch_uuid}"})
                return
            watch.update(body)
            self._send(200, "OK")
            return
        if method == "DELETE":
            self._delete_watch(watch, watch_uuid)
            return
        self._send(404, {"message": "No such API route"})

    def _get_watch(self, watch: dict[str, Any] | None, watch_uuid: str, query: dict[str, list[str]]) -> None:
        if watch is None:
            self._send(404, {"message": f"No watch exists with the UUID of {watch_uuid}"})
            return
        if _first(query, "recheck"):
            watch["last_checked"] = 1
            self._send(200, "OK")
            return
        if self._apply_flag(watch, query):
            return
        self._send(200, dict(watch))

    def _apply_flag(self, watch: dict[str, Any], query: dict[str, list[str]]) -> bool:
        paused = _first(query, "paused")
        muted = _first(query, "muted")
        if paused == "paused":
            watch["paused"] = True
        elif paused == "unpaused":
            watch["paused"] = False
        elif muted == "muted":
            watch["notification_muted"] = True
        elif muted == "unmuted":
            watch["notification_muted"] = False
        else:
            return False
        self._send(200, "OK")
        return True

    def _delete_watch(self, watch: dict[str, Any] | None, watch_uuid: str) -> None:
        if watch is None:
            self._send(400, {"message": f"No watch exists with the UUID of {watch_uuid}"})
            return
        del self._state.watches[watch_uuid]
        self._state.snapshots.pop(watch_uuid, None)
        self._send(204, None)

    def _watch_history(self, method: str, watch_uuid: str, rest: list[str], query: dict[str, list[str]]) -> None:
        if watch_uuid not in self._state.watches:
            self._send(404, {"message": f"No watch exists with the UUID of {watch_uuid}"})
            return
        snapshots = self._state.snapshots.get(watch_uuid, {})
        if method != "GET":
            self._send(404, {"message": "No such API route"})
            return
        if not rest:
            self._send(200, {ts: f"/datastore/{watch_uuid}/{ts}.txt" for ts in snapshots})
            return
        self._send_snapshot(watch_uuid, rest[0], snapshots, query)

    def _send_snapshot(self, watch_uuid: str, timestamp: str, snapshots: dict[str, str], query: dict[str, list[str]]) -> None:
        if timestamp == "latest":
            if not snapshots:
                self._send(404, {"message": f"Watch found but no history exists for the UUID {watch_uuid}"})
                return
            timestamp = sorted(snapshots, key=lambda item: int(item))[-1]
        content = snapshots.get(timestamp)
        if content is None:
            self._send(404, {"message": "No content found"})
            return
        content_type = "text/html" if _first(query, "html") else "text/plain"
        self._send(200, content, content_type=content_type)

    def _create_tag(self, body: dict[str, Any]) -> None:
        title = str(body.get("title", "")).strip()
        if not title:
            self._send(400, "Invalid or unsupported tag")
            return
        self._send(201, {"uuid": self._ensure_tag(title)})

    def _one_tag(self, method: str, tag_uuid: str, query: dict[str, list[str]], body: dict[str, Any]) -> None:
        tag = self._state.tags.get(tag_uuid)
        if method == "GET":
            self._get_tag(tag, tag_uuid, query)
            return
        if method == "PUT":
            if tag is None:
                self._send(404, {"message": f"No tag exists with the UUID of {tag_uuid}"})
                return
            tag.update(body)
            self._send(200, "OK")
            return
        if method == "DELETE":
            self._delete_tag(tag, tag_uuid)
            return
        self._send(404, {"message": "No such API route"})

    def _get_tag(self, tag: dict[str, Any] | None, tag_uuid: str, query: dict[str, list[str]]) -> None:
        if tag is None:
            self._send(404, {"message": f"No tag exists with the UUID of {tag_uuid}"})
            return
        muted = _first(query, "muted")
        if muted == "muted":
            tag["notification_muted"] = True
            self._send(200, "OK")
            return
        if muted == "unmuted":
            tag["notification_muted"] = False
            self._send(200, "OK")
            return
        self._send(200, dict(tag))

    def _delete_tag(self, tag: dict[str, Any] | None, tag_uuid: str) -> None:
        if tag is None:
            self._send(400, {"message": f"No tag exists with the UUID of {tag_uuid}"})
            return
        del self._state.tags[tag_uuid]
        for watch in self._state.watches.values():
            watch["tags"] = [item for item in watch.get("tags") or [] if item != tag_uuid]
        self._send(204, None)

    def _ensure_tag(self, title: str) -> str:
        wanted = title.strip().lower()
        for tag_uuid, tag in self._state.tags.items():
            if str(tag.get("title", "")).strip().lower() == wanted:
                return tag_uuid
        tag_uuid = str(uuid.uuid4())
        self._state.tags[tag_uuid] = {
            "uuid": tag_uuid,
            "title": title.strip(),
            "notification_muted": False,
            "notification_urls": [],
            "notification_group_routes": {},
            "date_created": 1,
        }
        return tag_uuid

    def _tag_uuid_by_title(self, title: str) -> str | None:
        wanted = title.strip().lower()
        if not wanted:
            return None
        for tag_uuid, tag in self._state.tags.items():
            if str(tag.get("title", "")).strip().lower() == wanted:
                return tag_uuid
        return None

    def _tag_titles(self, watch: dict[str, Any]) -> list[str]:
        titles = []
        for tag_uuid in watch.get("tags", []):
            tag = self._state.tags.get(tag_uuid)
            if tag:
                titles.append(str(tag.get("title", "")).lower())
        return titles

    def _notifications(self, method: str, body: dict[str, Any]) -> None:
        urls = [str(url) for url in (body.get("notification_urls") or [])]
        if method == "POST":
            added = []
            for url in urls:
                if url not in self._state.notification_urls:
                    self._state.notification_urls.append(url)
                    added.append(url)
            if not added:
                self._send(400, "No valid notification URLs were added")
                return
            self._send(201, {"notification_urls": added})
            return
        if method == "PUT":
            self._state.notification_urls = urls
            self._send(200, {"notification_urls": urls})
            return
        self._state.notification_urls = [url for url in self._state.notification_urls if url not in urls]
        self._send(204, None)

    def _create_group(self, body: dict[str, Any]) -> None:
        title = str(body.get("title") or "").strip()
        urls = body.get("notification_urls")
        if not title or not isinstance(urls, list):
            self._send(400, {"message": "title and notification_urls are required."})
            return
        for group in self._state.notification_groups.values():
            if str(group.get("title", "")).strip().lower() == title.lower():
                self._send(409, {"message": "A notification group with that title already exists."})
                return
        group_uuid = str(uuid.uuid4())
        group = {"uuid": group_uuid, "title": title, "notification_urls": [str(url) for url in urls], "date_created": 1}
        self._state.notification_groups[group_uuid] = group
        self._send(201, {"uuid": group_uuid, "group": _routing().public_group(group)})

    def _group_routes(self, method: str, parts: list[str], query: dict[str, list[str]], body: dict[str, Any]) -> None:
        group_uuid = parts[0]
        group = self._state.notification_groups.get(group_uuid)
        if len(parts) == 2 and parts[1] == "destinations":
            if group is None:
                self._send(404, {"message": f"No notification group exists with the UUID of {group_uuid}"})
                return
            urls = [str(url) for url in (body.get("notification_urls") or [])]
            if method == "POST":
                added = [url for url in urls if url not in group["notification_urls"]]
                group["notification_urls"].extend(added)
                self._send(200, {"added": len(added), "group": _routing().public_group(group)})
                return
            if method == "DELETE":
                if body.get("confirm") is not True:
                    self._send(400, {"message": "Pass confirm=true. This changes who receives alerts."})
                    return
                removed = [url for url in urls if url in group["notification_urls"]]
                group["notification_urls"] = [url for url in group["notification_urls"] if url not in urls]
                self._send(200, {"removed": len(removed), "group": _routing().public_group(group)})
                return
        if len(parts) != 1:
            self._send(404, {"message": "No such API route"})
            return
        if group is None:
            self._send(404, {"message": f"No notification group exists with the UUID of {group_uuid}"})
            return
        if method == "GET":
            references = _group_references(self._state, group_uuid)
            self._send(200, {
                "group": _routing().public_group(group),
                "references": {
                    "watches": len(references["watches"]),
                    "tags": len(references["tags"]),
                    "global_events": references["global_events"],
                },
            })
            return
        if method == "PUT":
            if "notification_urls" in body and body.get("confirm") is not True:
                self._send(400, {"message": "Pass confirm=true. This changes who receives alerts."})
                return
            if "title" in body:
                group["title"] = str(body["title"]).strip()
            if "notification_urls" in body:
                group["notification_urls"] = [str(url) for url in body["notification_urls"]]
            self._send(200, {"group": _routing().public_group(group)})
            return
        if method == "DELETE":
            if _first(query, "confirm") != "true":
                self._send(400, {"message": "Pass confirm=true. This deletes the group and clears it from watches and tags."})
                return
            references = _clear_group(self._state, group_uuid)
            del self._state.notification_groups[group_uuid]
            self._send(200, {
                "deleted": True,
                "cleared_watches": len(references["watches"]),
                "cleared_tags": len(references["tags"]),
                "cleared_global_events": references["global_events"],
            })
            return
        self._send(404, {"message": "No such API route"})

    def _watch_notification_routes(self, method: str, watch_uuid: str, body: dict[str, Any]) -> None:
        watch = self._state.watches.get(watch_uuid)
        if watch is None:
            self._send(404, {"message": f"No watch exists with the UUID of {watch_uuid}"})
            return
        if method == "GET":
            self._send(200, _routing().explain_watch(_application(self._state), watch, _watch_tags(self._state, watch)))
            return
        if method == "PUT":
            try:
                watch["notification_group_routes"] = _merge_routes(
                    watch.get("notification_group_routes") or {},
                    body.get("routes") or {},
                    self._state,
                )
            except ValueError as exc:
                self._send(400, {"message": str(exc)})
                return
            self._send(200, {"uuid": watch_uuid, "routes": watch["notification_group_routes"]})
            return
        self._send(404, {"message": "No such API route"})

    def _tag_routes(self, method: str, tag_uuid: str, body: dict[str, Any]) -> None:
        tag = self._state.tags.get(tag_uuid)
        if tag is None:
            self._send(404, {"message": f"No tag exists with the UUID of {tag_uuid}"})
            return
        events = ("content_changed", "filter_failure", "step_failure")
        if method == "GET":
            routes = tag.get("notification_group_routes") or {}
            self._send(200, {
                "uuid": tag_uuid,
                "events": _routing().event_catalog(),
                "inheritance": _routing().INHERITANCE_TEXT,
                "routes": {event: routes.get(event) for event in events},
            })
            return
        if method == "PUT":
            try:
                tag["notification_group_routes"] = _merge_routes(
                    tag.get("notification_group_routes") or {},
                    body.get("routes") or {},
                    self._state,
                )
            except ValueError as exc:
                self._send(400, {"message": str(exc)})
                return
            self._send(200, {"uuid": tag_uuid, "routes": tag["notification_group_routes"]})
            return
        self._send(404, {"message": "No such API route"})

    def _import(self, query: dict[str, list[str]], raw_text: str) -> None:
        lines = [line.strip() for line in raw_text.splitlines() if line.strip() and not line.strip().startswith("#")]
        created = []
        dedupe = _first(query, "dedupe").lower() not in {"false", "0", "no"}
        tag_name = _first(query, "tag").strip()
        for url in lines:
            if not (url.startswith("http://") or url.startswith("https://")):
                self._send(400, "Invalid or unsupported URL")
                return
            if dedupe and any(str(watch.get("url")) == url for watch in self._state.watches.values()):
                continue
            watch_uuid = str(uuid.uuid4())
            tags = [self._ensure_tag(tag_name)] if tag_name else []
            self._state.watches[watch_uuid] = _new_watch(watch_uuid, url, {}, tags)
            created.append(watch_uuid)
        self._send(200, created)

    def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
        if status == 204 or payload is None:
            body = b""
        elif isinstance(payload, str):
            body = payload.encode()
            if content_type == "application/json":
                content_type = "text/plain"
        else:
            body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


def _new_watch(watch_uuid: str, url: str, body: dict[str, Any], tag_uuids: list[str]) -> dict[str, Any]:
    return {
        "uuid": watch_uuid,
        "url": url,
        "title": body.get("title") or "",
        "tags": list(dict.fromkeys(tag_uuids)),
        "time_between_check": body.get("time_between_check") or {
            "weeks": None,
            "days": None,
            "hours": None,
            "minutes": None,
            "seconds": None,
        },
        "time_between_check_use_default": body.get("time_between_check_use_default", True),
        "include_filters": list(body.get("include_filters") or []),
        "subtractive_selectors": list(body.get("subtractive_selectors") or []),
        "ignore_text": list(body.get("ignore_text") or []),
        "trigger_text": list(body.get("trigger_text") or []),
        "notification_urls": list(body.get("notification_urls") or []),
        "paused": bool(body.get("paused") or False),
        "notification_muted": bool(body.get("notification_muted") or False),
        "notification_group_routes": dict(body.get("notification_group_routes") or {}),
        "last_checked": 0,
        "last_changed": 0,
        "last_error": False,
        "viewed": False,
    }


def _search_hit(watch: dict[str, Any], needle: str, partial: bool) -> bool:
    title = str(watch.get("title") or "")
    url = str(watch.get("url") or "")
    error = str(watch.get("last_error") or "")
    if partial:
        return needle in title.lower() or needle in url.lower() or needle in error.lower()
    return needle in {title.lower(), url.lower(), error.lower()}


def _split_names(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _summary(watch: dict[str, Any]) -> dict[str, Any]:
    return {
        "last_changed": watch.get("last_changed", 0),
        "last_checked": watch.get("last_checked", 0),
        "last_error": watch.get("last_error", False),
        "title": watch.get("title") or "",
        "url": watch.get("url"),
        "viewed": watch.get("viewed", False),
    }


def _first(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return values[0] if values else ""


_ROUTING = None


def _routing():
    global _ROUTING
    if _ROUTING is None:
        path = Path(__file__).resolve().parents[2] / "changedetectionio" / "notification_groups.py"
        spec = importlib.util.spec_from_file_location("cdio_notification_groups", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _ROUTING = module
    return _ROUTING


def _application(state: APIState) -> dict[str, Any]:
    return {
        "notification_groups": state.notification_groups,
        "notification_group_routes": state.notification_routes,
        "notification_urls": state.notification_urls,
    }


def _watch_tags(state: APIState, watch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        tag_uuid: state.tags[tag_uuid]
        for tag_uuid in watch.get("tags") or []
        if tag_uuid in state.tags
    }


def _route_document(routes: dict[str, str]) -> dict[str, Any]:
    routing = _routing()
    return {
        "events": routing.event_catalog(),
        "inheritance": routing.INHERITANCE_TEXT,
        "routes": {event: routes.get(event) for event in routing.NOTIFICATION_EVENTS},
    }


def _group_list(state: APIState) -> dict[str, Any]:
    routing = _routing()
    return {
        "events": routing.event_catalog(),
        "inheritance": routing.INHERITANCE_TEXT,
        "groups": {group_uuid: routing.public_group(group) for group_uuid, group in state.notification_groups.items()},
    }


def _merge_routes(current: dict[str, Any], patch: dict[str, Any], state: APIState) -> dict[str, str]:
    merged = {event: group_uuid for event, group_uuid in current.items() if group_uuid}
    for event, group_uuid in patch.items():
        if event not in {"content_changed", "filter_failure", "step_failure"}:
            raise ValueError(event)
        if group_uuid in (None, ""):
            merged.pop(event, None)
            continue
        if str(group_uuid) not in state.notification_groups:
            raise ValueError(group_uuid)
        merged[event] = str(group_uuid)
    return merged


def _group_references(state: APIState, group_uuid: str) -> dict[str, Any]:
    watches = [
        watch_uuid
        for watch_uuid, watch in state.watches.items()
        if group_uuid in (watch.get("notification_group_routes") or {}).values()
    ]
    tags = [
        tag_uuid
        for tag_uuid, tag in state.tags.items()
        if group_uuid in (tag.get("notification_group_routes") or {}).values()
    ]
    global_events = [event for event, assigned in state.notification_routes.items() if assigned == group_uuid]
    return {"watches": watches, "tags": tags, "global_events": global_events}


def _clear_group(state: APIState, group_uuid: str) -> dict[str, Any]:
    references = _group_references(state, group_uuid)
    for watch in state.watches.values():
        routes = watch.get("notification_group_routes") or {}
        watch["notification_group_routes"] = {event: assigned for event, assigned in routes.items() if assigned != group_uuid}
    for tag in state.tags.values():
        routes = tag.get("notification_group_routes") or {}
        tag["notification_group_routes"] = {event: assigned for event, assigned in routes.items() if assigned != group_uuid}
    state.notification_routes = {
        event: assigned for event, assigned in state.notification_routes.items() if assigned != group_uuid
    }
    return references
