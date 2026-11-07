"""HTTP client for the changedetection.io ``/api/v1`` REST API."""

from __future__ import annotations

import threading
from typing import Any
from urllib.parse import quote

import requests

from mcp_server import __version__


class ChangeDetectionError(Exception):
    """The changedetection.io API returned an error or could not be reached."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ChangeDetectionClient:
    """Small wrapper around ``/api/v1``.

    Every MCP tool goes through this client. The web application itself is not imported.
    """

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["Accept"] = "application/json, text/plain"
        self._session.headers["User-Agent"] = f"changedetection-mcp/{__version__}"
        if api_key:
            self._session.headers["x-api-key"] = api_key
        self._lock = threading.Lock()

    def close(self) -> None:
        self._session.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        raw_body: str | None = None,
        content_type: str | None = None,
        ok: tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        """Perform one API call and return parsed JSON, text, or ``None`` for an empty body."""
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": content_type} if content_type else None
        try:
            with self._lock:
                response = self._session.request(
                    method,
                    url,
                    params=params,
                    json=None if raw_body is not None else json_body,
                    data=raw_body.encode() if isinstance(raw_body, str) else raw_body,
                    headers=headers,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
        except requests.RequestException as exc:
            raise ChangeDetectionError(
                f"Could not reach changedetection.io at {self.base_url}: {exc}"
            ) from exc

        parsed = _parse_body(response)
        if response.status_code not in ok:
            raise ChangeDetectionError(
                _error_message(response.status_code, parsed, response.text),
                status_code=response.status_code,
            )
        if isinstance(parsed, str) and parsed.strip() == "OK":
            return {"status": "OK"}
        return parsed

    def list_watches(self, tag: str | None = None) -> dict[str, Any]:
        params = {"tag": tag} if tag else None
        data = self.request("GET", "/api/v1/watch", params=params)
        return _as_dict(data)

    def search_watches(self, query: str, tag: str | None = None, partial: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {"q": query, "partial": "true" if partial else "false"}
        if tag:
            params["tag"] = tag
        data = self.request("GET", "/api/v1/search", params=params)
        return _as_dict(data)

    def get_watch(self, uuid: str) -> dict[str, Any]:
        data = self.request("GET", f"/api/v1/watch/{_segment(uuid)}")
        return _as_dict(data)

    def create_watch(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self.request("POST", "/api/v1/watch", json_body=payload, ok=(201,))
        return _as_dict(data)

    def update_watch(self, uuid: str, fields: dict[str, Any]) -> dict[str, Any]:
        if not fields:
            raise ChangeDetectionError("No fields to update.")
        data = self.request("PUT", f"/api/v1/watch/{_segment(uuid)}", json_body=fields)
        return _as_dict(data) if isinstance(data, dict) else {"status": "OK"}

    def delete_watch(self, uuid: str) -> None:
        self.request("DELETE", f"/api/v1/watch/{_segment(uuid)}", ok=(200, 204))

    def set_watch_state(self, uuid: str, *, paused: str | None = None, muted: str | None = None, recheck: bool = False) -> dict[str, Any]:
        params: dict[str, str] = {}
        if recheck:
            params["recheck"] = "1"
        elif paused:
            params["paused"] = paused
        elif muted:
            params["muted"] = muted
        else:
            raise ChangeDetectionError("No watch state change was requested.")
        data = self.request("GET", f"/api/v1/watch/{_segment(uuid)}", params=params)
        return data if isinstance(data, dict) else {"status": "OK"}

    def get_history(self, uuid: str) -> dict[str, str]:
        data = self.request("GET", f"/api/v1/watch/{_segment(uuid)}/history")
        if not isinstance(data, dict):
            raise ChangeDetectionError(f"Unexpected history payload for {uuid}")
        return {str(key): str(value) for key, value in data.items()}

    def get_snapshot(self, uuid: str, timestamp: str, *, html: bool = False) -> str:
        params = {"html": "1"} if html else None
        data = self.request("GET", f"/api/v1/watch/{_segment(uuid)}/history/{_segment(timestamp)}", params=params)
        if data is None:
            return ""
        if isinstance(data, str):
            return data
        raise ChangeDetectionError(f"Unexpected snapshot payload for {uuid} at {timestamp}")

    def list_tags(self) -> dict[str, Any]:
        data = self.request("GET", "/api/v1/tags")
        return _as_dict(data)

    def get_tag(self, uuid: str) -> dict[str, Any]:
        data = self.request("GET", f"/api/v1/tag/{_segment(uuid)}")
        return _as_dict(data)

    def create_tag(self, title: str) -> dict[str, Any]:
        data = self.request("POST", "/api/v1/tag", json_body={"title": title}, ok=(201,))
        return _as_dict(data)

    def update_tag(self, uuid: str, fields: dict[str, Any]) -> dict[str, Any]:
        if not fields:
            raise ChangeDetectionError("No tag fields to update.")
        data = self.request("PUT", f"/api/v1/tag/{_segment(uuid)}", json_body=fields)
        return data if isinstance(data, dict) else {"status": "OK"}

    def delete_tag(self, uuid: str) -> None:
        self.request("DELETE", f"/api/v1/tag/{_segment(uuid)}", ok=(200, 204))

    def set_tag_muted(self, uuid: str, muted: bool) -> dict[str, Any]:
        data = self.request(
            "GET",
            f"/api/v1/tag/{_segment(uuid)}",
            params={"muted": "muted" if muted else "unmuted"},
        )
        return data if isinstance(data, dict) else {"status": "OK"}

    def ensure_tag_uuids(self, names: list[str]) -> list[str]:
        """Return tag UUIDs for ``names``, creating any tag that does not exist yet."""
        existing = self.list_tags()
        by_title = {
            str(tag.get("title", "")).strip().lower(): str(uuid)
            for uuid, tag in existing.items()
            if isinstance(tag, dict)
        }
        uuids: list[str] = []
        for name in names:
            key = name.strip().lower()
            if not key:
                continue
            found = by_title.get(key)
            if found is None:
                created = self.create_tag(name.strip())
                found = str(created["uuid"])
                by_title[key] = found
            uuids.append(found)
        return uuids

    def get_system_info(self) -> dict[str, Any]:
        data = self.request("GET", "/api/v1/systeminfo")
        return _as_dict(data)

    def recheck_all_watches(self) -> dict[str, Any]:
        data = self.request("GET", "/api/v1/watch", params={"recheck_all": "1"})
        return _as_dict(data) if isinstance(data, dict) else {"status": "OK"}

    def import_watches(
        self,
        urls: list[str],
        *,
        tag: str | None = None,
        tag_uuids: list[str] | None = None,
        dedupe: bool = True,
    ) -> list[str]:
        params: dict[str, Any] = {"dedupe": "true" if dedupe else "false"}
        if tag:
            params["tag"] = tag
        if tag_uuids:
            params["tag_uuids"] = ",".join(tag_uuids)
        data = self.request(
            "POST",
            "/api/v1/import",
            params=params,
            raw_body="\n".join(urls),
            content_type="text/plain; charset=utf-8",
        )
        if isinstance(data, list):
            return [str(item) for item in data]
        if isinstance(data, dict) and isinstance(data.get("uuids"), list):
            return [str(item) for item in data["uuids"]]
        raise ChangeDetectionError("Import did not return a list of watch ids.")

    def list_notification_urls(self) -> list[str]:
        data = self.request("GET", "/api/v1/notifications")
        urls = _as_dict(data).get("notification_urls") or []
        if not isinstance(urls, list):
            raise ChangeDetectionError("Notification URL list was not a list.")
        return [str(url) for url in urls]

    def add_notification_urls(self, urls: list[str]) -> list[str]:
        data = self.request(
            "POST",
            "/api/v1/notifications",
            json_body={"notification_urls": urls},
            ok=(200, 201),
        )
        return [str(url) for url in _as_dict(data).get("notification_urls") or []]

    def replace_notification_urls(self, urls: list[str]) -> list[str]:
        data = self.request("PUT", "/api/v1/notifications", json_body={"notification_urls": urls})
        return [str(url) for url in _as_dict(data).get("notification_urls") or []]

    def remove_notification_urls(self, urls: list[str]) -> None:
        self.request(
            "DELETE",
            "/api/v1/notifications",
            json_body={"notification_urls": urls},
            ok=(200, 204),
        )

    def list_notification_groups(self) -> dict[str, Any]:
        return _as_dict(self.request("GET", "/api/v1/notification-groups"))

    def get_notification_group(self, uuid: str) -> dict[str, Any]:
        return _as_dict(self.request("GET", f"/api/v1/notification-groups/{_segment(uuid)}"))

    def create_notification_group(self, title: str, urls: list[str]) -> dict[str, Any]:
        return _as_dict(
            self.request(
                "POST",
                "/api/v1/notification-groups",
                json_body={"title": title, "notification_urls": urls},
                ok=(201,),
            )
        )

    def update_notification_group(self, uuid: str, fields: dict[str, Any]) -> dict[str, Any]:
        return _as_dict(self.request("PUT", f"/api/v1/notification-groups/{_segment(uuid)}", json_body=fields))

    def delete_notification_group(self, uuid: str) -> dict[str, Any]:
        data = self.request(
            "DELETE",
            f"/api/v1/notification-groups/{_segment(uuid)}",
            params={"confirm": "true"},
        )
        return _as_dict(data) if isinstance(data, dict) else {"deleted": True}

    def add_notification_group_destinations(self, uuid: str, urls: list[str]) -> dict[str, Any]:
        return _as_dict(
            self.request(
                "POST",
                f"/api/v1/notification-groups/{_segment(uuid)}/destinations",
                json_body={"notification_urls": urls},
            )
        )

    def remove_notification_group_destinations(self, uuid: str, urls: list[str]) -> dict[str, Any]:
        return _as_dict(
            self.request(
                "DELETE",
                f"/api/v1/notification-groups/{_segment(uuid)}/destinations",
                json_body={"notification_urls": urls, "confirm": True},
            )
        )

    def get_global_notification_routes(self) -> dict[str, Any]:
        return _as_dict(self.request("GET", "/api/v1/notification-routes"))

    def set_global_notification_routes(self, routes: dict[str, Any]) -> dict[str, Any]:
        return _as_dict(self.request("PUT", "/api/v1/notification-routes", json_body={"routes": routes}))

    def get_watch_notification_routes(self, uuid: str) -> dict[str, Any]:
        return _as_dict(self.request("GET", f"/api/v1/watch/{_segment(uuid)}/notification-routes"))

    def set_watch_notification_routes(self, uuid: str, routes: dict[str, Any]) -> dict[str, Any]:
        return _as_dict(
            self.request("PUT", f"/api/v1/watch/{_segment(uuid)}/notification-routes", json_body={"routes": routes})
        )

    def get_tag_notification_routes(self, uuid: str) -> dict[str, Any]:
        return _as_dict(self.request("GET", f"/api/v1/tag/{_segment(uuid)}/notification-routes"))

    def set_tag_notification_routes(self, uuid: str, routes: dict[str, Any]) -> dict[str, Any]:
        return _as_dict(
            self.request("PUT", f"/api/v1/tag/{_segment(uuid)}/notification-routes", json_body={"routes": routes})
        )


def _segment(value: str) -> str:
    text = str(value).strip()
    if not text or any(character in text for character in "/?#\\"):
        raise ChangeDetectionError("Refusing an id that contains a path character.")
    return quote(text, safe="")


def _parse_body(response: requests.Response) -> Any:
    if response.status_code == 204 or not response.content:
        return None
    content_type = response.headers.get("Content-Type", "")
    if "json" in content_type:
        try:
            return response.json()
        except ValueError:
            return response.text
    text = response.text
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return response.json()
        except ValueError:
            return text
    return text


def _error_message(status_code: int, parsed: Any, raw: str) -> str:
    detail: str
    if isinstance(parsed, dict) and parsed.get("message"):
        detail = str(parsed["message"])
    elif isinstance(parsed, str) and parsed.strip():
        detail = parsed.strip().strip('"')
    elif raw.strip():
        detail = raw.strip()
    else:
        detail = "request failed"
    return f"changedetection.io API returned HTTP {status_code}: {detail}"


def _as_dict(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    raise ChangeDetectionError(f"Expected a JSON object from the API, got {type(data).__name__}")
