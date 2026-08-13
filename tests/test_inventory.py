"""Inventory: platform mapping, unmapped templates, reachable_via, slots."""

from __future__ import annotations

import pytest

from eveng_mcp.inventory import PlatformMap, PlatformMapError, build_inventory


def test_exact_template_lookup(platforms: PlatformMap):
    info = platforms.lookup("vjunosevolved")
    assert info is not None
    assert info.vendor == "juniper"
    assert info.os_family == "junos"
    assert info.scrapli_platform == "juniper_junos"
    assert info.console_tier == 1
    assert info.boot_seconds == 420


def test_prefix_fallback(platforms: PlatformMap):
    info = platforms.lookup("vjunosevolved-23.2R1-custom")
    assert info is not None and info.os_family == "junos"


def test_unmapped_returns_none(platforms: PlatformMap):
    assert platforms.lookup("custom-fw-9") is None


def test_console_tiers(platforms: PlatformMap):
    assert platforms.lookup("veos").console_tier == 1  # driver
    assert platforms.lookup("vyos").console_tier == 2  # expect block
    # linux family has a console block → tier 2, not 3
    assert platforms.lookup("linux").console_tier == 2


def test_eve_type(platforms: PlatformMap):
    assert platforms.lookup("iol").eve_type == "iol"
    assert platforms.lookup("vpcs").eve_type == "vpcs"
    assert platforms.lookup("veos").eve_type == "qemu"


def test_slot_resolution_junos_offset(platforms: PlatformMap):
    # junos data plane starts at slot 1 (slot 0 = fxp0)
    assert platforms.slot_for_interface("vjunosswitch", "ge-0/0/0") == 1
    assert platforms.slot_for_interface("vjunosswitch", "ge-0/0/3") == 4


def test_slot_resolution_eos_direct(platforms: PlatformMap):
    assert platforms.slot_for_interface("veos", "Ethernet1") == 1


def test_slot_numeric_passthrough(platforms: PlatformMap):
    assert platforms.slot_for_interface("anything", "3") == 3


def test_slot_bad_interface_raises(platforms: PlatformMap):
    with pytest.raises(PlatformMapError):
        platforms.slot_for_interface("veos", "GigabitEthernet0/0")


def test_interface_for_slot_roundtrip(platforms: PlatformMap):
    assert platforms.interface_for_slot("vjunosswitch", 1) == "ge-0/0/0"
    assert platforms.interface_for_slot("veos", 2) == "Ethernet2"


async def test_build_inventory(eveng_api, client, platforms):
    inv = await build_inventory(client, platforms, lab_path="/Test.unl")
    assert inv["summary"]["total"] == 3
    assert inv["summary"]["running"] == 2
    assert inv["unmapped_templates"] == ["custom-fw-9"]
    assert inv["summary"]["by_vendor"] == {"juniper": 1, "arista": 1, "unmapped": 1}

    by_name = {d["name"]: d for d in inv["devices"]}
    spine = by_name["spine1"]
    assert spine["reachable_via"] == ["console", "mgmt"]
    assert spine["console"] == {"protocol": "telnet", "host": "eve.test", "port": 32769}
    assert spine["mgmt"]["ip"] == "10.0.0.11"

    leaf = by_name["leaf1"]
    assert leaf["reachable_via"] == ["console"]  # running, console, no mgmt IP

    mystery = by_name["mystery"]
    assert mystery["vendor"] is None
    assert mystery["reachable_via"] == []  # stopped
    assert mystery["console_tier"] == 3


async def test_inventory_filters(eveng_api, client, platforms):
    inv = await build_inventory(
        client, platforms, lab_path="/Test.unl", running_only=True, vendor="juniper"
    )
    assert [d["name"] for d in inv["devices"]] == ["spine1"]
    # unmapped templates are reported even when filtered out of devices
    assert inv["unmapped_templates"] == ["custom-fw-9"]
