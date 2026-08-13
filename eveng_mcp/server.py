"""eveng-mcp-server — FastMCP app assembly and entry point.

Transport : Streamable HTTP (current MCP standard)
Auth      : Bearer token (MCP_AUTH_TOKEN) via StaticTokenVerifier
Health    : GET /health (unauthenticated)
"""

from __future__ import annotations

import sys

from eveng_mcp import __version__
from eveng_mcp.client import EvengClient
from eveng_mcp.config import Config, ConfigError
from eveng_mcp.inventory import PlatformMap
from eveng_mcp.loader import load_integrations

INSTRUCTIONS = (
    "Tools for driving an EVE-NG network lab emulator: lab/node/topology "
    "lifecycle (create, build declaratively, start/stop/wipe/delete), "
    "vendor-agnostic serial-console access in three tiers (driver-backed "
    "commands & config for mapped platforms, generic expect, raw read/write), "
    "a derived vendor-neutral inventory with per-device reachability, and a "
    "health gate for the host. Optional integrations (present only when "
    "configured): iDRAC host power control, GitLab config backup, "
    "junos-mcp-server device registration, NetBox lab build/publish/diff. "
    "For multi-step work, call skills_list FIRST — vetted runbooks beat "
    "improvising."
)


def build_mcp(cfg: Config):
    """Create the FastMCP instance with every applicable tool registered.
    Returns (mcp, integration_report)."""
    from fastmcp import FastMCP
    from fastmcp.server.auth import StaticTokenVerifier

    auth = StaticTokenVerifier(
        tokens={cfg.auth_token: {"client_id": "eveng-mcp-client", "scopes": []}}
    )
    mcp = FastMCP(name="eveng", auth=auth, instructions=INSTRUCTIONS)

    client = EvengClient(
        host=cfg.eveng_host,
        username=cfg.eveng_user,
        password=cfg.eveng_pass,
        protocol=cfg.eveng_protocol,
        verify_ssl=cfg.eveng_verify_ssl,
    )
    platforms = PlatformMap(cfg.platform_map_path)

    from eveng_mcp.tools import (
        console_tools,
        health,
        inventory_tools,
        labs,
        nodes,
        raw,
        skills_tools,
        topology,
    )

    labs.register(mcp, cfg, client)
    nodes.register(mcp, cfg, client, platforms)
    topology.register(mcp, cfg, client, platforms)
    console_tools.register(mcp, cfg, client, platforms)
    inventory_tools.register(mcp, cfg, client, platforms)
    health.register(mcp, cfg, client)
    raw.register(mcp, cfg, client)

    report = load_integrations(mcp, cfg, client, platforms)
    enabled_names = {e["name"] for e in report["enabled"]}
    skills_tools.register(mcp, cfg, enabled_names)

    @mcp.tool(tags={"read"})
    async def eveng_capabilities() -> dict:
        """What THIS deployment can do: enabled integrations, skipped ones and
        why, read-only flag, and server version. The tool list already
        reflects this — skipped integrations' tools are not registered."""
        return {
            "server_version": __version__,
            "read_only": cfg.read_only,
            "eveng_host": cfg.eveng_host,
            "integrations": report,
        }

    return mcp, report


def create_app(cfg: Config):
    """Starlette ASGI app: /health (unauthenticated) + MCP mount."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse
    from starlette.routing import Mount, Route

    mcp, report = build_mcp(cfg)
    mcp_asgi = mcp.http_app()

    async def health_route(request: Request):
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/health", health_route),
            Mount("/", app=mcp_asgi),
        ],
        lifespan=mcp_asgi.lifespan,
    )
    return app, report


def main() -> None:
    import uvicorn

    try:
        cfg = Config.from_env()
    except ConfigError as e:
        print(f"✗ Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        app, report = create_app(cfg)
    except ConfigError as e:
        print(f"✗ Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"eveng-mcp-server v{__version__}")
    print(f"EVE-NG target : {cfg.eveng_protocol}://{cfg.eveng_host}")
    print(f"MCP endpoint  : http://{cfg.host}:{cfg.port}/mcp  (Streamable HTTP, bearer auth)")
    print(f"Health check  : http://{cfg.host}:{cfg.port}/health")
    if cfg.read_only:
        print("Mode          : READ-ONLY (write tools not registered)")
    enabled = ", ".join(e["name"] for e in report["enabled"]) or "none"
    print(f"Integrations  : {enabled}")
    for s in report["skipped"]:
        print(f"  skipped     : {s['name']} ({s['reason']})")

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
