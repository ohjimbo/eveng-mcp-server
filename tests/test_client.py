"""Client behavior: auth caching, re-login on 401/412, error surfacing."""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from eveng_mcp.client import EvengApiError, EvengAuthError, EvengClient
from tests.conftest import BASE


async def test_login_cached(eveng_api, client: EvengClient):
    await client.request("GET", "/status")
    await client.request("GET", "/status")
    calls = [c for c in eveng_api.calls if c.request.url.path == "/api/auth/login"]
    assert len(calls) == 1, "second request should reuse the cached cookie"


async def test_relogin_on_412(eveng_api, client: EvengClient):
    route = eveng_api.get(f"{BASE}/list/templates/")
    route.side_effect = [
        Response(412, json={"code": 412, "message": "expired"}),
        Response(200, json={"code": 200, "data": {"veos": "Arista vEOS"}}),
    ]
    data = await client.data("GET", "/list/templates/")
    assert data == {"veos": "Arista vEOS"}
    login_calls = [c for c in eveng_api.calls if c.request.url.path == "/api/auth/login"]
    assert len(login_calls) == 2, "412 must force exactly one re-login"


async def test_auth_fails_loudly_after_retry(eveng_api, client: EvengClient):
    eveng_api.get(f"{BASE}/status").mock(
        return_value=Response(412, json={"code": 412})
    )
    with pytest.raises(EvengAuthError):
        await client.request("GET", "/status")


async def test_api_error_raises(eveng_api, client: EvengClient):
    eveng_api.get(f"{BASE}/labs/Nope.unl").mock(
        return_value=Response(404, json={"code": 404, "message": "Lab does not exist"})
    )
    with pytest.raises(EvengApiError) as exc:
        await client.get_lab("/Nope.unl")
    assert exc.value.status_code == 404
    assert "Lab does not exist" in str(exc.value)


async def test_login_failure(client: EvengClient):
    with respx.mock() as router:
        router.post(f"{BASE}/auth/login").mock(return_value=Response(401, json={}))
        with pytest.raises(EvengAuthError):
            await client.request("GET", "/status")


async def test_lab_path_with_spaces_is_quoted(eveng_api, client: EvengClient):
    route = eveng_api.get(f"{BASE}/labs/Campus%20Fabric.unl/nodes").mock(
        return_value=Response(200, json={"code": 200, "data": {}})
    )
    await client.get_nodes("/Campus Fabric.unl")
    assert route.called


async def test_bulk_node_action_iterates_per_node(eveng_api, client: EvengClient):
    """EVE-NG PRO 6.4 rejects the aggregate /nodes/start|stop endpoints —
    bulk must iterate per node, skipping nodes already in the target state."""
    stop1 = eveng_api.get(f"{BASE}/labs/Test.unl/nodes/1/stop").mock(
        return_value=Response(200, json={"code": 200})
    )
    stop2 = eveng_api.get(f"{BASE}/labs/Test.unl/nodes/2/stop").mock(
        return_value=Response(200, json={"code": 200})
    )
    result = await client.node_action("/Test.unl", "stop", None)
    assert stop1.called and stop2.called
    # node 3 is already stopped (status 0) — must NOT be hit
    assert result["nodes"]["3"] == "already stopped"
    assert result["ok"] is True
