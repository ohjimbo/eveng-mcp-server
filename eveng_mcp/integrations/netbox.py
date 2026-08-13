"""NetBox integration — build EVE-NG labs from NetBox, publish, and diff.

The pull direction (NetBox → EVE-NG) is the headline: point at a NetBox
site and instantiate it as a lab. platforms.yaml is the mapping spine —
`netbox_device_types` associates NetBox device-type models with EVE-NG
templates, and `interfaces.naming` maps real interface names to slots.

Publish (EVE-NG → NetBox) is deliberately narrow and guarded: explicit
target site, everything tagged 'eveng-lab', devices only in v1.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from eveng_mcp.client import EvengError
from eveng_mcp.config import _as_bool
from eveng_mcp.inventory import PlatformMap, build_inventory
from eveng_mcp.tools._common import make_tool
from eveng_mcp.tools.topology import build_topology

REQUIRES = ("NETBOX_URL", "NETBOX_TOKEN")
SUMMARY = "Build labs from NetBox sites (preview/build), publish lab devices, diff drift"

_LAB_TAG = "eveng-lab"


class NetboxError(EvengError):
    pass


class NetboxClient:
    def __init__(self, url: str, token: str, verify: bool = True):
        self.base = url.rstrip("/")
        self.verify = verify
        self.headers = {"Authorization": f"Token {token}",
                        "Accept": "application/json"}

    async def get_all(self, path: str, params: dict | None = None) -> list[dict]:
        """GET with pagination, following `next` links (same-origin only —
        the token header must never travel to another host)."""
        results: list[dict] = []
        first = True
        url: str | None = f"{self.base}/api{path}"
        query = {"limit": 200, **(params or {})}
        async with httpx.AsyncClient(headers=self.headers, timeout=30,
                                     verify=self.verify) as c:
            while url:
                if urlsplit(url)[:2] != urlsplit(self.base)[:2]:
                    raise NetboxError(
                        f"refusing to follow off-origin pagination link {url!r}"
                    )
                r = await c.get(url, params=query if first else None)
                first = False
                if r.status_code >= 400:
                    raise NetboxError(f"NetBox {r.status_code} on {path}: {r.text[:200]}")
                data = r.json()
                results.extend(data.get("results", []))
                url = data.get("next")
        return results

    async def post(self, path: str, body: dict) -> dict:
        async with httpx.AsyncClient(headers=self.headers, timeout=30,
                                     verify=self.verify) as c:
            r = await c.post(f"{self.base}/api{path}", json=body)
        if r.status_code >= 400:
            raise NetboxError(f"NetBox POST {path} failed {r.status_code}: {r.text[:300]}")
        return r.json()


def _reverse_type_index(platforms: PlatformMap) -> dict[str, str]:
    """NetBox device-type model (lowercased) → EVE-NG template."""
    index: dict[str, str] = {}
    for tpl in platforms.templates:
        info = platforms.lookup(tpl)
        if not info:
            continue
        for model in info.netbox_device_types:
            index[model.lower()] = tpl
    return index


async def _device_filters(
    nb: NetboxClient, site: str, role: str, tag: str, rack: str
) -> dict:
    params: dict = {"site": site}
    if role:
        params["role"] = role
    if tag:
        params["tag"] = tag
    if rack:
        # /dcim/devices/ has no name-based rack filter (only rack_id, and
        # NetBox silently IGNORES unknown params — a raw rack= would make the
        # filter a no-op and select the whole site). Resolve name → id(s);
        # rack names are only unique per location, so this can be several.
        racks = await nb.get_all("/dcim/racks/", {"site": site, "name": rack})
        if not racks:
            raise NetboxError(f"rack {rack!r} not found in site {site!r}")
        params["rack_id"] = [r["id"] for r in racks]
    return params


async def _fetch_site_view(
    nb: NetboxClient, platforms: PlatformMap, site: str, role: str, tag: str, rack: str
) -> dict:
    """Devices + links + mapping status for a filtered NetBox site slice."""
    if not site:
        raise NetboxError("site is required — never operate on all of NetBox at once")
    devices = await nb.get_all(
        "/dcim/devices/", await _device_filters(nb, site, role, tag, rack)
    )
    if not devices:
        return {"devices": [], "links": [], "unmapped": [],
                "note": f"no devices matched site={site} role={role or '*'} "
                        f"tag={tag or '*'} rack={rack or '*'}"}

    type_index = _reverse_type_index(platforms)
    mapped: list[dict] = []
    unmapped: list[dict] = []
    ids: dict[int, str] = {}
    for d in devices:
        model = ((d.get("device_type") or {}).get("model") or "").strip()
        template = type_index.get(model.lower())
        primary = d.get("primary_ip4") or d.get("primary_ip") or {}
        addr = (primary.get("address") or "").split("/")[0] or None
        entry = {
            "name": d["name"],
            "device_type": model,
            "role": ((d.get("role") or d.get("device_role") or {}).get("slug")),
            "template": template,
            "mgmt_ip": addr,
        }
        ids[d["id"]] = d["name"]
        (mapped if template else unmapped).append(entry)

    # Links: interfaces of selected devices that have a cable; dedupe by cable id.
    links: list[dict] = []
    seen_cables: set[int] = set()
    id_list = list(ids)
    for i in range(0, len(id_list), 50):
        chunk = id_list[i : i + 50]
        interfaces = await nb.get_all(
            "/dcim/interfaces/", {"device_id": chunk, "cabled": "true"}
        )
        for iface in interfaces:
            cable = iface.get("cable")
            cable_id = cable.get("id") if isinstance(cable, dict) else cable
            if not cable_id or cable_id in seen_cables:
                continue
            peers = iface.get("link_peers") or []
            if not peers:
                continue
            peer = peers[0]
            peer_dev = (peer.get("device") or {}).get("name")
            peer_if = peer.get("name")
            if peer_dev not in ids.values():
                continue  # cable leaves the selection — skip, reported via count
            seen_cables.add(cable_id)
            links.append({
                "a": f"{iface['device']['name']}:{iface['name']}",
                "b": f"{peer_dev}:{peer_if}",
            })

    return {"devices": mapped, "unmapped": unmapped, "links": links}


def register(mcp, cfg, client, platforms) -> None:
    tool = make_tool(mcp, cfg)
    # Default TRUE (unlike EVE-NG's self-signed-lab default): NetBox is
    # typically a production source of truth with a real certificate, and
    # this token can have write scope.
    nb = NetboxClient(
        cfg.env("NETBOX_URL"),
        cfg.env("NETBOX_TOKEN"),
        verify=_as_bool(cfg.env("NETBOX_VERIFY_SSL"), True),
    )

    @tool()
    async def netbox_preview_lab(
        site: str, role: str = "", tag: str = "", rack: str = ""
    ) -> dict:
        """DRY RUN of building an EVE-NG lab from a NetBox site: which devices
        map to which templates, which device types are unmapped (add them to
        platforms.yaml netbox_device_types), the links that would be created,
        planned mgmt IPs from IPAM, and a RAM feasibility estimate against
        current EVE-NG headroom. Creates nothing. ALWAYS run before
        netbox_build_lab.

        Args:
            site: NetBox site slug (required).
            role/tag/rack: optional narrowing filters — a real site is usually
                bigger than an EVE-NG host can run.
        """
        view = await _fetch_site_view(nb, platforms, site, role, tag, rack)
        if not view.get("devices") and not view.get("unmapped"):
            return view

        # RAM feasibility: template default RAM × node count vs host free mem.
        ram_needed = 0
        ram_unknown: list[str] = []
        template_ram: dict[str, int] = {}
        for dev in view["devices"]:
            tpl = dev["template"]
            if tpl not in template_ram:
                try:
                    detail = await client.template_detail(tpl)
                    opts = detail.get("options", {}) if isinstance(detail, dict) else {}
                    ram_val = opts.get("ram", {})
                    template_ram[tpl] = int(
                        (ram_val.get("value") if isinstance(ram_val, dict) else ram_val)
                        or 0
                    )
                except EvengError:
                    template_ram[tpl] = 0
            if template_ram[tpl]:
                ram_needed += template_ram[tpl]
            else:
                ram_unknown.append(dev["name"])

        feasibility: dict = {"estimated_ram_mb": ram_needed,
                             "ram_unknown_for": ram_unknown}
        try:
            status = await client.status()
            mem_pct = float(status.get("mem", 0) or 0)
            feasibility["host_mem_used_percent"] = mem_pct
            feasibility["warning"] = (
                "host memory already above 75% — build may OOM"
                if mem_pct > 75 else None
            )
        except EvengError as e:
            feasibility["warning"] = f"EVE-NG unreachable for headroom check: {e}"

        return {
            **view,
            "feasibility": feasibility,
            "buildable": not view["unmapped"],
            "hint": (None if not view["unmapped"] else
                     "unmapped device types — add them under netbox_device_types "
                     "in platforms.yaml (EVENG_PLATFORM_MAP override) or filter "
                     "them out with role/tag/rack"),
        }

    @tool(write=True)
    async def netbox_build_lab(
        lab_path: str,
        site: str,
        role: str = "",
        tag: str = "",
        rack: str = "",
        skip_unmapped: bool = False,
        create_lab: bool = True,
    ) -> dict:
        """Build an EVE-NG lab from a NetBox site slice: devices become nodes
        (template via platforms.yaml), cables become links. Run
        netbox_preview_lab first. Mgmt IPs from IPAM are returned for the
        bootstrap-mgmt-via-console skill — EVE-NG can't set them itself.

        Args:
            lab_path: lab to create/build into, e.g. '/NetBox-HQ.unl'.
            site: NetBox site slug (required).
            role/tag/rack: optional narrowing filters.
            skip_unmapped: build without the unmapped devices instead of
                refusing (their links are dropped too).
            create_lab: create lab_path first (default true).
        """
        view = await _fetch_site_view(nb, platforms, site, role, tag, rack)
        if view.get("unmapped") and not skip_unmapped:
            return {"error": "Unmapped device types — refusing to build a partial "
                             "topology silently. Re-run with skip_unmapped=true or "
                             "map them in platforms.yaml.",
                    "unmapped": view["unmapped"]}
        if not view.get("devices"):
            return {"error": "No mapped devices to build", **view}

        built_names = {d["name"] for d in view["devices"]}
        links = [ln for ln in view["links"]
                 if ln["a"].split(":")[0] in built_names
                 and ln["b"].split(":")[0] in built_names]

        spec = {
            "nodes": [{"name": d["name"], "template": d["template"]}
                      for d in view["devices"]],
            "links": links,
        }
        result = await build_topology(client, platforms, lab_path, spec,
                                      create_lab=create_lab)
        result["mgmt_plan"] = [
            {"name": d["name"], "mgmt_ip": d["mgmt_ip"]}
            for d in view["devices"] if d["mgmt_ip"]
        ]
        result["dropped_unmapped"] = view.get("unmapped", []) if skip_unmapped else []
        return result

    @tool(write=True)
    async def netbox_publish_lab(
        lab_path: str, site: str, device_role: str = "eveng-lab"
    ) -> dict:
        """Publish a lab's devices INTO NetBox (v1: devices only, no cables).
        Guarded: an explicit target site is required, every created object is
        tagged 'eveng-lab', and existing devices are never modified. Device
        types must already exist in NetBox (matched by model, via
        platforms.yaml netbox_device_types).

        Args:
            lab_path: e.g. '/MyLab.unl'.
            site: NetBox site slug to create the devices under (required).
            device_role: NetBox device role slug (default 'eveng-lab'; must
                exist or be creatable).
        """
        if not site:
            return {"error": "site is required — refusing to publish without an "
                             "explicit target"}
        inv = await build_inventory(client, platforms, lab_path=lab_path)

        sites = await nb.get_all("/dcim/sites/", {"slug": site})
        if not sites:
            return {"error": f"NetBox site {site!r} not found"}
        site_id = sites[0]["id"]

        roles = await nb.get_all("/dcim/device-roles/", {"slug": device_role})
        if roles:
            role_id = roles[0]["id"]
        else:
            created = await nb.post("/dcim/device-roles/",
                                    {"name": device_role, "slug": device_role,
                                     "color": "9e9e9e"})
            role_id = created["id"]

        tags = await nb.get_all("/extras/tags/", {"slug": _LAB_TAG})
        if tags:
            tag_id = tags[0]["id"]
        else:
            created = await nb.post("/extras/tags/",
                                    {"name": _LAB_TAG, "slug": _LAB_TAG})
            tag_id = created["id"]

        existing = {d["name"] for d in
                    await nb.get_all("/dcim/devices/", {"site_id": site_id})}

        created_devices, skipped = [], []
        for dev in inv["devices"]:
            if dev["name"] in existing:
                skipped.append({"name": dev["name"], "reason": "already exists in site"})
                continue
            info = platforms.lookup(dev["template"])
            model = (info.netbox_device_types[0]
                     if info and info.netbox_device_types else None)
            if not model:
                skipped.append({"name": dev["name"],
                                "reason": f"no netbox_device_types mapping for "
                                          f"template {dev['template']!r}"})
                continue
            types = await nb.get_all("/dcim/device-types/", {"model": model})
            if not types:
                skipped.append({"name": dev["name"],
                                "reason": f"device type {model!r} not in NetBox"})
                continue
            await nb.post("/dcim/devices/", {
                "name": dev["name"],
                "device_type": types[0]["id"],
                "role": role_id,
                "site": site_id,
                "status": "active",
                "tags": [tag_id],
                "comments": f"Published from EVE-NG lab {lab_path} by eveng-mcp-server",
            })
            created_devices.append(dev["name"])

        return {"created": created_devices, "skipped": skipped,
                "site": site, "tag": _LAB_TAG,
                "note": "devices only — cables/interfaces are a v2 item"}

    @tool()
    async def netbox_diff_lab(
        lab_path: str, site: str, role: str = "", tag: str = "", rack: str = ""
    ) -> dict:
        """Drift check: does the lab still match the NetBox slice it was built
        from? Compares device sets and link sets; read-only.

        Args:
            lab_path: e.g. '/NetBox-HQ.unl'.
            site: NetBox site slug it was built from (plus the same filters).
            role/tag/rack: the filters used at build time.
        """
        view = await _fetch_site_view(nb, platforms, site, role, tag, rack)
        inv = await build_inventory(client, platforms, lab_path=lab_path,
                                    include_links=True)

        nb_devices = {d["name"] for d in view["devices"]} | {
            d["name"] for d in view.get("unmapped", [])
        }
        lab_devices = {d["name"] for d in inv["devices"]}

        def _norm(a: str, b: str) -> tuple[str, str]:
            return (a, b) if a <= b else (b, a)

        nb_links = {_norm(ln["a"], ln["b"]) for ln in view["links"]}
        # Lab side: derive endpoint names from topology entries best-effort.
        lab_links: set[tuple[str, str]] = set()
        for ln in inv.get("links", []):
            src, dst = ln.get("source", ""), ln.get("destination", "")
            src_if = ln.get("source_label") or ln.get("source_interface") or ""
            dst_if = ln.get("destination_label") or ln.get("destination_interface") or ""
            if src and dst and not str(dst).startswith("network"):
                lab_links.add(_norm(f"{src}:{src_if}", f"{dst}:{dst_if}"))

        return {
            "devices": {
                "only_in_netbox": sorted(nb_devices - lab_devices),
                "only_in_lab": sorted(lab_devices - nb_devices),
                "in_both": len(nb_devices & lab_devices),
            },
            "links": {
                "netbox_count": len(nb_links),
                "lab_count": len(lab_links),
                "note": "link-level diff is best-effort — EVE-NG topology "
                        "labels use node display names",
            },
            "in_sync": nb_devices == lab_devices,
        }
