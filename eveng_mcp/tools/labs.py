"""Lab lifecycle tools — list, create, inspect, delete."""

from __future__ import annotations

from eveng_mcp.client import EvengClient
from eveng_mcp.config import Config
from eveng_mcp.tools._common import make_tool


def register(mcp, cfg: Config, client: EvengClient) -> None:
    tool = make_tool(mcp, cfg)

    @tool()
    async def eveng_list_labs(folder: str = "/") -> dict:
        """List labs and subfolders in an EVE-NG folder.

        Args:
            folder: folder path like '/' or '/Demos'. Use eveng_inventory for
                a flattened view of every lab with device detail.
        """
        return await client.list_folder(folder)

    @tool()
    async def eveng_get_lab(lab_path: str) -> dict:
        """Get lab metadata (name, description, author, id).

        Args:
            lab_path: e.g. '/MyLab.unl' or '/Demos/Fabric.unl'.
        """
        return await client.get_lab(lab_path)

    @tool()
    async def eveng_list_templates() -> dict:
        """List node templates available on this EVE-NG server (template key →
        display name). Use eveng_template_detail for images/options of one."""
        return await client.list_templates()

    @tool()
    async def eveng_template_detail(template: str) -> dict:
        """Get one template's options: available images, default CPU/RAM/
        ethernet counts, console type. Call BEFORE eveng_add_node when unsure
        which image names exist on this server.

        Args:
            template: template key, e.g. 'vjunosswitch', 'veos', 'iol'.
        """
        return await client.template_detail(template)

    @tool(write=True)
    async def eveng_create_lab(
        path: str, name: str, description: str = "", author: str = ""
    ) -> dict:
        """Create a new lab.

        Args:
            path: folder to create it in, e.g. '/'.
            name: lab name WITHOUT .unl extension.
            description: optional lab description.
            author: optional author string.
        """
        result = await client.create_lab(path, name, description, author)
        lab_path = (path.rstrip("/") or "") + f"/{name}.unl"
        return {"created": lab_path, "response": result}

    @tool(write=True, destructive=True)
    async def eveng_delete_lab(lab_path: str) -> dict:
        """Delete a lab permanently, including its node configs. Stop and
        consider backing up first (see the safe-lab-teardown skill).

        Args:
            lab_path: e.g. '/MyLab.unl'.
        """
        result = await client.delete_lab(lab_path)
        return {"deleted": lab_path, "response": result}
