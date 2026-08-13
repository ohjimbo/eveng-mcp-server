"""junos-mcp-server integration — publish lab inventory downstream.

Registers running Junos lab devices in a junos-mcp-server instance's
devices.json, then hot-reloads it over its MCP endpoint — cross-MCP
composition: this server acts as an MCP *client* of another MCP server.

Rewritten from mcp-homelab's jmcp_bridge with the known defects fixed:
  - a failed read of devices.json ABORTS the write (never wipe-on-error)
  - file transfer uses the Docker archive API (tar over the socket) — no
    shell, no string interpolation of credentials
  - device selection is os_family == junos from the shared inventory, not a
    hardcoded template tuple
  - no hardcoded credential fallback

Requires junos-mcp-server as a container on the SAME Docker host, with
/var/run/docker.sock mounted into this server's container.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import PurePosixPath

import httpx

from eveng_mcp.client import EvengError
from eveng_mcp.inventory import build_inventory
from eveng_mcp.tools._common import make_tool

REQUIRES = ("JMCP_URL",)
SUMMARY = "Register EVE-NG Junos lab devices with a junos-mcp-server instance"


class JmcpError(EvengError):
    pass


class DockerFileTransport:
    """Read/write one file inside a container via the Docker archive API.

    GET  /containers/{c}/archive?path=...  → tar stream (404 = file absent)
    PUT  /containers/{c}/archive?path=DIR  ← tar stream

    Content travels as tar bytes — nothing is ever interpolated into a shell.
    """

    def __init__(self, container: str, file_path: str, socket_path: str):
        self.container = container
        self.file_path = PurePosixPath(file_path)
        self.socket_path = socket_path

    def _client(self) -> httpx.AsyncClient:
        transport = httpx.AsyncHTTPTransport(uds=self.socket_path)
        return httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=30)

    async def read(self) -> str | None:
        """File content, or None if the file doesn't exist. Raises JmcpError
        on any transport/daemon error — caller must NOT treat that as empty."""
        async with self._client() as c:
            try:
                r = await c.get(
                    f"/containers/{self.container}/archive",
                    params={"path": str(self.file_path)},
                )
            except httpx.HTTPError as e:
                raise JmcpError(f"Docker socket error reading devices.json: {e}") from e
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise JmcpError(
                f"Docker archive read failed ({r.status_code}): {r.text[:200]}"
            )
        with tarfile.open(fileobj=io.BytesIO(r.content)) as tar:
            for member in tar.getmembers():
                if member.isfile():
                    f = tar.extractfile(member)
                    if f:
                        return f.read().decode("utf-8", errors="replace")
        raise JmcpError("Docker archive read returned no file member")

    async def write(self, content: str) -> None:
        buf = io.BytesIO()
        data = content.encode("utf-8")
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=self.file_path.name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
        async with self._client() as c:
            try:
                r = await c.put(
                    f"/containers/{self.container}/archive",
                    params={"path": str(self.file_path.parent)},
                    content=buf.getvalue(),
                    headers={"Content-Type": "application/x-tar"},
                )
            except httpx.HTTPError as e:
                raise JmcpError(f"Docker socket error writing devices.json: {e}") from e
        if r.status_code != 200:
            raise JmcpError(
                f"Docker archive write failed ({r.status_code}): {r.text[:200]}"
            )


async def _trigger_reload(jmcp_url: str, devices_path: str) -> str:
    """Hot-reload junos-mcp-server via its own MCP endpoint (Streamable HTTP
    handshake: initialize → notifications/initialized → tools/call)."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.post(
                f"{jmcp_url}/mcp/",
                headers={"Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                 "clientInfo": {"name": "eveng-mcp-server",
                                                "version": "0.1"}}},
            )
            sid = resp.headers.get("mcp-session-id", "")
            if not sid:
                return "reload skipped: no MCP session id from junos-mcp-server"
            hdrs = {"Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "Mcp-Session-Id": sid}
            await c.post(f"{jmcp_url}/mcp/", headers=hdrs,
                         json={"jsonrpc": "2.0",
                               "method": "notifications/initialized"})
            r2 = await c.post(f"{jmcp_url}/mcp/", headers=hdrs,
                              json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                    "params": {"name": "reload_devices",
                                               "arguments": {"file_name": devices_path}}})
            return f"reload triggered: HTTP {r2.status_code}"
    except httpx.HTTPError as e:
        return f"reload failed: {e}"


def register(mcp, cfg, client, platforms) -> None:
    tool = make_tool(mcp, cfg)
    jmcp_url = cfg.env("JMCP_URL").rstrip("/")
    devices_path = cfg.env("JMCP_DEVICES_PATH", "/app/config/devices.json")
    transport = DockerFileTransport(
        container=cfg.env("JMCP_CONTAINER", "junos-mcp-server"),
        file_path=devices_path,
        socket_path=cfg.env("DOCKER_SOCKET", "/var/run/docker.sock"),
    )

    def _device_creds(username: str, password: str) -> tuple[str, str]:
        user = username or cfg.env("JMCP_DEVICE_USER")
        pwd = password or cfg.env("JMCP_DEVICE_PASS")
        if not user or not pwd:
            raise JmcpError(
                "Device SSH credentials required — pass username/password or "
                "set JMCP_DEVICE_USER / JMCP_DEVICE_PASS"
            )
        return user, pwd

    async def _read_devices() -> dict:
        raw = await transport.read()  # raises on transport error — never {} on failure
        if raw is None or not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise JmcpError(
                f"devices.json exists but is not valid JSON ({e}) — refusing to "
                "overwrite; fix or clear it manually"
            ) from e
        if not isinstance(parsed, dict):
            raise JmcpError("devices.json is not a JSON object — refusing to overwrite")
        return parsed

    @tool(write=True)
    async def eveng_register_junos_devices(
        lab_path: str, username: str = "", password: str = ""
    ) -> dict:
        """Register a lab's running Junos devices with junos-mcp-server:
        merges them into its devices.json (as lab-<name>) and hot-reloads it.
        Devices need a management IP — bootstrap one via console first (see
        the bootstrap-mgmt-via-console skill). Non-Junos and unreachable
        nodes are reported, not silently dropped.

        Args:
            lab_path: e.g. '/MyLab.unl'.
            username/password: SSH creds junos-mcp-server will use (default:
                JMCP_DEVICE_USER/PASS env).
        """
        user, pwd = _device_creds(username, password)
        inv = await build_inventory(client, platforms, lab_path=lab_path,
                                    running_only=True)
        devices = await _read_devices()

        registered, skipped = [], []
        for dev in inv["devices"]:
            if dev["os_family"] != "junos":
                skipped.append({"node": dev["name"],
                                "reason": f"os_family={dev['os_family']} (not junos)"})
                continue
            if not dev["mgmt"]:
                skipped.append({"node": dev["name"],
                                "reason": "no management IP — bootstrap via console first"})
                continue
            key = f"lab-{dev['name']}"
            devices[key] = {
                "ip": dev["mgmt"]["ip"],
                "port": dev["mgmt"]["port"],
                "username": user,
                "auth": {"type": "password", "password": pwd},
            }
            registered.append({"name": key, "ip": dev["mgmt"]["ip"],
                               "template": dev["template"]})

        if not registered:
            return {"registered": [], "skipped": skipped,
                    "note": "nothing to register — devices.json untouched"}

        await transport.write(json.dumps(devices, indent=2))
        reload_status = await _trigger_reload(jmcp_url, devices_path)
        return {"registered": registered, "skipped": skipped,
                "total_devices_in_file": len(devices), "reload": reload_status}

    @tool(write=True)
    async def eveng_unregister_junos_devices(
        names: list[str] | None = None, clear_all: bool = False
    ) -> dict:
        """Remove devices from junos-mcp-server's devices.json and hot-reload.

        Args:
            names: device names to remove ('R1' or 'lab-R1' both work).
            clear_all: remove ALL registered devices instead.
        """
        if not names and not clear_all:
            return {"error": "Pass names=[...] or clear_all=true"}
        devices = await _read_devices()
        removed: list[str] = []
        if clear_all:
            removed = sorted(devices)
            devices = {}
        else:
            for name in names or []:
                for key in (name, f"lab-{name}"):
                    if key in devices:
                        del devices[key]
                        removed.append(key)
                        break
        await transport.write(json.dumps(devices, indent=2))
        reload_status = await _trigger_reload(jmcp_url, devices_path)
        return {"removed": removed, "remaining": len(devices),
                "reload": reload_status}

    @tool()
    async def jmcp_list_registered_devices() -> dict:
        """List devices currently registered with junos-mcp-server (name, ip,
        port, username — passwords are not returned)."""
        devices = await _read_devices()
        return {"devices": [
            {"name": k, "ip": v.get("ip"), "port": v.get("port", 22),
             "username": v.get("username")}
            for k, v in sorted(devices.items())
        ], "count": len(devices)}
