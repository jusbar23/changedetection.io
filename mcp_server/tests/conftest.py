import pytest

from mcp_server.client import ChangeDetectionClient
from mcp_server.server import build_server
from mcp_server.tests.fake_api import FakeChangeDetectionAPI


@pytest.fixture
def api():
    server = FakeChangeDetectionAPI()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def cd_client(api):
    client = ChangeDetectionClient(api.base_url, api.api_key, timeout=5)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def mcp_app(cd_client):
    return build_server(cd_client)
