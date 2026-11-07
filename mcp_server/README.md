# changedetection.io MCP server

Version 1.1.0. The server wraps the existing `/api/v1` REST API. It does not import the Flask application, does not add a delivery provider, and does not send notifications.

## Install

From a checkout, with the web app already running:

```bash
pip install -r mcp_server/requirements.txt
export CHANGEDETECTION_BASE_URL=http://127.0.0.1:5000
export CHANGEDETECTION_API_KEY=the-key-from-settings-api
python -m mcp_server
```

`python -m mcp_server` uses stdio. Streamable HTTP:

```bash
python -m mcp_server --transport streamable-http
```

Docker Compose starts the web app and this server as two containers. The image installs MCP dependencies into `/opt/mcp`, separate from the web app's Python path. `changedetection-mcp` is the wrapper.

| Variable | Purpose |
| --- | --- |
| `CHANGEDETECTION_BASE_URL` | App URL. Compose uses `http://changedetection:5000`. |
| `CHANGEDETECTION_API_KEY` | Sent as the `x-api-key` header. Copy it from Settings → API. |
| `CHANGEDETECTION_TIMEOUT` | API timeout in seconds. Default `60`. |
| `MCP_TRANSPORT` | `stdio` (default) or `streamable-http`. |
| `MCP_HOST` / `MCP_PORT` | HTTP bind address and port. Default port `8000`. |
| `MCP_BEARER_TOKEN` | Optional shared secret. When set, HTTP clients must send `Authorization: Bearer <token>`. stdio does not see HTTP headers, so this applies only to streamable HTTP. |
| `MCP_PUBLIC_URL` | URL advertised as the protected resource. Default `http://127.0.0.1:8000/mcp`. |

There is one API key and one datastore. The bearer token only guards the MCP HTTP listener. It is not a per-user login.

## Connect

Streamable HTTP:

```json
{
  "mcpServers": {
    "changedetection": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {"Authorization": "Bearer optional-shared-secret"}
    }
  }
}
```

Omit `headers` when `MCP_BEARER_TOKEN` is unset.

stdio:

```json
{
  "mcpServers": {
    "changedetection": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "env": {
        "CHANGEDETECTION_BASE_URL": "http://127.0.0.1:5000",
        "CHANGEDETECTION_API_KEY": "your-key-here",
        "PYTHONPATH": "/path/to/this/repo"
      }
    }
  }
}
```

## Notification groups

A notification group is a named list of Apprise URLs the app already accepts (`mailto://`, `discord://`, and the other built-in schemes). It is not a watch tag. The main menu label "Groups" means tags.

The workers send three alerts. Do not invent others:

| Alert | When it is sent |
| --- | --- |
| `content_changed` | The checked content changed. Restock and price changes use this same alert. |
| `filter_failure` | A CSS, XPath, JSONPath, or jq filter was missing. |
| `step_failure` | A configured browser step could not be run. |

For each alert, one destination list is chosen:

1. The watch's group, unless that watch is muted.
2. Otherwise the first tag that sets a group for that alert, unless that tag is muted. Tag order is the stored tag order, the same order the URL cascade already uses.
3. Otherwise the system group for that alert.
4. Otherwise the existing URL list. Content change uses the watch list, then tag lists, then Settings → Notifications, and it honors mute. Filter and browser-step alerts keep their previous behavior: the watch list, then the system list, without applying mute.

A selected group replaces the URL list for that alert, so the same destination is not notified twice. A group with no destinations notifies nobody for that alert. A missing or deleted group is skipped. Saving or deleting a group does not copy or erase existing notification URL lists. Deleting a group clears it from watches, tags, and the system routes; those alerts then use the next level.

Tool results redact destinations and secret headers. The list tools return redacted URLs, so a later replace must be given the original URLs, not the redacted copies. Replacing destinations and deleting a group require `confirm=true`.

Example:

```text
create_notification_group title="On-call" notification_urls=["mailto://oncall@example.com"]
set_watch_notification_routes uuid="<watch>" routes={"content_changed": "<group uuid>"}
explain_watch_notifications uuid="<watch>"
```

`explain_watch_notifications` reports the source (`watch_group`, `tag_group`, `global_group`, `watch_urls`, `tag_urls`, `global_urls`, or `none`), the redacted destinations, duplicates removed, skipped groups, and muted levels. It does not send anything.

## Action coverage

Tools that change more than one watch, replace a destination list, or delete a group require `confirm` to be exactly `true`. `delete_watch` stays a single-id delete without that flag so existing clients keep working, and it is still marked destructive.

| User action | Tool |
| --- | --- |
| List, search, and page watches | `list_watches`, `search_watches` |
| Read one watch | `get_watch` |
| History, snapshot, diff | `get_watch_history`, `get_watch_snapshot`, `get_watch_diff` |
| Create and edit a watch, including fetch method, processor, filters, headers, browser steps stored as config | `create_watch`, `update_watch` |
| Pause, resume, mute, recheck one watch | `pause_watch`, `unpause_watch`, `mute_watch`, `unmute_watch`, `recheck_watch` |
| The same for up to 50 watches, and recheck every watch | `pause_watches`, `unpause_watches`, `mute_watches`, `unmute_watches`, `recheck_watches`, `recheck_all_watches` |
| Delete one, or up to 50 | `delete_watch`, `delete_watches` |
| Import plaintext http(s) URLs, export URLs | `import_watches`, `export_watch_urls` |
| Tags (the UI label Groups) | `list_tags`, `get_tag`, `create_tag`, `update_tag`, `delete_tag`, `mute_tag`, `unmute_tag` |
| System notification URL list | `list_notification_urls`, `add_notification_urls`, `remove_notification_urls`, `replace_notification_urls` |
| Notification groups | `list_notification_groups`, `get_notification_group`, `create_notification_group`, `update_notification_group`, `add_notification_group_destinations`, `remove_notification_group_destinations`, `replace_notification_group_destinations`, `delete_notification_group` |
| Per-alert group assignment | `get_global_notification_routes`, `set_global_notification_routes`, `get_watch_notification_routes`, `set_watch_notification_routes`, `get_tag_notification_routes`, `set_tag_notification_routes`, `explain_watch_notifications` |
| Version, uptime, queue size, overdue watches | `get_system_info` |

`list_watches` can filter errors, unviewed watches, and a URL substring, and it can sort by title, URL, or last change. The list payload does not include paused state. Omit `limit` to return every match. A limit is 1 to 200.

`recheck_all_watches` follows the API: it queues every watch, including paused ones. The UI "recheck all" skips paused watches.

`import_watches` checks every URL before creating any watch. Only `http://` and `https://` are accepted.

### Intentionally not a tool

| UI or API action | Why it is not exposed |
| --- | --- |
| Send test notification | It delivers a real notification. |
| Favicon | Binary response, not a user action. |
| Clear history, mark viewed, clone | No REST route. Not composed on top of delete. |
| Share to changedetection.io | Uploads the watch to a third party. |
| Zip backup | The archive contains `secret.txt`. `export_watch_urls` is the URL export, with passwords redacted. |
| Settings password, workers, proxies, timezone, API key reset | No REST route. Resetting the key would lock out the MCP client and reveal a secret. |
| Excel and Distill.io import | The API import is plaintext URLs. The file importers exist only in the UI. |
| Browser-step recorder, proxy check | Interactive UI against the fetcher. |
| Price follower accept or reject | Not in the API schema. |
| RSS feed | A separate tokenized feed, not an MCP action. |
| Conditions verify, time schedule limits | Not settable through the API schema. |
| Delete every tag, unlink every tag | UI-only and too broad to compose. |
| Notification debug log | Operational log, not a supported user setting. |
| Worker health, gc cleanup | Queue size is already in `get_system_info`. |
| OAuth or per-user ownership | The app is one datastore and one API key. |
| Run `webdriver_js_execute_code` or browser steps | They can be stored on a watch. This server never executes them. |
| Shell, file, or arbitrary code execution | Out of scope. |

## Hosted multi-user gap

One process has one datastore and one API key. Notification groups are instance-wide, not per person. `MCP_BEARER_TOKEN` is a shared secret for the HTTP listener. This server does not add accounts, OAuth, or per-user watch ownership. A hosted install that needs isolation needs a separate changedetection.io instance per tenant.

## License

This directory is part of the changedetection.io tree and is covered by the Apache License 2.0 in `LICENSE`. There is no `NOTICE` file in the tree. `README.md` refers to `COMMERCIAL_LICENCE.md`; that file is not in this checkout. `changedetectionio.html_tools.elementpath_tostring` is attributed in the README to SISSA under the MIT license. The MCP server does not copy that code.

## Tests

```bash
pip install -r mcp_server/requirements.txt pytest
pytest mcp_server/tests
pytest changedetectionio/tests/test_notification_groups.py
```

The MCP tests use an in-process stand-in for `/api/v1`. They do not send notifications. The notification-group tests load `changedetectionio/tests/fixtures/notification_routing.json` and check routing, migration, redaction, and the dropdown page.
