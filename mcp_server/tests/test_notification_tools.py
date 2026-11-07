import anyio
from mcp import Client

from mcp_server.tests.helpers import call


def test_notification_groups_route_and_redact(api, mcp_app):
    async def scenario():
        async with Client(mcp_app) as client:
            created = await call(client, "create_watch", {
                "url": "https://example.com/pricing",
                "tag": "Billing",
                "notification_urls": ["mailto://legacy@example.com"],
            })
            watch_uuid = created["uuid"]
            legacy = await call(client, "explain_watch_notifications", {"uuid": watch_uuid})
            assert legacy["resolution"]["content_changed"]["source"] == "watch_urls"

            group = await call(client, "create_notification_group", {
                "title": "On-call",
                "notification_urls": ["discord://hook/secret-token", "mailto://ops:pw@example.com", "discord://hook/secret-token"],
            })
            group_uuid = group["uuid"]
            encoded = str(group)
            assert "secret-token" not in encoded
            assert "mailto://ops:pw@" not in encoded
            assert group["group"]["destinations"] == ["discord://***/***", "mailto://ops:***@example.com", "discord://***/***"]

            duplicate = await client.call_tool("create_notification_group", {
                "title": "on-call",
                "notification_urls": [],
            })
            assert duplicate.is_error

            empty = await call(client, "create_notification_group", {"title": "Silence", "notification_urls": []})
            await call(client, "set_watch_notification_routes", {
                "uuid": watch_uuid,
                "routes": {"step_failure": empty["uuid"]},
            })
            silent = await call(client, "explain_watch_notifications", {"uuid": watch_uuid})
            assert silent["resolution"]["step_failure"]["source"] == "watch_group"
            assert silent["resolution"]["step_failure"]["destination_count"] == 0

            await call(client, "set_watch_notification_routes", {
                "uuid": watch_uuid,
                "routes": {"content_changed": group_uuid},
            })
            explained = await call(client, "explain_watch_notifications", {"uuid": watch_uuid})
            content = explained["resolution"]["content_changed"]
            assert content["source"] == "watch_group"
            assert content["duplicates_removed"] == 1
            assert content["destination_count"] == 2
            assert "secret-token" not in str(explained)

            tags = await call(client, "list_tags")
            tag_uuid = next(iter(tags["tags"]))
            await call(client, "set_tag_notification_routes", {
                "uuid": tag_uuid,
                "routes": {"filter_failure": group_uuid},
            })
            tag_routes = await call(client, "get_tag_notification_routes", {"uuid": tag_uuid})
            assert tag_routes["routes"]["filter_failure"] == group_uuid
            routed = await call(client, "explain_watch_notifications", {"uuid": watch_uuid})
            assert routed["resolution"]["filter_failure"]["source"] == "tag_group"

            await call(client, "mute_watch", {"uuid": watch_uuid})
            muted = await call(client, "explain_watch_notifications", {"uuid": watch_uuid})
            assert muted["resolution"]["content_changed"]["source"] != "watch_group"
            assert "watch" in muted["resolution"]["content_changed"]["muted_levels"]
            await call(client, "unmute_watch", {"uuid": watch_uuid})

            catalog = await call(client, "list_notification_groups")
            assert [event["id"] for event in catalog["events"]] == ["content_changed", "filter_failure", "step_failure"]
            assert "restock" not in str(catalog["events"])
            assert "watch group" in catalog["inheritance"].lower()

            refused = await client.call_tool("delete_notification_group", {"uuid": group_uuid, "confirm": False})
            assert refused.is_error
            still_there = await call(client, "get_notification_group", {"uuid": group_uuid})
            assert still_there["references"]["watches"] == 1

            del api.state.notification_groups[group_uuid]
            missing = await call(client, "explain_watch_notifications", {"uuid": watch_uuid})
            assert missing["resolution"]["content_changed"]["skipped_groups"]
            assert missing["resolution"]["content_changed"]["source"] == "watch_urls"

    anyio.run(scenario)


def test_destructive_confirm_does_not_mutate(api, mcp_app):
    async def scenario():
        async with Client(mcp_app) as client:
            created = await call(client, "create_watch", {"url": "https://example.com/keep"})
            watch_uuid = created["uuid"]
            refused = await client.call_tool("delete_watches", {"uuids": [watch_uuid], "confirm": False})
            assert refused.is_error
            assert watch_uuid in (await call(client, "list_watches"))["watches"]

            added = await call(client, "add_notification_urls", {"notification_urls": ["json://localhost/hook/secret"]})
            assert "secret" not in str(added)
            replace = await client.call_tool("replace_notification_urls", {
                "notification_urls": [],
                "confirm": False,
            })
            assert replace.is_error
            listed = await call(client, "list_notification_urls")
            assert listed["count"] == 1

            rejected = await client.call_tool("import_watches", {
                "urls": ["https://example.com/imported", "file:///etc/passwd"],
                "confirm": True,
            })
            assert rejected.is_error
            assert "example.com/imported" not in str((await call(client, "list_watches"))["watches"])

    anyio.run(scenario)


def test_import_validates_before_insert_and_recheck_needs_confirm(api, mcp_app):
    async def scenario():
        async with Client(mcp_app) as client:
            bad = await client.call_tool("import_watches", {
                "urls": ["https://example.com/ok", "file:///tmp/secret"],
                "confirm": True,
            })
            assert bad.is_error
            assert (await call(client, "list_watches"))["count"] == 0

            created = await call(client, "import_watches", {
                "urls": ["https://example.com/one", "https://user:secret@example.com/two"],
                "confirm": True,
                "tag": "Imported",
            })
            assert len(created["uuids"]) == 2
            exported = await call(client, "export_watch_urls", {"tag": "Imported"})
            assert "secret" not in exported["urls"]
            assert "https://user:***@example.com/two" in exported["urls"]

            refused = await client.call_tool("recheck_all_watches", {"confirm": False})
            assert refused.is_error
            await call(client, "recheck_all_watches", {"confirm": True})
            watch = await call(client, "get_watch", {"uuid": created["uuids"][0]})
            assert watch["last_checked"] == 1

            unknown = await client.call_tool("set_global_notification_routes", {"routes": {"restock": "abc"}})
            assert unknown.is_error

    anyio.run(scenario)


def test_pagination_and_auth_hint(api, mcp_app):
    async def scenario():
        async with Client(mcp_app) as client:
            await call(client, "create_watch", {"url": "https://example.com/a", "title": "A"})
            await call(client, "create_watch", {"url": "https://example.com/b", "title": "B"})
            page = await call(client, "list_watches", {"limit": 1, "offset": 0, "sort": "title"})
            assert page["count"] == 2
            assert page["page"]["returned"] == 1
            assert page["page"]["has_more"] is True
            bad_limit = await client.call_tool("list_watches", {"limit": 0})
            assert bad_limit.is_error
            path_id = await client.call_tool("get_watch", {"uuid": "../secret"})
            assert path_id.is_error

    anyio.run(scenario)
