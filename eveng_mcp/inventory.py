"""Derived inventory — a pure function of (EVE-NG API, platforms.yaml, config).

Nothing is stored. The lab is the source of truth; platforms.yaml supplies
vendor/OS identity and console behavior; ``reachable_via`` tells consumers
which path (console / mgmt) actually works right now.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from eveng_mcp.client import EvengClient, EvengError

_SHIPPED_MAP = Path(__file__).parent / "platforms.yaml"

# EVE-NG node status codes
_STATUS = {0: "stopped", 1: "stopped", 2: "running", 3: "building"}


class PlatformMapError(EvengError):
    pass


@dataclass
class PlatformInfo:
    """Resolved platform identity for one template (family + template merge)."""

    template: str
    os_family: str | None = None
    vendor: str | None = None
    scrapli_platform: str | None = None
    console_block: dict = field(default_factory=dict)
    show_config: str | None = None
    halt: str | None = None
    boot_seconds: int = 60
    interfaces: dict = field(default_factory=dict)
    netbox_platform: str | None = None
    netbox_device_types: list[str] = field(default_factory=list)
    eve_type: str = "qemu"  # EVE-NG node type: qemu | iol | dynamips | docker | vpcs

    @property
    def console_tier(self) -> int:
        if self.scrapli_platform:
            return 1
        if self.console_block:
            return 2
        return 3


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class PlatformMap:
    """Loads platforms.yaml (shipped defaults, deep-merged with an optional
    user override file) and resolves template names to PlatformInfo."""

    def __init__(self, override_path: str | None = None):
        raw = yaml.safe_load(_SHIPPED_MAP.read_text(encoding="utf-8")) or {}
        if override_path:
            p = Path(override_path)
            if not p.exists():
                raise PlatformMapError(f"EVENG_PLATFORM_MAP file not found: {override_path}")
            user = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            raw = _deep_merge(raw, user)
        self.defaults: dict = raw.get("defaults", {}) or {}
        self.os_families: dict = raw.get("os_families", {}) or {}
        self.templates: dict = raw.get("templates", {}) or {}
        self.prefixes: dict = raw.get("prefixes", {}) or {}
        self._cache: dict[str, PlatformInfo | None] = {}

    def lookup(self, template: str) -> PlatformInfo | None:
        """Resolve a template name. Exact match first, then longest prefix.
        Returns None for unmapped templates (caller surfaces them loudly)."""
        if template in self._cache:
            return self._cache[template]
        info = self._resolve(template or "")
        self._cache[template] = info
        return info

    def _resolve(self, template: str) -> PlatformInfo | None:
        tpl_entry = self.templates.get(template)
        os_name = None
        if tpl_entry is not None:
            os_name = tpl_entry.get("os")
        else:
            t = template.lower()
            best = ""
            for prefix, fam in self.prefixes.items():
                if t.startswith(prefix.lower()) and len(prefix) > len(best):
                    best, os_name = prefix, fam
            if not os_name:
                return None
            tpl_entry = {}

        family = self.os_families.get(os_name, {}) if os_name else {}
        merged = _deep_merge(family, tpl_entry)
        return PlatformInfo(
            template=template,
            os_family=os_name,
            vendor=merged.get("vendor"),
            scrapli_platform=merged.get("scrapli"),
            console_block=merged.get("console", {}) or {},
            show_config=merged.get("show_config"),
            halt=merged.get("halt"),
            boot_seconds=int(
                merged.get("boot_seconds", self.defaults.get("boot_seconds", 60))
            ),
            interfaces=merged.get("interfaces", {}) or {},
            netbox_platform=merged.get("netbox_platform"),
            netbox_device_types=list(merged.get("netbox_device_types", []) or []),
            eve_type=merged.get("eve_type", "qemu"),
        )

    # ── interface name ↔ slot resolution (used by topology + netbox) ────────

    def slot_for_interface(self, template: str, interface: str) -> int:
        """Map 'ge-0/0/1' or a bare slot number to an EVE-NG slot index."""
        interface = interface.strip()
        if re.fullmatch(r"\d+", interface):
            return int(interface)
        info = self.lookup(template)
        if not info or not info.interfaces.get("naming"):
            raise PlatformMapError(
                f"Cannot resolve interface {interface!r} for template {template!r}: "
                "no interface naming pattern in platforms.yaml — use a numeric slot instead"
            )
        naming: str = info.interfaces["naming"]
        pattern = re.escape(naming).replace(re.escape("{n}"), r"(\d+)")
        m = re.fullmatch(pattern, interface)
        if not m:
            raise PlatformMapError(
                f"Interface {interface!r} does not match pattern {naming!r} "
                f"for template {template!r}"
            )
        n = int(m.group(1))
        return self._apply_slot_expr(info.interfaces.get("slot", "n"), n)

    def interface_for_slot(self, template: str, slot: int) -> str:
        """Inverse of slot_for_interface — best-effort display name for a slot."""
        info = self.lookup(template)
        if not info or not info.interfaces.get("naming"):
            return str(slot)
        expr = info.interfaces.get("slot", "n")
        n = self._invert_slot_expr(expr, slot)
        if n is None or n < 0:
            return str(slot)
        return info.interfaces["naming"].replace("{n}", str(n))

    @staticmethod
    def _apply_slot_expr(expr: str, n: int) -> int:
        expr = str(expr).replace(" ", "")
        if expr == "n":
            return n
        m = re.fullmatch(r"n([+-])(\d+)", expr)
        if not m:
            raise PlatformMapError(f"Unsupported slot expression: {expr!r}")
        delta = int(m.group(2))
        return n + delta if m.group(1) == "+" else n - delta

    @staticmethod
    def _invert_slot_expr(expr: str, slot: int) -> int | None:
        expr = str(expr).replace(" ", "")
        if expr == "n":
            return slot
        m = re.fullmatch(r"n([+-])(\d+)", expr)
        if not m:
            return None
        delta = int(m.group(2))
        return slot - delta if m.group(1) == "+" else slot + delta


# ── inventory build ─────────────────────────────────────────────────────────


def _parse_console(node: dict) -> dict:
    """Extract console reachability from an EVE-NG node object.

    The node's ``url`` field carries the allocated console endpoint
    (e.g. 'telnet://192.0.2.10:32769'); ``console`` names the protocol."""
    url = node.get("url") or ""
    protocol = (node.get("console") or "").lower() or None
    host = port = None
    if url.startswith("telnet://"):
        parsed = urlparse(url)
        host, port = parsed.hostname, parsed.port
        protocol = protocol or "telnet"
    elif url:
        protocol = protocol or ("vnc" if "vnc" in url else None)
    return {"protocol": protocol, "host": host, "port": port}


def node_to_device(
    lab_path: str, node_id: str | int, node: dict, platforms: PlatformMap
) -> dict:
    template = node.get("template", "") or ""
    info = platforms.lookup(template)
    state = _STATUS.get(node.get("status"), str(node.get("status")))
    console = _parse_console(node)
    mgmt_ip = node.get("ip") or None

    reachable: list[str] = []
    if state == "running" and console["protocol"] == "telnet" and console["port"]:
        reachable.append("console")
    if state == "running" and mgmt_ip:
        reachable.append("mgmt")

    return {
        "name": node.get("name", f"node-{node_id}"),
        "node_id": int(node_id),
        "lab_path": lab_path,
        "template": template,
        "image": node.get("image"),
        "vendor": info.vendor if info else None,
        "os_family": info.os_family if info else None,
        "state": state,
        "cpu": node.get("cpu"),
        "ram": node.get("ram"),
        "ethernet": node.get("ethernet"),
        "mgmt": {"ip": mgmt_ip, "port": 22, "protocol": "ssh"} if mgmt_ip else None,
        "console": console,
        "console_tier": info.console_tier if info else 3,
        "boot_seconds": info.boot_seconds if info else 60,
        "reachable_via": reachable,
    }


async def build_inventory(
    client: EvengClient,
    platforms: PlatformMap,
    lab_path: str | None = None,
    running_only: bool = False,
    vendor: str | None = None,
    include_links: bool = False,
) -> dict:
    lab_paths = [lab_path] if lab_path else await client.walk_labs()

    devices: list[dict] = []
    links: list[Any] = []
    unmapped: set[str] = set()
    lab_errors: dict[str, str] = {}

    for lp in lab_paths:
        try:
            nodes = await client.get_nodes(lp)
        except EvengError as e:
            lab_errors[lp] = str(e)
            continue
        for node_id, node in nodes.items():
            dev = node_to_device(lp, node_id, node, platforms)
            if dev["vendor"] is None and dev["template"]:
                unmapped.add(dev["template"])
            if running_only and dev["state"] != "running":
                continue
            if vendor and dev["vendor"] != vendor:
                continue
            devices.append(dev)
        if include_links:
            try:
                topo = await client.get_topology(lp)
                if isinstance(topo, list):
                    links.extend({"lab_path": lp, **t} for t in topo if isinstance(t, dict))
            except EvengError as e:
                lab_errors[lp] = f"topology: {e}"

    by_vendor: dict[str, int] = {}
    for d in devices:
        key = d["vendor"] or "unmapped"
        by_vendor[key] = by_vendor.get(key, 0) + 1

    result = {
        "labs": lab_paths,
        "devices": devices,
        "unmapped_templates": sorted(unmapped),
        "summary": {
            "total": len(devices),
            "running": sum(1 for d in devices if d["state"] == "running"),
            "by_vendor": by_vendor,
        },
    }
    if include_links:
        result["links"] = links
    if lab_errors:
        result["lab_errors"] = lab_errors
    return result
