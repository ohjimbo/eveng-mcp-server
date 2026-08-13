"""Console tools — three tiers of vendor-agnostic serial-console access.

tier 1 (driver): console_command / console_get_config / console_send_config
tier 2 (expect): console_expect
tier 3 (raw):    console_read / console_write / console_wait_for
"""

from __future__ import annotations

import time

from eveng_mcp.client import EvengClient, EvengError
from eveng_mcp.config import Config
from eveng_mcp.console import TelnetConsole, driver_run
from eveng_mcp.inventory import PlatformMap, node_to_device
from eveng_mcp.tools._common import make_tool


async def _find_device(
    client: EvengClient, platforms: PlatformMap, lab_path: str, node: str
) -> dict:
    """Resolve a node by name or id to its inventory view; require a usable
    telnet console."""
    nodes = await client.get_nodes(lab_path)
    match = None
    for node_id, obj in nodes.items():
        if obj.get("name") == node or str(node_id) == str(node):
            match = (node_id, obj)
            break
    if match is None:
        raise EvengError(
            f"Node {node!r} not found in {lab_path}. "
            f"Available: {sorted(o.get('name', '?') for o in nodes.values())}"
        )
    dev = node_to_device(lab_path, match[0], match[1], platforms)
    if dev["state"] != "running":
        raise EvengError(f"Node {dev['name']} is {dev['state']} — start it first")
    con = dev["console"]
    if con["protocol"] != "telnet" or not con["port"]:
        raise EvengError(
            f"Node {dev['name']} has no telnet console "
            f"(protocol={con['protocol']}) — VNC consoles cannot be text-driven"
        )
    return dev


def register(mcp, cfg: Config, client: EvengClient, platforms: PlatformMap) -> None:
    tool = make_tool(mcp, cfg)

    def _creds(username: str, password: str) -> tuple[str, str]:
        return username or cfg.console_user, password or cfg.console_pass

    def _require_tier1(dev: dict) -> str:
        info = platforms.lookup(dev["template"])
        if not info or not info.scrapli_platform:
            raise EvengError(
                f"Template {dev['template']!r} has no driver (console tier "
                f"{dev['console_tier']}) — use console_expect or console_read/"
                "console_write, or add a scrapli platform in platforms.yaml"
            )
        return info.scrapli_platform

    # ── tier 1 ──────────────────────────────────────────────────────────────

    @tool()
    async def console_command(
        lab_path: str,
        node: str,
        command: str,
        username: str = "",
        password: str = "",
    ) -> dict:
        """Run one operational command on a node via its console, with proper
        prompt/paging handling (tier-1, driver-backed templates only).

        Args:
            lab_path: e.g. '/MyLab.unl'.
            node: node name or numeric id.
            command: operational command, e.g. 'show version'.
            username/password: console login (default: CONSOLE_USER/PASS env).
        """
        dev = await _find_device(client, platforms, lab_path, node)
        platform = _require_tier1(dev)
        user, pwd = _creds(username, password)
        return await driver_run(
            platform, dev["console"]["host"], dev["console"]["port"],
            commands=[command], username=user, password=pwd,
        )

    @tool()
    async def console_get_config(
        lab_path: str, node: str, username: str = "", password: str = ""
    ) -> dict:
        """Fetch a node's running configuration via console (tier-1 templates;
        uses the platform's show_config command from platforms.yaml).

        Args:
            lab_path: e.g. '/MyLab.unl'.
            node: node name or numeric id.
            username/password: console login (default: CONSOLE_USER/PASS env).
        """
        dev = await _find_device(client, platforms, lab_path, node)
        platform = _require_tier1(dev)
        info = platforms.lookup(dev["template"])
        if not info or not info.show_config:
            raise EvengError(
                f"No show_config command mapped for template {dev['template']!r}"
            )
        user, pwd = _creds(username, password)
        result = await driver_run(
            platform, dev["console"]["host"], dev["console"]["port"],
            commands=[info.show_config], username=user, password=pwd,
        )
        out = result["results"][0] if result.get("results") else {}
        return {"node": dev["name"], "command": info.show_config,
                "failed": out.get("failed"), "config": out.get("output", "")}

    @tool(write=True)
    async def console_send_config(
        lab_path: str,
        node: str,
        configs: list[str],
        username: str = "",
        password: str = "",
    ) -> dict:
        """Push configuration lines to a node via console — enters config
        mode, applies lines, commits/ends per the platform's semantics
        (tier-1 templates only).

        Args:
            lab_path: e.g. '/MyLab.unl'.
            node: node name or numeric id.
            configs: config lines in the device's native syntax.
            username/password: console login (default: CONSOLE_USER/PASS env).
        """
        dev = await _find_device(client, platforms, lab_path, node)
        platform = _require_tier1(dev)
        user, pwd = _creds(username, password)
        info = platforms.lookup(dev["template"])
        # Some platforms (junos) hold changes in a candidate config until an
        # explicit commit — append it unless the caller already did.
        if (info and info.config_commit
                and not any(c.strip().startswith(info.config_commit)
                            for c in configs)):
            configs = [*configs, info.config_commit]
        return await driver_run(
            platform, dev["console"]["host"], dev["console"]["port"],
            configs=configs, username=user, password=pwd,
        )

    # ── tier 2 ──────────────────────────────────────────────────────────────

    @tool(write=True)
    async def console_expect(
        lab_path: str,
        node: str,
        expect: str,
        send: str = "",
        timeout: float = 30,
    ) -> dict:
        """Generic expect: optionally send a line, then read until a regex
        matches. Works on ANY node with a telnet console — this is the escape
        hatch for templates without a driver.

        Args:
            lab_path: e.g. '/MyLab.unl'.
            node: node name or numeric id.
            expect: regex to wait for (searched over accumulated output).
            send: text to send first; '\\r' is appended unless it already ends
                with a newline. Empty = just read.
            timeout: seconds to wait for the pattern.
        """
        dev = await _find_device(client, platforms, lab_path, node)
        async with TelnetConsole(dev["console"]["host"], dev["console"]["port"]) as con:
            if send:
                if not send.endswith(("\n", "\r")):
                    send += "\r"
                await con.write(send)
            result = await con.expect(expect, timeout=timeout)
        return {"node": dev["name"], **result}

    # ── tier 3 ──────────────────────────────────────────────────────────────

    @tool()
    async def console_read(
        lab_path: str, node: str, duration: float = 3
    ) -> dict:
        """Read whatever the console emits for N seconds (boot output, log
        spew, current prompt). Sends nothing.

        Args:
            lab_path: e.g. '/MyLab.unl'.
            node: node name or numeric id.
            duration: seconds to listen (default 3).
        """
        dev = await _find_device(client, platforms, lab_path, node)
        async with TelnetConsole(dev["console"]["host"], dev["console"]["port"]) as con:
            output = await con.read(duration=duration)
        return {"node": dev["name"], "output": output}

    @tool(write=True)
    async def console_write(lab_path: str, node: str, data: str) -> dict:
        """Write raw bytes to the console EXACTLY as given (no newline added).
        For bootloader interrupts, password prompts, breaking a hung session.
        Returns anything the console echoes within 2s.

        Args:
            lab_path: e.g. '/MyLab.unl'.
            node: node name or numeric id.
            data: exact text to send (include your own \\r if needed).
        """
        dev = await _find_device(client, platforms, lab_path, node)
        async with TelnetConsole(dev["console"]["host"], dev["console"]["port"]) as con:
            await con.write(data)
            output = await con.read(duration=2)
        return {"node": dev["name"], "output": output}

    @tool()
    async def console_wait_for(
        lab_path: str,
        node: str,
        pattern: str = "(login:|Login:|>|#|\\$)",
        timeout: float = 0,
    ) -> dict:
        """Wait for a node's console to show signs of life (boot completion).
        Default pattern matches common login/shell prompts; default timeout is
        the template's boot_seconds from platforms.yaml. Nudges the console
        with a newline every 15s to provoke a prompt (nudge is disabled in
        read-only mode — the wait is then purely passive).

        Args:
            lab_path: e.g. '/MyLab.unl'.
            node: node name or numeric id.
            pattern: regex to wait for.
            timeout: seconds (0 = use template boot_seconds).
        """
        dev = await _find_device(client, platforms, lab_path, node)
        effective = timeout or dev["boot_seconds"]
        carry = ""
        async with TelnetConsole(dev["console"]["host"], dev["console"]["port"]) as con:
            remaining = float(effective)
            while remaining > 0:
                slice_t = min(15.0, remaining)
                t0 = time.monotonic()
                # carry the previous slice's tail so a pattern printed across
                # the slice boundary still matches
                result = await con.expect(pattern, timeout=slice_t, initial=carry)
                if result["matched"]:
                    return {"node": dev["name"], "booted": True,
                            "waited_max": effective, **result}
                if result.get("eof"):
                    return {"node": dev["name"], "booted": False, "eof": True,
                            "pattern": pattern, "output": result.get("output", ""),
                            "hint": "console closed mid-wait — node likely "
                                    "stopped/crashed or EVE-NG recycled the "
                                    "console; check eveng_list_nodes"}
                carry = result.get("output", "")[-1024:]
                remaining -= time.monotonic() - t0
                if remaining > 0 and not cfg.read_only:
                    await con.write("\r")
        return {"node": dev["name"], "booted": False, "waited": effective,
                "pattern": pattern, "output": carry,
                "hint": "increase timeout or check console_read for boot errors"}
