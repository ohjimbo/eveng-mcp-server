"""Health gate — check the EVE-NG host BEFORE building a 20-node lab on it."""

from __future__ import annotations

from eveng_mcp.client import EvengClient, EvengError
from eveng_mcp.config import Config
from eveng_mcp.tools._common import make_tool


def _verdict(cpu: float, mem: float) -> tuple[str, str]:
    if cpu > 90 or mem > 90:
        return "CRITICAL", "🔴"
    if cpu > 75 or mem > 75:
        return "DEGRADED", "🟡"
    return "GO", "🟢"


def register(mcp, cfg: Config, client: EvengClient) -> None:
    tool = make_tool(mcp, cfg)

    @tool()
    async def eveng_health(count_labs: bool = False) -> dict:
        """Health gate for the EVE-NG server: reachability, version, CPU/
        memory/disk headroom, QEMU count, and a GO/DEGRADED/CRITICAL verdict.
        Run this before building or starting large labs.

        Args:
            count_labs: also walk every lab and count running nodes (slower).
        """
        try:
            status = await client.status()
        except EvengError as e:
            return {
                "verdict": "CRITICAL",
                "emoji": "🔴",
                "reachable": False,
                "error": str(e),
                "hint": "EVE-NG API unreachable — host down, still booting, or "
                        "wrong EVENG_HOST/credentials",
            }

        cpu = float(status.get("cpu", 0) or 0)
        mem = float(status.get("mem", 0) or 0)
        disk = float(status.get("disk", 0) or 0)
        verdict, emoji = _verdict(cpu, mem)

        result = {
            "verdict": verdict,
            "emoji": emoji,
            "reachable": True,
            "version": status.get("version"),
            "qemu_version": status.get("qemu_version"),
            "cpu_percent": cpu,
            "mem_percent": mem,
            "disk_percent": disk,
            "running_qemu_nodes": status.get("qemu", 0),
        }
        if verdict != "GO":
            result["hint"] = (
                "Host is under pressure — starting more nodes may OOM or crawl. "
                "Stop unused labs first."
            )

        if count_labs:
            labs: dict[str, int] = {}
            for lp in await client.walk_labs():
                try:
                    nodes = await client.get_nodes(lp)
                except EvengError:
                    continue
                running = sum(1 for n in nodes.values() if n.get("status") == 2)
                if running:
                    labs[lp] = running
            result["labs_with_running_nodes"] = labs

        return result
