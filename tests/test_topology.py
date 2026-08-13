"""build_topology: validation before execution, fail-fast reporting, slots."""

from __future__ import annotations

import json

from httpx import Response

from eveng_mcp.tools.topology import build_topology
from tests.conftest import BASE

SPEC = {
    "nodes": [
        {"name": "R1", "template": "vjunosswitch"},
        {"name": "R2", "template": "veos"},
    ],
    "links": [{"a": "R1:ge-0/0/0", "b": "R2:Ethernet1"}],
}


async def test_build_happy_path(eveng_api, client, platforms):
    eveng_api.post(f"{BASE}/labs/Build.unl/nodes").side_effect = [
        Response(201, json={"code": 201, "data": {"id": 1}}),
        Response(201, json={"code": 201, "data": {"id": 2}}),
    ]
    eveng_api.post(f"{BASE}/labs/Build.unl/networks").mock(
        return_value=Response(201, json={"code": 201, "data": {"id": 7}})
    )
    put_r1 = eveng_api.put(f"{BASE}/labs/Build.unl/nodes/1/interfaces").mock(
        return_value=Response(201, json={"code": 201})
    )
    put_r2 = eveng_api.put(f"{BASE}/labs/Build.unl/nodes/2/interfaces").mock(
        return_value=Response(201, json={"code": 201})
    )

    result = await build_topology(client, platforms, "/Build.unl", SPEC)
    assert result["success"] is True
    assert result["node_ids"] == {"R1": 1, "R2": 2}
    assert result["failed"] is None
    # ge-0/0/0 → slot 1 (junos offset); Ethernet1 → slot 1
    assert json.loads(put_r1.calls[0].request.content) == {"1": "7"}
    assert json.loads(put_r2.calls[0].request.content) == {"1": "7"}
    # required slots (2) < template defaults (11/8) → ethernet must NOT be
    # sent, or the node would be created with FEWER NICs than its default
    node_posts = [c for c in eveng_api.calls
                  if c.request.url.path == "/api/labs/Build.unl/nodes"
                  and c.request.method == "POST"]
    for call in node_posts:
        assert "ethernet" not in json.loads(call.request.content)


async def test_validation_before_any_api_call(eveng_api, client, platforms):
    bad = {
        "nodes": [{"name": "R1", "template": "veos"}],
        "links": [{"a": "R1:Ethernet1", "b": "GHOST:0"}],
    }
    result = await build_topology(client, platforms, "/Build.unl", bad)
    assert "error" in result
    # nothing was created
    api_paths = [c.request.url.path for c in eveng_api.calls]
    assert not any("/labs/Build.unl" in p for p in api_paths)


async def test_bad_interface_name_rejected(eveng_api, client, platforms):
    bad = {
        "nodes": [{"name": "R1", "template": "veos"}],
        "links": [{"a": "R1:ge-0/0/0", "network": "x"}],
        "networks": [{"name": "x"}],
    }
    result = await build_topology(client, platforms, "/Build.unl", bad)
    assert "error" in result


async def test_fail_fast_reports_remaining(eveng_api, client, platforms):
    eveng_api.post(f"{BASE}/labs/Build.unl/nodes").side_effect = [
        Response(201, json={"code": 201, "data": {"id": 1}}),
        Response(400, json={"code": 400, "message": "no space"}),
    ]
    result = await build_topology(client, platforms, "/Build.unl", SPEC)
    assert result["success"] is False
    assert result["done"] == ["node:R1"]
    assert "node:R2" in result["failed"]
    assert result["not_attempted"] == ["link:R1:1<->R2:1"]


async def test_missing_template_rejected_before_api(eveng_api, client, platforms):
    result = await build_topology(
        client, platforms, "/Build.unl",
        {"nodes": [{"name": "R1"}], "networks": [{"name": "mgmt"}]},
    )
    assert result["error"] == "Every node needs a non-empty 'template'"
    assert result["nodes"] == ["R1"]
    api_paths = [c.request.url.path for c in eveng_api.calls]
    assert not any("/labs/Build.unl" in p for p in api_paths)


async def test_disconnect_sends_empty_string(eveng_api, client):
    """EVE-NG only disconnects on '' — '0' would 400 with 'network not found'."""
    route = eveng_api.put(f"{BASE}/labs/Test.unl/nodes/1/interfaces").mock(
        return_value=Response(201, json={"code": 201})
    )
    await client.connect_interface("/Test.unl", 1, 1, 0)
    assert json.loads(route.calls[0].request.content) == {"1": ""}
    await client.connect_interface("/Test.unl", 1, 1, 7)
    assert json.loads(route.calls[1].request.content) == {"1": "7"}


async def test_duplicate_names_rejected(eveng_api, client, platforms):
    result = await build_topology(
        client, platforms, "/Build.unl",
        {"nodes": [{"name": "R1", "template": "veos"},
                   {"name": "R1", "template": "veos"}]},
    )
    assert "error" in result
