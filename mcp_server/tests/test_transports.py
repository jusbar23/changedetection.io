"""The server speaks stdio and streamable HTTP, including optional bearer auth."""

import socket
import sys
import threading
import urllib.error
import urllib.request

import anyio
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from mcp_server.client import ChangeDetectionClient, ChangeDetectionError
from mcp_server.server import build_server
from mcp_server.tests.helpers import call


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve(server, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    app = server.streamable_http_app(
        host="127.0.0.1",
        stateless_http=True,
        json_response=True,
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    http_server = uvicorn.Server(config)
    thread = threading.Thread(target=http_server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if http_server.started:
            return http_server, thread
        thread.join(0.05)
    raise RuntimeError("MCP HTTP server did not start")


def _stop(http_server: uvicorn.Server, thread: threading.Thread) -> None:
    http_server.should_exit = True
    thread.join(timeout=5)


def test_streamable_http_lists_tools_and_manages_a_watch(cd_client):
    port = _free_port()
    server = build_server(cd_client, bearer_token="secret", public_url=f"http://127.0.0.1:{port}/mcp")
    http_server, thread = _serve(server, port)
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        async def scenario():
            _expect_http_status(url, 401)

            async with httpx2.AsyncClient(
                headers={"Authorization": "Bearer secret"},
                timeout=httpx2.Timeout(30.0, read=30.0),
            ) as http_client:
                transport = streamable_http_client(url, http_client=http_client)
                async with Client(transport) as client:
                    listed = await client.list_tools()
                    names = {tool.name for tool in listed.tools}
                    assert "create_watch" in names
                    assert "get_system_info" in names
                    created = await call(client, "create_watch", {
                        "url": "https://example.com/http-transport",
                        "title": "HTTP",
                    })
                    watch_uuid = created["uuid"]
                    watches = await call(client, "list_watches")
                    assert watch_uuid in watches["watches"]
                    await call(client, "delete_watch", {"uuid": watch_uuid})
                    watches = await call(client, "list_watches")
                    assert watch_uuid not in watches["watches"]

            _expect_http_status(url, 401, token="wrong")

        anyio.run(scenario)
    finally:
        _stop(http_server, thread)


def test_stdio_lists_tools_and_creates_a_watch(api):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server"],
        cwd="/workspace",
        env={
            "PYTHONPATH": "/workspace",
            "CHANGEDETECTION_BASE_URL": api.base_url,
            "CHANGEDETECTION_API_KEY": api.api_key,
            "MCP_TRANSPORT": "stdio",
        },
    )

    async def scenario():
        async with Client(params) as client:
            listed = await client.list_tools()
            assert "delete_watch" in {tool.name for tool in listed.tools}
            created = await call(client, "create_watch", {
                "url": "https://example.com/stdio",
                "title": "Stdio",
            })
            watch_uuid = created["uuid"]
            watches = await call(client, "list_watches")
            assert watch_uuid in watches["watches"]
            await call(client, "delete_watch", {"uuid": watch_uuid})
            watches = await call(client, "list_watches")
            assert watch_uuid not in watches["watches"]

    anyio.run(scenario)


def test_cli_rejects_bad_config(monkeypatch):
    from mcp_server.cli import main

    monkeypatch.setenv("CHANGEDETECTION_BASE_URL", "not-a-url")
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_client_timeout_reaches_a_closed_port():
    client = ChangeDetectionClient("http://127.0.0.1:1", api_key="x", timeout=1)
    try:
        with pytest.raises(ChangeDetectionError):
            client.get_system_info()
    finally:
        client.close()


def _expect_http_status(url: str, status: int, token: str | None = None) -> None:
    request = urllib.request.Request(url, data=b"{}", method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json, text/event-stream")
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raise AssertionError(f"expected HTTP {status}, got {response.status}")
    except urllib.error.HTTPError as exc:
        assert exc.code == status
