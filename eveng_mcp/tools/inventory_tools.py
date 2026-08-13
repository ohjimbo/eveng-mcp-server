"""Inventory tool — the derived, vendor-neutral view of what's in the labs."""

from __future__ import annotations

from eveng_mcp.client import EvengClient
from eveng_mcp.config import Config
from eveng_mcp.inventory import PlatformMap, build_inventory
from eveng_mcp.tools._common import make_tool


def register(mcp, cfg: Config, client: EvengClient, platforms: PlatformMap) -> None:
    tool = make_tool(mcp, cfg)

    @tool()
    async def eveng_inventory(
        lab_path: str = "",
        running_only: bool = False,
        vendor: str = "",
        include_links: bool = False,
    ) -> dict:
        """Vendor-neutral inventory of lab devices, derived live from the
        EVE-NG API + platforms.yaml (nothing stored). Each device carries
        vendor/os_family, state, console endpoint + tier, boot_seconds, and
        reachable_via ('console' and/or 'mgmt') so consumers know which path
        works right now. Unknown templates are listed in unmapped_templates —
        add them to platforms.yaml rather than ignoring them.

        Args:
            lab_path: one lab like '/MyLab.unl' (empty = every lab on the
                server; slower).
            running_only: only running devices.
            vendor: filter by vendor slug (juniper/arista/cisco/...).
            include_links: include the connection map (bigger payload).
        """
        return await build_inventory(
            client,
            platforms,
            lab_path=lab_path or None,
            running_only=running_only,
            vendor=vendor or None,
            include_links=include_links,
        )
