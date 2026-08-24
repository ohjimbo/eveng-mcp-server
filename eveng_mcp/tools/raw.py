"""Raw API escape hatch — anything the typed tools don't cover.

Gated by EVENG_RAW_API because an unrestricted passthrough hands the agent
the whole EVE-NG API surface:
  read (default) — GET only; discovery and inspection, no mutations
  full           — any method (write-tagged, so absent in read-only mode)
  off            — not registered at all
"""

from __future__ import annotations

from eveng_mcp.client import EvengClient
from eveng_mcp.config import Config, ConfigError
from eveng_mcp.tools._common import make_tool

_MODES = ("off", "read", "full")


def raw_api_mode(cfg: Config) -> str:
    mode = (cfg.env("EVENG_RAW_API", "read") or "read").strip().lower()
    if mode not in _MODES:
        raise ConfigError(
            f"EVENG_RAW_API must be one of {', '.join(_MODES)} — got {mode!r}"
        )
    return mode


def register(mcp, cfg: Config, client: EvengClient) -> None:
    mode = raw_api_mode(cfg)
    if mode == "off":
        return
    tool = make_tool(mcp, cfg)

    if mode == "read":

        @tool()
        async def eveng_api(method: str, path: str, body: dict | None = None) -> dict:
            """Raw EVE-NG API request, READ-ONLY (GET) — the escape hatch for
            endpoints without a dedicated tool. Prefer the typed tools when
            one exists. This deployment runs EVENG_RAW_API=read; set it to
            'full' to allow mutating methods.

            Args:
                method: GET only in this mode.
                path: API path after /api, e.g. '/labs/MyLab.unl/nodes'.
                body: ignored for GET.
            """
            if method.upper() != "GET":
                return {
                    "error": f"Raw API is read-only in this deployment "
                             f"({method.upper()} rejected) — set "
                             "EVENG_RAW_API=full to allow mutating methods",
                }
            payload = await client.request("GET", path)
            return payload if isinstance(payload, dict) else {"response": payload}

    else:  # full

        @tool(write=True)
        async def eveng_api(method: str, path: str, body: dict | None = None) -> dict:
            """Raw EVE-NG API request — the escape hatch for endpoints without
            a dedicated tool. Prefer the typed tools when one exists.

            Args:
                method: GET / POST / PUT / DELETE.
                path: API path after /api, e.g. '/labs/MyLab.unl/nodes'.
                body: JSON body for POST/PUT.
            """
            payload = await client.request(method, path, body)
            return payload if isinstance(payload, dict) else {"response": payload}
