"""Node lifecycle tools — add, inspect, start/stop, wipe, delete."""

from __future__ import annotations

from eveng_mcp.client import EvengClient
from eveng_mcp.config import Config
from eveng_mcp.inventory import PlatformMap
from eveng_mcp.tools._common import make_tool


def register(mcp, cfg: Config, client: EvengClient, platforms: PlatformMap) -> None:
    tool = make_tool(mcp, cfg)

    @tool()
    async def eveng_list_nodes(lab_path: str) -> dict:
        """List all nodes in a lab with raw EVE-NG detail (status, template,
        console URL, resources). For vendor/OS identity and reachability use
        eveng_inventory instead.

        Args:
            lab_path: e.g. '/MyLab.unl'.
        """
        return {"nodes": await client.get_nodes(lab_path)}

    @tool()
    async def eveng_node_detail(lab_path: str, node_id: int) -> dict:
        """Get one node's full detail plus its current interface connections.

        Args:
            lab_path: e.g. '/MyLab.unl'.
            node_id: numeric node id (see eveng_list_nodes).
        """
        node = await client.get_node(lab_path, node_id)
        interfaces = await client.get_interfaces(lab_path, node_id)
        return {"node": node, "interfaces": interfaces}

    @tool(write=True)
    async def eveng_add_node(
        lab_path: str,
        template: str,
        name: str,
        image: str = "",
        left: int = 100,
        top: int = 100,
        ethernet: int = 0,
        ram: int = 0,
        cpu: int = 0,
    ) -> dict:
        """Add a single node to a lab. For multi-node builds with links use
        eveng_build_topology — it is one call instead of dozens.

        Args:
            lab_path: e.g. '/MyLab.unl'.
            template: template key, e.g. 'vjunosswitch' (eveng_list_templates).
            name: node display name.
            image: specific image (default: template default; check
                eveng_template_detail).
            left/top: canvas position in pixels.
            ethernet: interface count (0 = template default).
            ram: MB (0 = template default). cpu: vCPUs (0 = template default).
        """
        info = platforms.lookup(template)
        spec: dict = {
            "template": template,
            "name": name,
            "left": left,
            "top": top,
            "type": info.eve_type if info else "qemu",
        }
        if image:
            spec["image"] = image
        if ethernet:
            spec["ethernet"] = ethernet
        if ram:
            spec["ram"] = ram
        if cpu:
            spec["cpu"] = cpu
        result = await client.add_node(lab_path, spec)
        return {"added": name, "response": result}

    @tool(write=True)
    async def eveng_start_nodes(lab_path: str, node_id: int | None = None) -> dict:
        """Start all nodes in a lab, or one node.

        Args:
            lab_path: e.g. '/MyLab.unl'.
            node_id: start only this node (default: all).
        """
        result = await client.node_action(lab_path, "start", node_id)
        return {"started": node_id if node_id is not None else "all", "response": result}

    @tool(write=True)
    async def eveng_stop_nodes(lab_path: str, node_id: int | None = None) -> dict:
        """Stop all nodes in a lab, or one node. This is a hard power-off —
        for a graceful shutdown of network OSes use the safe-lab-teardown
        skill (console halt first).

        Args:
            lab_path: e.g. '/MyLab.unl'.
            node_id: stop only this node (default: all).
        """
        result = await client.node_action(lab_path, "stop", node_id)
        return {"stopped": node_id if node_id is not None else "all", "response": result}

    @tool(write=True, destructive=True)
    async def eveng_wipe_nodes(lab_path: str, node_id: int | None = None) -> dict:
        """Wipe node state back to factory/pristine (deletes the node's saved
        startup config and disk overlay). The single most useful reset for a
        repeatable demo. Nodes must be stopped first.

        Args:
            lab_path: e.g. '/MyLab.unl'.
            node_id: wipe only this node (default: all).
        """
        result = await client.node_action(lab_path, "wipe", node_id)
        return {"wiped": node_id if node_id is not None else "all", "response": result}

    @tool(write=True, destructive=True)
    async def eveng_delete_node(lab_path: str, node_id: int) -> dict:
        """Remove a node from a lab permanently.

        Args:
            lab_path: e.g. '/MyLab.unl'.
            node_id: numeric node id.
        """
        result = await client.delete_node(lab_path, node_id)
        return {"deleted": node_id, "response": result}
