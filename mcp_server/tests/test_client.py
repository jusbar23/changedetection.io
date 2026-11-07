from mcp_server.client import ChangeDetectionClient, ChangeDetectionError


def test_api_key_is_sent_and_rejected(api):
    anonymous = ChangeDetectionClient(api.base_url, api_key="nope", timeout=5)
    try:
        try:
            anonymous.get_system_info()
        except ChangeDetectionError as exc:
            assert exc.status_code == 403
            assert "API key" in str(exc)
        else:
            raise AssertionError("expected an API key error")
    finally:
        anonymous.close()


def test_create_list_update_delete_roundtrip(cd_client):
    created = cd_client.create_watch({
        "url": "https://example.com/pricing",
        "title": "Pricing",
        "tag": "Shop, Alerts",
        "include_filters": ["#price"],
        "subtractive_selectors": ["footer"],
        "notification_urls": ["json://example.com/hook"],
        "time_between_check": {"weeks": None, "days": None, "hours": 2, "minutes": None, "seconds": None},
        "time_between_check_use_default": False,
    })
    watch_uuid = created["uuid"]

    listed = cd_client.list_watches(tag="shop")
    assert watch_uuid in listed
    assert listed[watch_uuid]["title"] == "Pricing"

    found = cd_client.search_watches("https://example.com/pricing")
    assert watch_uuid in found
    assert cd_client.search_watches("example.com", partial=True)

    watch = cd_client.get_watch(watch_uuid)
    assert watch["include_filters"] == ["#price"]
    assert watch["time_between_check"]["hours"] == 2
    assert watch["time_between_check_use_default"] is False
    assert len(watch["tags"]) == 2

    cd_client.update_watch(watch_uuid, {"title": "Pricing page", "notification_urls": ["mailto://alerts@example.com"]})
    assert cd_client.get_watch(watch_uuid)["title"] == "Pricing page"

    cd_client.set_watch_state(watch_uuid, paused="paused")
    assert cd_client.get_watch(watch_uuid)["paused"] is True
    cd_client.set_watch_state(watch_uuid, paused="unpaused")
    cd_client.set_watch_state(watch_uuid, muted="muted")
    assert cd_client.get_watch(watch_uuid)["notification_muted"] is True
    cd_client.set_watch_state(watch_uuid, recheck=True)
    assert cd_client.get_watch(watch_uuid)["last_checked"] == 1

    info = cd_client.get_system_info()
    assert info["version"] == "test"
    assert info["watch_count"] == 1

    cd_client.delete_watch(watch_uuid)
    assert watch_uuid not in cd_client.list_watches()


def test_missing_watch_is_an_error(cd_client):
    try:
        cd_client.get_watch("missing")
    except ChangeDetectionError as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("expected 404")


def test_tags_and_snapshots(api, cd_client):
    created = cd_client.create_tag("News")
    tag_uuid = created["uuid"]
    again = cd_client.create_tag("news")
    assert again["uuid"] == tag_uuid

    cd_client.update_tag(tag_uuid, {"title": "Headlines"})
    assert cd_client.get_tag(tag_uuid)["title"] == "Headlines"
    cd_client.set_tag_muted(tag_uuid, True)
    assert cd_client.get_tag(tag_uuid)["notification_muted"] is True

    watch_uuid = cd_client.create_watch({"url": "https://example.com/news", "tags": [tag_uuid]})["uuid"]
    api.add_snapshot(watch_uuid, "100", "alpha\n")
    api.add_snapshot(watch_uuid, "200", "beta\n")
    history = cd_client.get_history(watch_uuid)
    assert set(history) == {"100", "200"}
    assert cd_client.get_snapshot(watch_uuid, "latest") == "beta\n"
    assert cd_client.get_snapshot(watch_uuid, "100") == "alpha\n"

    cd_client.delete_tag(tag_uuid)
    assert tag_uuid not in cd_client.list_tags()
    assert tag_uuid not in cd_client.get_watch(watch_uuid)["tags"]
