import anyio
from mcp import Client

from mcp_server.tests.helpers import call

EXPECTED_TOOLS = {
    "list_watches",
    "search_watches",
    "get_watch",
    "get_watch_history",
    "get_watch_snapshot",
    "get_watch_diff",
    "create_watch",
    "update_watch",
    "delete_watch",
    "pause_watch",
    "unpause_watch",
    "mute_watch",
    "unmute_watch",
    "recheck_watch",
    "list_tags",
    "get_tag",
    "create_tag",
    "update_tag",
    "delete_tag",
    "mute_tag",
    "unmute_tag",
    "get_system_info",
    "pause_watches",
    "unpause_watches",
    "mute_watches",
    "unmute_watches",
    "recheck_watches",
    "recheck_all_watches",
    "delete_watches",
    "import_watches",
    "export_watch_urls",
    "list_notification_urls",
    "add_notification_urls",
    "remove_notification_urls",
    "replace_notification_urls",
    "list_notification_groups",
    "get_notification_group",
    "create_notification_group",
    "update_notification_group",
    "add_notification_group_destinations",
    "remove_notification_group_destinations",
    "replace_notification_group_destinations",
    "delete_notification_group",
    "get_global_notification_routes",
    "set_global_notification_routes",
    "get_watch_notification_routes",
    "set_watch_notification_routes",
    "get_tag_notification_routes",
    "set_tag_notification_routes",
    "explain_watch_notifications",
}


def test_tool_catalogue(mcp_app):
    async def scenario():
        async with Client(mcp_app) as client:
            listed = await client.list_tools()
            names = {tool.name for tool in listed.tools}
            assert names == EXPECTED_TOOLS
            create = next(tool for tool in listed.tools if tool.name == "create_watch")
            assert "url" in create.input_schema["required"]
            assert create.annotations is not None
            assert create.annotations.read_only_hint is False
            info = next(tool for tool in listed.tools if tool.name == "get_system_info")
            assert info.annotations.read_only_hint is True
            groups = next(tool for tool in listed.tools if tool.name == "list_notification_groups")
            assert groups.annotations.read_only_hint is True
            bulk_delete = next(tool for tool in listed.tools if tool.name == "delete_watches")
            assert bulk_delete.annotations.destructive_hint is True
            assert "confirm" in bulk_delete.input_schema["required"]
            single_delete = next(tool for tool in listed.tools if tool.name == "delete_watch")
            assert "confirm" not in single_delete.input_schema.get("required", [])

    anyio.run(scenario)


def test_watch_lifecycle_and_diff(api, mcp_app):
    async def scenario():
        async with Client(mcp_app) as client:
            created = await call(client, "create_watch", {
                "url": "https://example.com/item",
                "title": "Item",
                "tag": "Shop",
                "check_hours": 4,
                "include_filters": ["h1", "json:$.price"],
                "subtractive_selectors": [".ads"],
                "notification_urls": ["discord://hook/token"],
                "ignore_text": ["sponsored"],
            })
            watch_uuid = created["uuid"]
            watch = created["watch"]
            assert watch["title"] == "Item"
            assert watch["include_filters"] == ["h1", "json:$.price"]
            assert watch["notification_urls"] == ["discord://***/***"]
            assert "token" not in str(watch["notification_urls"])
            assert watch["time_between_check"]["hours"] == 4
            assert watch["time_between_check_use_default"] is False

            listed = await call(client, "list_watches", {"tag": "shop"})
            assert listed["count"] == 1
            assert watch_uuid in listed["watches"]

            found = await call(client, "search_watches", {"query": "item"})
            assert watch_uuid in found["watches"]
            exact_miss = await call(client, "search_watches", {"query": "example.com", "partial": False})
            assert exact_miss["count"] == 0
            partial_hit = await call(client, "search_watches", {"query": "example.com", "partial": True})
            assert watch_uuid in partial_hit["watches"]

            updated = await call(client, "update_watch", {
                "uuid": watch_uuid,
                "title": "Item page",
                "check_minutes": 30,
                "notification_urls": ["mailto://user:pass@example.com"],
            })
            assert updated["watch"]["title"] == "Item page"
            assert updated["watch"]["time_between_check"]["minutes"] == 30
            assert updated["watch"]["time_between_check"]["hours"] is None
            assert updated["watch"]["notification_urls"] == ["mailto://user:***@example.com"]
            assert "pass" not in str(updated["watch"]["notification_urls"])

            await call(client, "pause_watch", {"uuid": watch_uuid})
            assert (await call(client, "get_watch", {"uuid": watch_uuid}))["paused"] is True
            await call(client, "unpause_watch", {"uuid": watch_uuid})
            await call(client, "mute_watch", {"uuid": watch_uuid})
            muted = await call(client, "get_watch", {"uuid": watch_uuid})
            assert muted["notification_muted"] is True
            assert muted["paused"] is False
            await call(client, "unmute_watch", {"uuid": watch_uuid})
            rechecked = await call(client, "recheck_watch", {"uuid": watch_uuid})
            assert rechecked["recheck_queued"] is True

            empty = await call(client, "get_watch_diff", {"uuid": watch_uuid})
            assert empty["changed"] is False
            assert "two snapshots" in empty["message"]

            api.add_snapshot(watch_uuid, "10", "Price: 10\nIn stock\n")
            api.add_snapshot(watch_uuid, "20", "Price: 8\nIn stock\n")
            history = await call(client, "get_watch_history", {"uuid": watch_uuid})
            assert history["timestamps"] == ["10", "20"]
            assert history["latest"] == "20"
            assert history["previous"] == "10"

            latest = await call(client, "get_watch_snapshot", {"uuid": watch_uuid, "timestamp": "latest"})
            assert latest["content"] == "Price: 8\nIn stock\n"
            previous = await call(client, "get_watch_snapshot", {"uuid": watch_uuid, "timestamp": "previous", "max_chars": 5})
            assert previous["content"] == "Price"
            assert previous["truncated"] is True

            diff = await call(client, "get_watch_diff", {"uuid": watch_uuid})
            assert diff["from_timestamp"] == "10"
            assert diff["to_timestamp"] == "20"
            assert diff["changed"] is True
            assert "-Price: 10" in diff["diff"]
            assert "+Price: 8" in diff["diff"]
            assert diff["additions"] == 1
            assert diff["deletions"] == 1

            explicit = await call(client, "get_watch_diff", {
                "uuid": watch_uuid,
                "from_timestamp": "20",
                "to_timestamp": "10",
            })
            assert explicit["from_timestamp"] == "20"
            assert "+Price: 10" in explicit["diff"]

            info = await call(client, "get_system_info")
            assert info["watch_count"] == 1
            assert info["version"] == "test"

            tags = await call(client, "list_tags")
            assert tags["count"] == 1
            tag_uuid = next(iter(tags["tags"]))
            renamed = await call(client, "update_tag", {"uuid": tag_uuid, "title": "Store"})
            assert renamed["tag"]["title"] == "Store"
            await call(client, "mute_tag", {"uuid": tag_uuid})
            assert (await call(client, "get_tag", {"uuid": tag_uuid}))["notification_muted"] is True
            await call(client, "unmute_tag", {"uuid": tag_uuid})

            extra = await call(client, "create_tag", {"title": "Pinned"})
            await call(client, "update_watch", {"uuid": watch_uuid, "tag": "Pinned"})
            retagged = await call(client, "get_watch", {"uuid": watch_uuid})
            assert retagged["tags"] == [extra["uuid"]]

            await call(client, "delete_tag", {"uuid": extra["uuid"]})
            assert extra["uuid"] not in (await call(client, "get_watch", {"uuid": watch_uuid}))["tags"]

            await call(client, "delete_watch", {"uuid": watch_uuid})
            remaining = await call(client, "list_watches")
            assert watch_uuid not in remaining["watches"]

    anyio.run(scenario)


def test_tool_errors_are_reported(mcp_app):
    async def scenario():
        async with Client(mcp_app) as client:
            missing = await client.call_tool("get_watch", {"uuid": "does-not-exist"})
            assert missing.is_error
            empty = await client.call_tool("update_watch", {"uuid": "does-not-exist"})
            assert empty.is_error
            bad = await client.call_tool("create_watch", {"url": "not a url"})
            assert bad.is_error

    anyio.run(scenario)
