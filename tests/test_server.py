"""Server assembly smoke tests — exercises the FastMCP API surface so a
library-version mismatch fails in CI, not at deploy."""

from __future__ import annotations

from eveng_mcp.server import build_mcp, create_app
from tests.conftest import make_config


async def test_build_mcp_registers_core_tools():
    mcp, report = build_mcp(make_config())
    names = {t.name for t in await mcp._list_tools()}
    for expected in (
        "eveng_list_labs", "eveng_create_lab", "eveng_build_topology",
        "eveng_inventory", "eveng_health", "eveng_capabilities",
        "console_command", "console_expect", "console_wait_for",
        "console_get_config", "console_halt", "eveng_halt_nodes",
        "skills_list", "skills_load", "eveng_api",
    ):
        assert expected in names, f"missing tool: {expected}"
    # no integrations configured → none of their tools registered
    assert "eveng_host_power_on" not in names
    assert "netbox_preview_lab" not in names
    assert report["enabled"] == []


async def test_read_only_server_has_no_write_tools():
    mcp, _ = build_mcp(make_config(EVENG_MCP_READ_ONLY="true"))
    names = {t.name for t in await mcp._list_tools()}
    assert "eveng_inventory" in names
    assert "eveng_health" in names
    for write_tool in ("eveng_create_lab", "eveng_wipe_nodes",
                       "eveng_build_topology", "console_send_config",
                       "console_write", "console_halt", "eveng_halt_nodes"):
        assert write_tool not in names, f"write tool leaked: {write_tool}"
    # default EVENG_RAW_API=read makes eveng_api a GET-only READ tool, so it
    # legitimately survives read-only mode
    assert "eveng_api" in names


async def test_raw_api_modes():
    from fastmcp import FastMCP

    from eveng_mcp.tools import raw

    # off → not registered (honest tool list)
    mcp = FastMCP(name="t")
    raw.register(mcp, make_config(EVENG_RAW_API="off"), client=None)
    assert "eveng_api" not in {t.name for t in await mcp._list_tools()}

    # read (default) → registered, but non-GET methods are refused
    mcp = FastMCP(name="t")
    raw.register(mcp, make_config(), client=None)
    result = await mcp.call_tool(
        "eveng_api", {"method": "DELETE", "path": "/labs/X.unl"}
    )
    payload = result.structured_content
    body = payload.get("result", payload) if isinstance(payload, dict) else payload
    assert "read-only" in body["error"]

    # bad value → hard config error
    import pytest

    from eveng_mcp.config import ConfigError
    with pytest.raises(ConfigError, match="EVENG_RAW_API"):
        raw.register(FastMCP(name="t"), make_config(EVENG_RAW_API="yes"), client=None)


async def test_mcp_annotations_mirror_tags():
    mcp, _ = build_mcp(make_config())
    tools = {t.name: t for t in await mcp._list_tools()}
    wipe = tools["eveng_wipe_nodes"].annotations
    assert wipe.readOnlyHint is False
    assert wipe.destructiveHint is True
    inv = tools["eveng_inventory"].annotations
    assert inv.readOnlyHint is True


def test_create_app_builds():
    app, report = create_app(make_config())
    paths = [getattr(r, "path", None) for r in app.routes]
    assert "/health" in paths


async def test_skills_list_marks_runnability():
    mcp, _ = build_mcp(make_config())
    result = await mcp.call_tool("skills_list", {})
    payload = result.structured_content
    body = payload.get("result", payload) if isinstance(payload, dict) else payload
    skills = {s["name"]: s for s in body["skills"]}
    assert skills["safe-lab-teardown"]["runnable"] is True  # no required integrations
    assert skills["build-fabric-from-netbox"]["runnable"] is False
    assert skills["build-fabric-from-netbox"]["missing_integrations"] == ["netbox"]
