"""Raw API escape hatch — anything the typed tools don't cover."""

from __future__ import annotations

from eveng_mcp.client import EvengClient
from eveng_mcp.config import Config
from eveng_mcp.tools._common import make_tool


def register(mcp, cfg: Config, client: EvengClient) -> None:
    tool = make_tool(mcp, cfg)

    @tool(write=True)
    async def eveng_api(method: str, path: str, body: dict | None = None) -> dict:
        """Raw EVE-NG API request — the escape hatch for endpoints without a
        dedicated tool. Prefer the typed tools when one exists.

        Args:
            method: GET / POST / PUT / DELETE.
            path: API path after /api, e.g. '/labs/MyLab.unl/nodes'.
            body: JSON body for POST/PUT.
        """
        payload = await client.request(method, path, body)
        return payload if isinstance(payload, dict) else {"response": payload}
