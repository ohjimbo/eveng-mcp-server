"""The single async EVE-NG REST client used by every tool and integration.

Replaces the two divergent implementations that grew in mcp-homelab
(tools/eveng.py and tools/jmcp_bridge.py). Cookie login is cached; a 401/412
triggers exactly one re-login + retry, then fails loudly.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import httpx


class EvengError(Exception):
    """Base error for EVE-NG operations."""


class EvengAuthError(EvengError):
    """Login failed or re-authentication was rejected."""


class EvengApiError(EvengError):
    """The EVE-NG API returned an error status."""

    def __init__(self, status_code: int, message: str, body: Any = None):
        super().__init__(f"EVE-NG API {status_code}: {message}")
        self.status_code = status_code
        self.body = body


def _quote_lab(lab_path: str) -> str:
    """Percent-encode a lab path (spaces are common: '/Campus Fabric.unl')."""
    if not lab_path.startswith("/"):
        lab_path = "/" + lab_path
    return quote(lab_path, safe="/")


class EvengClient:
    COOKIE_TTL = 3600

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        protocol: str = "https",
        verify_ssl: bool = False,
        timeout: float = 30,
    ):
        self.host = host
        self.username = username
        self.password = password
        self.protocol = protocol
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._cookies: dict[str, str] = {}
        self._cookies_expire: float = 0.0

    @property
    def base_url(self) -> str:
        return f"{self.protocol}://{self.host}/api"

    # ── auth ────────────────────────────────────────────────────────────────

    async def login(self, force: bool = False) -> None:
        if not force and self._cookies and time.time() < self._cookies_expire:
            return
        async with httpx.AsyncClient(timeout=15, verify=self.verify_ssl) as client:
            r = await client.post(
                f"{self.base_url}/auth/login",
                json={"username": self.username, "password": self.password, "html5": -1},
            )
        if r.status_code != 200:
            raise EvengAuthError(
                f"EVE-NG login failed ({r.status_code}) for {self.username}@{self.host}"
            )
        self._cookies = dict(r.cookies)
        self._cookies_expire = time.time() + self.COOKIE_TTL

    # ── transport ───────────────────────────────────────────────────────────

    async def _do(self, method: str, url: str, body: dict | None) -> httpx.Response:
        async with httpx.AsyncClient(
            timeout=self.timeout, verify=self.verify_ssl, cookies=self._cookies
        ) as client:
            if method in ("GET", "DELETE"):
                return await client.request(method, url)
            return await client.request(method, url, json=body or {})

    async def request(self, method: str, path: str, body: dict | None = None) -> Any:
        """Make an authenticated request; return the parsed response payload.

        Raises EvengApiError on HTTP >= 400 and EvengAuthError if the session
        can't be (re-)established.
        """
        method = method.upper()
        if method not in ("GET", "POST", "PUT", "DELETE"):
            raise EvengError(f"Unsupported method: {method}")
        await self.login()
        url = f"{self.base_url}{path}"
        r = await self._do(method, url, body)
        if r.status_code in (401, 412):
            await self.login(force=True)
            r = await self._do(method, url, body)
            if r.status_code in (401, 412):
                raise EvengAuthError(
                    "EVE-NG authentication failed after retry — check credentials"
                )
        try:
            payload: Any = r.json()
        except ValueError:
            payload = r.text
        if r.status_code >= 400:
            msg = payload.get("message") if isinstance(payload, dict) else str(payload)[:200]
            raise EvengApiError(r.status_code, msg or "request failed", payload)
        return payload

    async def data(self, method: str, path: str, body: dict | None = None) -> Any:
        """Like request() but unwraps EVE-NG's {code, status, data} envelope."""
        payload = await self.request(method, path, body)
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    # ── server ──────────────────────────────────────────────────────────────

    async def status(self) -> dict:
        return await self.data("GET", "/status")

    async def list_templates(self) -> dict:
        return await self.data("GET", "/list/templates/")

    async def template_detail(self, template: str) -> dict:
        return await self.data("GET", f"/list/templates/{quote(template, safe='')}")

    # ── labs / folders ──────────────────────────────────────────────────────

    async def list_folder(self, folder: str = "/") -> dict:
        if not folder.startswith("/"):
            folder = "/" + folder
        return await self.data("GET", f"/folders{quote(folder, safe='/')}")

    async def walk_labs(self) -> list[str]:
        """Return every lab path on the server, walking all folders."""
        labs: list[str] = []
        pending = ["/"]
        seen: set[str] = set()
        while pending:
            folder = pending.pop()
            if folder in seen:
                continue
            seen.add(folder)
            listing = await self.list_folder(folder)
            for lab in listing.get("labs", []) or []:
                path = lab.get("path") or lab.get("file")
                if path:
                    labs.append(path if path.startswith("/") else f"/{path}")
            for sub in listing.get("folders", []) or []:
                name = sub.get("name", "")
                path = sub.get("path", "")
                if name in ("..", ".") or not path:
                    continue
                pending.append(path)
        return sorted(set(labs))

    async def get_lab(self, lab_path: str) -> dict:
        return await self.data("GET", f"/labs{_quote_lab(lab_path)}")

    async def create_lab(
        self,
        path: str,
        name: str,
        description: str = "",
        author: str = "",
        version: str = "1",
    ) -> Any:
        body = {
            "path": path,
            "name": name,
            "version": version,
            "description": description,
            "author": author,
        }
        return await self.request("POST", "/labs", body)

    async def delete_lab(self, lab_path: str) -> Any:
        return await self.request("DELETE", f"/labs{_quote_lab(lab_path)}")

    # ── nodes ───────────────────────────────────────────────────────────────

    async def get_nodes(self, lab_path: str) -> dict[str, dict]:
        data = await self.data("GET", f"/labs{_quote_lab(lab_path)}/nodes")
        return data if isinstance(data, dict) else {}

    async def get_node(self, lab_path: str, node_id: int) -> dict:
        return await self.data("GET", f"/labs{_quote_lab(lab_path)}/nodes/{node_id}")

    async def add_node(self, lab_path: str, spec: dict) -> Any:
        return await self.request("POST", f"/labs{_quote_lab(lab_path)}/nodes", spec)

    async def delete_node(self, lab_path: str, node_id: int) -> Any:
        return await self.request("DELETE", f"/labs{_quote_lab(lab_path)}/nodes/{node_id}")

    async def node_action(self, lab_path: str, action: str, node_id: int | None = None) -> Any:
        """start / stop / wipe — one node, or all nodes when node_id is None."""
        if action not in ("start", "stop", "wipe"):
            raise EvengError(f"Unknown node action: {action}")
        base = f"/labs{_quote_lab(lab_path)}/nodes"
        path = f"{base}/{node_id}/{action}" if node_id is not None else f"{base}/{action}"
        return await self.request("GET", path)

    async def get_interfaces(self, lab_path: str, node_id: int) -> dict:
        return await self.data("GET", f"/labs{_quote_lab(lab_path)}/nodes/{node_id}/interfaces")

    async def connect_interface(
        self, lab_path: str, node_id: int, slot: int, network_id: int
    ) -> Any:
        # EVE-NG disconnects an interface ONLY on an empty-string value
        # (strict !== '' check in Lab::connectNode); any non-empty value is
        # validated against existing networks, so "0" would 400. We map the
        # tool-facing "network_id 0 = disconnect" sentinel to "" here.
        value = "" if network_id == 0 else str(network_id)
        body = {str(slot): value}
        return await self.request(
            "PUT", f"/labs{_quote_lab(lab_path)}/nodes/{node_id}/interfaces", body
        )

    # ── networks / topology ─────────────────────────────────────────────────

    async def get_networks(self, lab_path: str) -> dict[str, dict]:
        data = await self.data("GET", f"/labs{_quote_lab(lab_path)}/networks")
        return data if isinstance(data, dict) else {}

    async def add_network(
        self,
        lab_path: str,
        name: str,
        network_type: str = "bridge",
        left: int = 0,
        top: int = 0,
        visibility: int = 1,
    ) -> Any:
        body = {
            "name": name,
            "type": network_type,
            "left": left,
            "top": top,
            "visibility": visibility,
        }
        return await self.request("POST", f"/labs{_quote_lab(lab_path)}/networks", body)

    async def update_network(self, lab_path: str, network_id: int, body: dict) -> Any:
        return await self.request(
            "PUT", f"/labs{_quote_lab(lab_path)}/networks/{network_id}", body
        )

    async def delete_network(self, lab_path: str, network_id: int) -> Any:
        return await self.request(
            "DELETE", f"/labs{_quote_lab(lab_path)}/networks/{network_id}"
        )

    async def get_topology(self, lab_path: str) -> Any:
        return await self.data("GET", f"/labs{_quote_lab(lab_path)}/topology")
