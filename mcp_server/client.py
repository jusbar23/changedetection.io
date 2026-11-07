"""HTTP client for the changedetection.io ``/api/v1`` REST API."""

from __future__ import annotations

import threading
from typing import Any

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
        ok: tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        """Perform one API call and return parsed JSON, text, or ``None`` for an empty body."""
        url = f"{self.base_url}{path}"
        try:
            with self._lock:
                response = self._session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
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
        data = self.request("GET", f"/api/v1/watch/{uuid}")
        return _as_dict(data)

    def create_watch(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self.request("POST", "/api/v1/watch", json_body=payload, ok=(201,))
        return _as_dict(data)

    def update_watch(self, uuid: str, fields: dict[str, Any]) -> dict[str, Any]:
        if not fields:
            raise ChangeDetectionError("No fields to update.")
        data = self.request("PUT", f"/api/v1/watch/{uuid}", json_body=fields)
        return _as_dict(data) if isinstance(data, dict) else {"status": "OK"}

    def delete_watch(self, uuid: str) -> None:
        self.request("DELETE", f"/api/v1/watch/{uuid}", ok=(200, 204))

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
        data = self.request("GET", f"/api/v1/watch/{uuid}", params=params)
        return data if isinstance(data, dict) else {"status": "OK"}

    def get_history(self, uuid: str) -> dict[str, str]:
        data = self.request("GET", f"/api/v1/watch/{uuid}/history")
        if not isinstance(data, dict):
            raise ChangeDetectionError(f"Unexpected history payload for {uuid}")
        return {str(key): str(value) for key, value in data.items()}

    def get_snapshot(self, uuid: str, timestamp: str, *, html: bool = False) -> str:
        params = {"html": "1"} if html else None
        data = self.request("GET", f"/api/v1/watch/{uuid}/history/{timestamp}", params=params)
        if data is None:
            return ""
        if isinstance(data, str):
            return data
        raise ChangeDetectionError(f"Unexpected snapshot payload for {uuid} at {timestamp}")

    def list_tags(self) -> dict[str, Any]:
        data = self.request("GET", "/api/v1/tags")
        return _as_dict(data)

    def get_tag(self, uuid: str) -> dict[str, Any]:
        data = self.request("GET", f"/api/v1/tag/{uuid}")
        return _as_dict(data)

    def create_tag(self, title: str) -> dict[str, Any]:
        data = self.request("POST", "/api/v1/tag", json_body={"title": title}, ok=(201,))
        return _as_dict(data)

    def update_tag(self, uuid: str, fields: dict[str, Any]) -> dict[str, Any]:
        if not fields:
            raise ChangeDetectionError("No tag fields to update.")
        data = self.request("PUT", f"/api/v1/tag/{uuid}", json_body=fields)
        return data if isinstance(data, dict) else {"status": "OK"}

    def delete_tag(self, uuid: str) -> None:
        self.request("DELETE", f"/api/v1/tag/{uuid}", ok=(200, 204))

    def set_tag_muted(self, uuid: str, muted: bool) -> dict[str, Any]:
        data = self.request(
            "GET",
            f"/api/v1/tag/{uuid}",
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
