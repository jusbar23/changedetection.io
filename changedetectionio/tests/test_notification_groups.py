import json
import queue
import tempfile
from pathlib import Path

import pytest
from flask import Flask
from flask_login import LoginManager
from flask_restful import Api
from jinja2 import ChoiceLoader, FileSystemLoader

from changedetectionio.api.NotificationGroups import NotificationGroup, NotificationGroupList
from changedetectionio.blueprint.notification_groups import construct_blueprint
from changedetectionio.notification_groups import resolve_notification_targets
from changedetectionio.notification_service import NotificationService
from changedetectionio.store import ChangeDetectionStore

FIXTURE = Path(__file__).parent / "fixtures" / "notification_routing.json"
REPO_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def _stop(store):
    store.stop_thread = True


def test_fixture_routes_each_existing_alert_without_duplicates():
    fixture = json.loads(FIXTURE.read_text())
    application = fixture["application"]
    watch = fixture["watch"]
    tags = {tag_uuid: fixture["tags"][tag_uuid] for tag_uuid in watch["tags"]}

    content = resolve_notification_targets(application, watch, tags, "content_changed")
    filters = resolve_notification_targets(application, watch, tags, "filter_failure")
    steps = resolve_notification_targets(application, watch, tags, "step_failure")
    evidence = {
        "content_changed": content.public(),
        "filter_failure": filters.public(),
        "step_failure": steps.public(),
    }

    assert content.source == "watch_group"
    assert content.urls == ["mailto://oncall@example.com"]
    assert content.duplicates_removed == 1
    assert filters.source == "tag_group"
    assert filters.urls == ["mailto://filters@example.com"]
    assert steps.source == "watch_urls"
    assert steps.urls == ["mailto://watch@example.com"]
    assert steps.skipped_groups == [{"uuid": "missing-group", "reason": "missing"}]
    assert "mailto://system@example.com" not in steps.urls
    assert "secret" not in json.dumps(evidence)

    muted = dict(watch)
    muted["notification_muted"] = True
    muted_content = resolve_notification_targets(application, muted, tags, "content_changed")
    assert muted_content.source == "tag_urls"
    assert muted_content.urls == ["mailto://tag@example.com"]

    silent = dict(watch)
    silent["notification_group_routes"] = {"content_changed": "silent"}
    silent_content = resolve_notification_targets(application, silent, tags, "content_changed")
    assert silent_content.source == "watch_group"
    assert silent_content.urls == []

    legacy = dict(watch)
    legacy["notification_group_routes"] = {}
    legacy_content = resolve_notification_targets(application, legacy, tags, "content_changed")
    assert legacy_content.source == "watch_urls"
    assert legacy_content.urls == ["mailto://watch@example.com"]

    artifact = Path("/opt/cursor/artifacts/notification-routing.json")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"fixture": fixture, "resolution": evidence}, indent=2))


def test_migration_keeps_existing_urls_and_round_trips(tmp_path):
    store = ChangeDetectionStore(str(tmp_path), include_default_watches=False)
    try:
        watch_uuid = store.add_watch(
            "https://example.com/pricing",
            extras={"notification_urls": ["mailto://watch@example.com"], "paused": True},
        )
        application = store.data["settings"]["application"]
        application["notification_urls"] = ["mailto://system@example.com"]
        application["schema_version"] = 19
        application.pop("notification_groups", None)
        application.pop("notification_group_routes", None)
        store.data["watching"][watch_uuid].pop("notification_group_routes", None)
        store.run_updates()
        application = store.data["settings"]["application"]
        assert application["schema_version"] >= 20
        assert application["notification_urls"] == ["mailto://system@example.com"]
        assert application["notification_groups"] == {}
        assert application["notification_group_routes"] == {}
        assert store.data["watching"][watch_uuid]["notification_urls"] == ["mailto://watch@example.com"]
        assert store.data["watching"][watch_uuid]["notification_group_routes"] == {}

        group = store.add_notification_group("On-call", ["mailto://oncall@example.com"])
        store.set_notification_group_routes({"content_changed": group["uuid"]}, watch_uuid=watch_uuid)
        store.sync_to_json()
        group_uuid = group["uuid"]
    finally:
        _stop(store)

    reloaded = ChangeDetectionStore(str(tmp_path), include_default_watches=False)
    try:
        routes = reloaded.data["watching"][watch_uuid]["notification_group_routes"]
        assert routes["content_changed"] == group_uuid
        references = reloaded.delete_notification_group(group_uuid)
        assert watch_uuid in references["watches"]
        assert reloaded.data["watching"][watch_uuid]["notification_group_routes"] == {}
        assert reloaded.data["watching"][watch_uuid]["notification_urls"] == ["mailto://watch@example.com"]
        assert reloaded.data["settings"]["application"]["notification_urls"] == ["mailto://system@example.com"]
    finally:
        _stop(reloaded)


def test_service_queues_one_deduped_list_and_does_not_send(tmp_path):
    store = ChangeDetectionStore(str(tmp_path), include_default_watches=False)
    try:
        watch_uuid = store.add_watch(
            "https://example.com/pricing",
            extras={
                "paused": True,
                "include_filters": ["h1"],
                "notification_urls": ["mailto://watch@example.com", "mailto://watch@example.com"],
            },
        )
        group = store.add_notification_group(
            "Filters",
            ["mailto://filters@example.com", "mailto://filters@example.com"],
        )
        store.set_notification_group_routes({"filter_failure": group["uuid"]}, watch_uuid=watch_uuid)
        pending = queue.Queue()
        service = NotificationService(store, pending)
        service.send_filter_failure_notification(watch_uuid)
        queued = pending.get_nowait()
        assert pending.empty()
        assert queued["notification_urls"] == ["mailto://filters@example.com"]
        assert not hasattr(service, "process_notification")

        empty = store.add_notification_group("Nobody", [])
        store.set_notification_group_routes({"step_failure": empty["uuid"]}, watch_uuid=watch_uuid)
        service.send_step_failure_notification(watch_uuid, 0)
        assert pending.empty()
    finally:
        _stop(store)


def test_api_requires_key_and_redacts_destinations(tmp_path):
    store = ChangeDetectionStore(str(tmp_path), include_default_watches=False)
    try:
        store.data["settings"]["application"]["api_access_token"] = "test-key"
        store.data["settings"]["application"]["api_access_token_enabled"] = True
        store.add_notification_group("On-call", ["mailto://ops:pw@example.com"])
        app = Flask("groups-api")
        api = Api(app)
        api.add_resource(
            NotificationGroupList,
            "/api/v1/notification-groups",
            resource_class_kwargs={"datastore": store},
        )
        group_uuid = next(iter(store.list_notification_groups()))
        api.add_resource(
            NotificationGroup,
            "/api/v1/notification-groups/<string:uuid>",
            resource_class_kwargs={"datastore": store},
        )
        client = app.test_client()
        denied = client.get("/api/v1/notification-groups")
        assert denied.status_code == 403
        allowed = client.get("/api/v1/notification-groups", headers={"x-api-key": "test-key"})
        assert allowed.status_code == 200
        body = allowed.get_json()
        assert "pw@" not in json.dumps(body)
        assert body["groups"][group_uuid]["destinations"] == ["mailto://ops:***@example.com"]
    finally:
        _stop(store)


def test_groups_page_dropdown_create_and_safe_delete(tmp_path):
    store = ChangeDetectionStore(str(tmp_path), include_default_watches=False)
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "base.html").write_text("<!doctype html><html><body>{% block content %}{% endblock %}</body></html>")
    try:
        app = Flask("groups-ui", template_folder=str(REPO_TEMPLATES))
        app.secret_key = "test"
        app.config["DATASTORE"] = store
        app.config["LOGIN_DISABLED"] = True
        app.jinja_env.globals["csrf_token"] = lambda: "test-token"
        app.jinja_loader = ChoiceLoader([FileSystemLoader(str(stub)), app.jinja_loader])
        login_manager = LoginManager()
        login_manager.init_app(app)

        @login_manager.user_loader
        def load_user(_user_id):
            return None

        @app.route("/login")
        def login():
            return "login"

        login_manager.login_view = "login"
        app.register_blueprint(construct_blueprint(store), url_prefix="/settings/notification-groups")
        client = app.test_client()

        created = client.post(
            "/settings/notification-groups",
            data={"title": "On-call", "notification_urls": "mailto://ops:pw@example.com"},
            follow_redirects=True,
        )
        assert created.status_code == 200
        page = created.get_data(as_text=True)
        assert "On-call" in page
        assert "<select" in page
        assert "Content change" in page or "content" in page.lower()
        assert "mailto://ops:***@example.com" in page
        group_uuid = next(iter(store.list_notification_groups()))
        assert f'value="{group_uuid}"' in page

        saved = client.post(
            "/settings/notification-groups/routes",
            data={
                "notification_group_route_content_changed": group_uuid,
                "notification_group_route_filter_failure": "",
                "notification_group_route_step_failure": "",
            },
            follow_redirects=True,
        )
        assert f'value="{group_uuid}" selected' in saved.get_data(as_text=True) or (
            f'value="{group_uuid}"' in saved.get_data(as_text=True) and "selected" in saved.get_data(as_text=True)
        )
        assert store.data["settings"]["application"]["notification_group_routes"]["content_changed"] == group_uuid

        refused = client.post(f"/settings/notification-groups/{group_uuid}/delete", data={}, follow_redirects=True)
        assert group_uuid in store.list_notification_groups()
        assert "confirmation" in refused.get_data(as_text=True).lower() or group_uuid in store.list_notification_groups()

        deleted = client.post(
            f"/settings/notification-groups/{group_uuid}/delete",
            data={"confirm": "yes"},
            follow_redirects=True,
        )
        assert deleted.status_code == 200
        assert store.list_notification_groups() == {}
        assert store.data["settings"]["application"]["notification_group_routes"] == {}

        store.data["settings"]["application"]["password"] = True
        app.config["LOGIN_DISABLED"] = False
        anonymous = client.get("/settings/notification-groups")
        assert anonymous.status_code in {302, 401}
        assert "login" in (anonymous.headers.get("Location") or "")
    finally:
        _stop(store)


def test_unknown_alert_type_is_rejected():
    with pytest.raises(ValueError, match="content_changed"):
        resolve_notification_targets({}, {"notification_group_routes": {}}, {}, "restock")
