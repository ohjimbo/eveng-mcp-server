"""Shared fixtures — a respx-mocked EVE-NG API and ready-made objects."""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from eveng_mcp.client import EvengClient
from eveng_mcp.config import Config
from eveng_mcp.inventory import PlatformMap

BASE = "https://eve.test/api"


def make_config(**env_overrides) -> Config:
    env = {
        "EVENG_HOST": "eve.test",
        "EVENG_USER": "admin",
        "EVENG_PASS": "secret",
        "MCP_AUTH_TOKEN": "test-token",
        **env_overrides,
    }
    return Config.from_env(env)


@pytest.fixture
def cfg() -> Config:
    return make_config()


@pytest.fixture
def client() -> EvengClient:
    return EvengClient("eve.test", "admin", "secret", protocol="https")


@pytest.fixture
def platforms() -> PlatformMap:
    return PlatformMap()


NODES_PAYLOAD = {
    "1": {
        "name": "spine1",
        "template": "vjunosevolved",
        "image": "vjunosevolved-23.2R1",
        "status": 2,
        "cpu": 4,
        "ram": 8192,
        "ethernet": 8,
        "url": "telnet://eve.test:32769",
        "console": "telnet",
        "ip": "10.0.0.11",
    },
    "2": {
        "name": "leaf1",
        "template": "veos",
        "image": "veos-4.30",
        "status": 2,
        "url": "telnet://eve.test:32770",
        "console": "telnet",
    },
    "3": {
        "name": "mystery",
        "template": "custom-fw-9",
        "status": 0,
        "url": "",
        "console": "vnc",
    },
}


@pytest.fixture
def eveng_api():
    """respx router with login + a standard lab pre-mocked."""
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE}/auth/login").mock(
            return_value=Response(
                200,
                json={"code": 200, "status": "success"},
                headers={"set-cookie": "unetlab_session=abc123; path=/"},
            )
        )
        router.get(f"{BASE}/labs/Test.unl/nodes").mock(
            return_value=Response(
                200, json={"code": 200, "status": "success", "data": NODES_PAYLOAD}
            )
        )
        router.get(f"{BASE}/list/templates/vjunosswitch").mock(
            return_value=Response(
                200, json={"code": 200, "data": {"options": {"ethernet": {"value": 11}}}}
            )
        )
        router.get(f"{BASE}/list/templates/veos").mock(
            return_value=Response(
                200, json={"code": 200, "data": {"options": {"ethernet": {"value": 8}}}}
            )
        )
        router.get(f"{BASE}/status").mock(
            return_value=Response(
                200,
                json={
                    "code": 200,
                    "status": "success",
                    "data": {"cpu": 12, "mem": 40, "disk": 33, "qemu": 3,
                             "version": "6.2.0-4", "qemu_version": "2.4.0"},
                },
            )
        )
        yield router
