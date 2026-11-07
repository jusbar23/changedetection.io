"""REST API for notification groups and per-alert assignment.

Responses list destinations with secrets removed. Callers that add or replace
destinations send the original URLs; those URLs are not echoed back.
"""

from flask import request
from flask_restful import Resource, abort

from changedetectionio.notification_groups import (
    INHERITANCE_TEXT,
    event_catalog,
    explain_watch,
    public_group,
)
from . import auth


def _json_body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        abort(400, message="Send a JSON object.")
    return body


def _urls(body):
    urls = body.get("notification_urls")
    if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
        abort(400, message="notification_urls must be a list of strings.")
    return urls


def _require_confirm(body):
    if body.get("confirm") is not True:
        abort(400, message="Pass confirm=true. This changes who receives alerts.")


def _value_error(exc: ValueError):
    abort(400, message=str(exc))


class NotificationGroupList(Resource):
    def __init__(self, **kwargs):
        self.datastore = kwargs["datastore"]

    @auth.check_token
    def get(self):
        groups = self.datastore.list_notification_groups()
        return {
            "events": event_catalog(),
            "inheritance": INHERITANCE_TEXT,
            "groups": {group_uuid: public_group(group) for group_uuid, group in groups.items()},
        }, 200

    @auth.check_token
    def post(self):
        body = _json_body()
        title = body.get("title")
        if not isinstance(title, str):
            abort(400, message="title must be a string.")
        try:
            group = self.datastore.add_notification_group(title, _urls(body))
        except ValueError as exc:
            _value_error(exc)
        return {"uuid": group["uuid"], "group": public_group(group)}, 201


class NotificationGroup(Resource):
    def __init__(self, **kwargs):
        self.datastore = kwargs["datastore"]

    @auth.check_token
    def get(self, uuid):
        group = self.datastore.get_notification_group(uuid)
        if not group:
            abort(404, message=f"No notification group exists with the UUID of {uuid}")
        references = self.datastore.notification_group_references(uuid)
        return {
            "group": public_group(group),
            "references": {
                "watches": len(references["watches"]),
                "tags": len(references["tags"]),
                "global_events": references["global_events"],
            },
        }, 200

    @auth.check_token
    def put(self, uuid):
        body = _json_body()
        title = body.get("title") if "title" in body else None
        urls = None
        if "notification_urls" in body:
            _require_confirm(body)
            urls = _urls(body)
        if title is None and urls is None:
            abort(400, message="Pass title and/or notification_urls.")
        if title is not None and not isinstance(title, str):
            abort(400, message="title must be a string.")
        try:
            group = self.datastore.update_notification_group(uuid, title=title, notification_urls=urls)
        except KeyError:
            abort(404, message=f"No notification group exists with the UUID of {uuid}")
        except ValueError as exc:
            _value_error(exc)
        return {"group": public_group(group)}, 200

    @auth.check_token
    def delete(self, uuid):
        if request.args.get("confirm") != "true":
            abort(400, message="Pass confirm=true. This deletes the group and clears it from watches and tags.")
        try:
            references = self.datastore.delete_notification_group(uuid)
        except KeyError:
            abort(404, message=f"No notification group exists with the UUID of {uuid}")
        return {
            "deleted": True,
            "cleared_watches": len(references["watches"]),
            "cleared_tags": len(references["tags"]),
            "cleared_global_events": references["global_events"],
        }, 200


class NotificationGroupDestinations(Resource):
    def __init__(self, **kwargs):
        self.datastore = kwargs["datastore"]

    @auth.check_token
    def post(self, uuid):
        body = _json_body()
        try:
            added = self.datastore.add_notification_group_destinations(uuid, _urls(body))
        except KeyError:
            abort(404, message=f"No notification group exists with the UUID of {uuid}")
        except ValueError as exc:
            _value_error(exc)
        group = self.datastore.get_notification_group(uuid)
        return {"added": len(added), "group": public_group(group)}, 200

    @auth.check_token
    def delete(self, uuid):
        body = _json_body()
        _require_confirm(body)
        try:
            removed = self.datastore.remove_notification_group_destinations(uuid, _urls(body))
        except KeyError:
            abort(404, message=f"No notification group exists with the UUID of {uuid}")
        except ValueError as exc:
            _value_error(exc)
        group = self.datastore.get_notification_group(uuid)
        return {"removed": len(removed), "group": public_group(group)}, 200


class GlobalNotificationRoutes(Resource):
    def __init__(self, **kwargs):
        self.datastore = kwargs["datastore"]

    @auth.check_token
    def get(self):
        application = self.datastore.data["settings"]["application"]
        return {
            "events": event_catalog(),
            "inheritance": INHERITANCE_TEXT,
            "routes": application.get("notification_group_routes") or {},
        }, 200

    @auth.check_token
    def put(self):
        body = _json_body()
        routes = body.get("routes", body)
        if not isinstance(routes, dict):
            abort(400, message="routes must be an object of alert type to group id.")
        try:
            saved = self.datastore.set_notification_group_routes(routes)
        except ValueError as exc:
            _value_error(exc)
        return {"routes": saved}, 200


class WatchNotificationRoutes(Resource):
    def __init__(self, **kwargs):
        self.datastore = kwargs["datastore"]

    def _watch(self, uuid):
        watch = self.datastore.data["watching"].get(uuid)
        if not watch:
            abort(404, message=f"No watch exists with the UUID of {uuid}")
        return watch

    @auth.check_token
    def get(self, uuid):
        watch = self._watch(uuid)
        tags = self.datastore.get_all_tags_for_watch(uuid=uuid) or {}
        return explain_watch(self.datastore.data["settings"]["application"], watch, tags), 200

    @auth.check_token
    def put(self, uuid):
        self._watch(uuid)
        body = _json_body()
        routes = body.get("routes", body)
        if not isinstance(routes, dict):
            abort(400, message="routes must be an object of alert type to group id.")
        try:
            saved = self.datastore.set_notification_group_routes(routes, watch_uuid=uuid)
        except KeyError:
            abort(404, message=f"No watch exists with the UUID of {uuid}")
        except ValueError as exc:
            _value_error(exc)
        return {"uuid": uuid, "routes": saved}, 200


class TagNotificationRoutes(Resource):
    def __init__(self, **kwargs):
        self.datastore = kwargs["datastore"]

    @auth.check_token
    def get(self, uuid):
        tag = self.datastore.data["settings"]["application"].get("tags", {}).get(uuid)
        if not tag:
            abort(404, message=f"No tag exists with the UUID of {uuid}")
        routes = tag.get("notification_group_routes") or {}
        return {
            "uuid": uuid,
            "events": event_catalog(),
            "inheritance": INHERITANCE_TEXT,
            "routes": {event: routes.get(event) for event in ("content_changed", "filter_failure", "step_failure")},
        }, 200

    @auth.check_token
    def put(self, uuid):
        body = _json_body()
        routes = body.get("routes", body)
        if not isinstance(routes, dict):
            abort(400, message="routes must be an object of alert type to group id.")
        try:
            saved = self.datastore.set_notification_group_routes(routes, tag_uuid=uuid)
        except KeyError:
            abort(404, message=f"No tag exists with the UUID of {uuid}")
        except ValueError as exc:
            _value_error(exc)
        return {"uuid": uuid, "routes": saved}, 200
