"""Loader gating: auto-enable, skip reporting, explicit-but-unmet = error."""

from __future__ import annotations

import pytest
from fastmcp import FastMCP

from eveng_mcp.config import ConfigError
from eveng_mcp.inventory import PlatformMap
from eveng_mcp.loader import load_integrations
from tests.conftest import make_config


@pytest.fixture
def mcp() -> FastMCP:
    return FastMCP(name="test")


def _names(entries: list[dict]) -> set[str]:
    return {e["name"] for e in entries}


def test_nothing_configured_all_skipped(mcp, client, platforms: PlatformMap):
    report = load_integrations(mcp, make_config(), client, platforms)
    assert report["enabled"] == []
    assert _names(report["skipped"]) == {"gitlab_backup", "idrac", "junos_mcp", "netbox"}
    for s in report["skipped"]:
        assert "not set" in s["reason"]


def test_auto_enable_by_config_presence(mcp, client, platforms: PlatformMap):
    cfg = make_config(JMCP_URL="http://localhost:30030")
    report = load_integrations(mcp, cfg, client, platforms)
    assert _names(report["enabled"]) == {"junos_mcp"}


async def test_enabled_tools_are_registered(mcp, client, platforms: PlatformMap):
    cfg = make_config(JMCP_URL="http://localhost:30030")
    load_integrations(mcp, cfg, client, platforms)
    tools = await mcp._list_tools()
    names = {t.name for t in tools}
    assert "eveng_register_junos_devices" in names
    assert "eveng_host_power_on" not in names  # idrac not configured


def test_explicit_but_unconfigured_is_hard_error(mcp, client, platforms: PlatformMap):
    cfg = make_config(EVENG_MCP_INTEGRATIONS="idrac")
    with pytest.raises(ConfigError, match="idrac"):
        load_integrations(mcp, cfg, client, platforms)


def test_explicit_allowlist_excludes_others(mcp, client, platforms: PlatformMap):
    cfg = make_config(
        EVENG_MCP_INTEGRATIONS="junos_mcp",
        JMCP_URL="http://localhost:30030",
        NETBOX_URL="https://nb.test",
        NETBOX_TOKEN="tok",
    )
    report = load_integrations(mcp, cfg, client, platforms)
    assert _names(report["enabled"]) == {"junos_mcp"}
    netbox_skip = next(s for s in report["skipped"] if s["name"] == "netbox")
    assert "EVENG_MCP_INTEGRATIONS" in netbox_skip["reason"]


def test_disable_beats_config_presence(mcp, client, platforms: PlatformMap):
    cfg = make_config(
        JMCP_URL="http://localhost:30030", EVENG_MCP_DISABLE="junos_mcp"
    )
    report = load_integrations(mcp, cfg, client, platforms)
    assert report["enabled"] == []


def test_unknown_explicit_name_is_error(mcp, client, platforms: PlatformMap):
    cfg = make_config(EVENG_MCP_INTEGRATIONS="nonexistent")
    with pytest.raises(ConfigError, match="nonexistent"):
        load_integrations(mcp, cfg, client, platforms)


async def test_read_only_skips_write_tools(client, platforms: PlatformMap):
    from eveng_mcp.tools import labs, nodes

    mcp = FastMCP(name="ro")
    cfg = make_config(EVENG_MCP_READ_ONLY="true")
    labs.register(mcp, cfg, client)
    nodes.register(mcp, cfg, client, platforms)
    names = {t.name for t in await mcp._list_tools()}
    assert "eveng_list_labs" in names
    assert "eveng_list_nodes" in names
    assert "eveng_create_lab" not in names
    assert "eveng_wipe_nodes" not in names
