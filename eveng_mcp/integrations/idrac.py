"""iDRAC integration — Redfish power control for the EVE-NG bare-metal host.

Site-specific by nature (most public users run EVE-NG in a VM); enabled only
when IDRAC_* is configured.
"""

from __future__ import annotations

import httpx

from eveng_mcp.config import _as_bool
from eveng_mcp.tools._common import make_tool

REQUIRES = ("IDRAC_HOST", "IDRAC_USER", "IDRAC_PASS")
SUMMARY = "Dell iDRAC (Redfish) power control for the EVE-NG bare-metal host"

_SYSTEM = "/redfish/v1/Systems/System.Embedded.1"
_RESET = f"{_SYSTEM}/Actions/ComputerSystem.Reset"


def register(mcp, cfg, client, platforms) -> None:
    tool = make_tool(mcp, cfg)
    host = cfg.env("IDRAC_HOST")
    auth = (cfg.env("IDRAC_USER"), cfg.env("IDRAC_PASS"))
    # Default false: BMC self-signed certs are the norm. Set IDRAC_VERIFY_SSL
    # =true if you've provisioned a real certificate.
    verify = _as_bool(cfg.env("IDRAC_VERIFY_SSL"), False)

    async def _redfish(method: str, path: str, body: dict | None = None) -> dict:
        async with httpx.AsyncClient(auth=auth, verify=verify, timeout=25) as c:
            r = await c.request(method, f"https://{host}{path}", json=body)
        if r.status_code >= 400:
            return {"error": f"iDRAC {r.status_code}: {r.text[:300]}"}
        try:
            return r.json() if r.content else {"status_code": r.status_code}
        except ValueError:
            return {"status_code": r.status_code}

    @tool()
    async def eveng_host_power_status() -> dict:
        """Power state of the EVE-NG bare-metal host via iDRAC (On/Off), with
        model info. Use before eveng_health when the API is unreachable — it
        tells you whether the host is even powered."""
        data = await _redfish("GET", _SYSTEM)
        if "error" in data:
            return data
        return {
            "power_state": data.get("PowerState", "Unknown"),
            "model": data.get("Model"),
            "host": host,
        }

    @tool(write=True)
    async def eveng_host_power_on() -> dict:
        """Power on the EVE-NG bare-metal host via iDRAC. Boot takes several
        minutes; poll eveng_health until it answers."""
        result = await _redfish("POST", _RESET, {"ResetType": "On"})
        return {"requested": "On", "host": host, "response": result}

    @tool(write=True, destructive=True)
    async def eveng_host_power_off(force: bool = False) -> dict:
        """Power off the EVE-NG bare-metal host via iDRAC. Default is a
        graceful OS shutdown; force=True cuts power immediately. Run the
        safe-lab-teardown skill FIRST so running nodes aren't corrupted.

        Args:
            force: hard power-off instead of graceful shutdown.
        """
        reset = "ForceOff" if force else "GracefulShutdown"
        result = await _redfish("POST", _RESET, {"ResetType": reset})
        return {"requested": reset, "host": host, "response": result}
