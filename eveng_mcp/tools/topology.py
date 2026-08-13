"""Topology tools — networks, links, and the declarative bulk builder.

``build_topology`` is a plain async function (not just a tool) so the NetBox
integration can reuse it: NetBox → spec → build_topology.
"""

from __future__ import annotations

import contextlib
import re

from eveng_mcp.client import EvengApiError, EvengClient, EvengError
from eveng_mcp.config import Config
from eveng_mcp.inventory import PlatformMap
from eveng_mcp.tools._common import make_tool

_ENDPOINT_RE = re.compile(r"^(?P<node>[^:]+):(?P<iface>.+)$")


def _new_id(response) -> int | None:
    """Extract the created object id from an EVE-NG POST response."""
    if isinstance(response, dict):
        data = response.get("data")
        if isinstance(data, dict) and "id" in data:
            return int(data["id"])
    return None


def _parse_endpoint(ref: str) -> tuple[str, str]:
    m = _ENDPOINT_RE.match(ref.strip())
    if not m:
        raise EvengError(
            f"Bad endpoint {ref!r} — expected 'node:interface' like 'R1:0' or "
            "'spine1:et-0/0/0'"
        )
    return m.group("node"), m.group("iface")


async def build_topology(
    client: EvengClient,
    platforms: PlatformMap,
    lab_path: str,
    spec: dict,
    create_lab: bool = False,
) -> dict:
    """Build a topology from a declarative spec. Fail-fast: on the first
    failure, stop and report what was done, what failed, and what was never
    attempted — no silent partial builds.

    Spec:
      nodes:    [{name, template, image?, left?, top?, ram?, cpu?, ethernet?}]
      networks: [{name, type?}]                       # named/shared segments
      links:    [{a: "node:iface", b: "node:iface"}   # p2p (auto bridge)
                 | {a: "node:iface", network: "name"}] # attach to named net
    """
    nodes_spec: list[dict] = spec.get("nodes", []) or []
    networks_spec: list[dict] = spec.get("networks", []) or []
    links_spec: list[dict] = spec.get("links", []) or []

    done: list[str] = []
    failed: str | None = None
    not_attempted: list[str] = []

    # ── validate up front ───────────────────────────────────────────────────
    names = [n.get("name", "") for n in nodes_spec]
    if len(set(names)) != len(names) or "" in names:
        return {"error": "Node names must be present and unique", "nodes": names}
    name_set = set(names)
    net_names = {n.get("name", "") for n in networks_spec}
    templates = {n["name"]: n.get("template", "") for n in nodes_spec}
    no_template = sorted(name for name, tpl in templates.items() if not tpl)
    if no_template:
        return {"error": "Every node needs a non-empty 'template'",
                "nodes": no_template}

    # Resolve every link endpoint to (node, slot) before touching the server,
    # so mapping errors surface before anything is created.
    resolved_links: list[dict] = []
    for i, link in enumerate(links_spec):
        try:
            a_node, a_iface = _parse_endpoint(link["a"])
            if a_node not in name_set:
                raise EvengError(f"Link {i}: unknown node {a_node!r}")
            a_slot = platforms.slot_for_interface(templates[a_node], a_iface)
            entry: dict = {"a_node": a_node, "a_slot": a_slot}
            if "b" in link:
                b_node, b_iface = _parse_endpoint(link["b"])
                if b_node not in name_set:
                    raise EvengError(f"Link {i}: unknown node {b_node!r}")
                entry["b_node"] = b_node
                entry["b_slot"] = platforms.slot_for_interface(templates[b_node], b_iface)
            elif "network" in link:
                if link["network"] not in net_names:
                    raise EvengError(
                        f"Link {i}: network {link['network']!r} not declared in spec.networks"
                    )
                entry["network"] = link["network"]
            else:
                raise EvengError(f"Link {i}: needs either 'b' or 'network'")
            resolved_links.append(entry)
        except (EvengError, KeyError) as e:
            return {"error": f"Link validation failed: {e}", "link": link}

    # Interface count each node actually needs (slots are 0-based).
    required_slots: dict[str, int] = dict.fromkeys(name_set, 0)
    for link in resolved_links:
        required_slots[link["a_node"]] = max(required_slots[link["a_node"]], link["a_slot"] + 1)
        if "b_node" in link:
            required_slots[link["b_node"]] = max(
                required_slots[link["b_node"]], link["b_slot"] + 1
            )

    # ── execute ─────────────────────────────────────────────────────────────
    steps: list[tuple[str, dict]] = []
    if create_lab:
        steps.append(("create_lab", {}))
    for net in networks_spec:
        steps.append(("network", net))
    for node in nodes_spec:
        steps.append(("node", node))
    for link in resolved_links:
        steps.append(("link", link))

    net_ids: dict[str, int] = {}
    node_ids: dict[str, int] = {}
    warnings: list[str] = []
    template_defaults: dict[str, dict] = {}

    async def _defaults(template: str) -> dict:
        """Template default ram/cpu/ethernet. EVE-NG does NOT apply template
        defaults to API-created nodes (the GUI does that client-side) — an
        omitted 'ram' gives a 1024MB node that a vJunos image will never
        boot on, so we must fetch and send them explicitly."""
        if template not in template_defaults:
            out: dict = {}
            try:
                detail = await client.template_detail(template)
                opts = detail.get("options", {}) if isinstance(detail, dict) else {}
                for key in ("ram", "cpu", "ethernet"):
                    val = opts.get(key, {})
                    val = val.get("value") if isinstance(val, dict) else val
                    if val:
                        out[key] = int(val)
            except (EvengError, TypeError, ValueError):
                pass
            template_defaults[template] = out
        return template_defaults[template]

    for idx, (kind, item) in enumerate(steps):
        label = _step_label(kind, item)
        try:
            if kind == "create_lab":
                folder, _, fname = lab_path.rpartition("/")
                name = fname.removesuffix(".unl")
                await client.create_lab(folder or "/", name, spec.get("description", ""))
            elif kind == "network":
                resp = await client.add_network(
                    lab_path,
                    item["name"],
                    item.get("type", "bridge"),
                    left=item.get("left", 400),
                    top=item.get("top", 50),
                )
                nid = _new_id(resp)
                if nid is None:
                    raise EvengError(f"Network {item['name']!r}: no id in response {resp}")
                net_ids[item["name"]] = nid
            elif kind == "node":
                i = names.index(item["name"])
                info = platforms.lookup(item.get("template", ""))
                body: dict = {
                    "template": item["template"],
                    "name": item["name"],
                    "type": info.eve_type if info else "qemu",
                    "left": item.get("left", 100 + (i % 4) * 250),
                    "top": item.get("top", 100 + (i // 4) * 200),
                }
                if item.get("image"):
                    body["image"] = item["image"]
                required = required_slots[item["name"]]
                if body["type"] == "qemu":
                    defaults = await _defaults(item["template"])
                    if not defaults:
                        warnings.append(
                            f"{item['name']}: could not read template defaults — "
                            "node may get EVE's generic 1024MB/1cpu/4eth"
                        )
                    for k in ("ram", "cpu"):
                        v = item.get(k) or defaults.get(k)
                        if v:
                            body[k] = v
                    eth = max(item.get("ethernet") or defaults.get("ethernet") or 0,
                              required)
                    if eth:
                        body["ethernet"] = eth
                else:
                    for k in ("ram", "cpu", "ethernet"):
                        if item.get(k):
                            body[k] = item[k]
                resp = await client.add_node(lab_path, body)
                nid = _new_id(resp)
                if nid is None:
                    raise EvengError(f"Node {item['name']!r}: no id in response {resp}")
                node_ids[item["name"]] = nid
            elif kind == "link":
                p2p = "network" not in item
                if p2p:
                    # p2p bridge: EVE-NG REJECTS connections to hidden
                    # (visibility=0) networks, so create visible, connect
                    # both ends, then hide the dot best-effort.
                    bridge = f"{item['a_node']}-{item['b_node']}"
                    resp = await client.add_network(
                        lab_path, bridge, "bridge", visibility=1
                    )
                    net_id = _new_id(resp)
                    if net_id is None:
                        raise EvengError(f"Bridge {bridge!r}: no id in response {resp}")
                else:
                    net_id = net_ids[item["network"]]
                await client.connect_interface(
                    lab_path, node_ids[item["a_node"]], item["a_slot"], net_id
                )
                if "b_node" in item:
                    await client.connect_interface(
                        lab_path, node_ids[item["b_node"]], item["b_slot"], net_id
                    )
                if p2p:
                    # cosmetic only — the link itself is up
                    with contextlib.suppress(EvengError):
                        await client.update_network(lab_path, net_id, {"visibility": 0})
            done.append(label)
        except (EvengError, EvengApiError, KeyError) as e:
            # KeyError defends the fail-fast report against future spec-shape
            # bugs — an uncaught exception here would hide what was built.
            failed = f"{label}: {type(e).__name__}: {e}"
            not_attempted = [_step_label(k, it) for k, it in steps[idx + 1 :]]
            break

    result = {
        "lab_path": lab_path,
        "success": failed is None,
        "done": done,
        "failed": failed,
        "not_attempted": not_attempted,
        "node_ids": node_ids,
        "network_ids": net_ids,
    }
    if warnings:
        result["warnings"] = warnings
    return result


def _step_label(kind: str, item: dict) -> str:
    if kind == "create_lab":
        return "create_lab"
    if kind == "network":
        return f"network:{item['name']}"
    if kind == "node":
        return f"node:{item['name']}"
    if "network" in item:
        return f"link:{item['a_node']}:{item['a_slot']}->{item['network']}"
    return (
        f"link:{item['a_node']}:{item['a_slot']}<->"
        f"{item.get('b_node')}:{item.get('b_slot')}"
    )


def register(mcp, cfg: Config, client: EvengClient, platforms: PlatformMap) -> None:
    tool = make_tool(mcp, cfg)

    @tool()
    async def eveng_list_networks(lab_path: str) -> dict:
        """List networks (bridges/clouds) in a lab.

        Args:
            lab_path: e.g. '/MyLab.unl'.
        """
        return {"networks": await client.get_networks(lab_path)}

    @tool()
    async def eveng_get_topology(lab_path: str) -> dict:
        """Get the lab's connection map (which node interfaces connect where).

        Args:
            lab_path: e.g. '/MyLab.unl'.
        """
        return {"topology": await client.get_topology(lab_path)}

    @tool(write=True)
    async def eveng_add_network(
        lab_path: str, name: str, network_type: str = "bridge"
    ) -> dict:
        """Add a network segment to a lab.

        Args:
            lab_path: e.g. '/MyLab.unl'.
            name: network display name.
            network_type: 'bridge' (default), 'ovs', or 'pnet0'-'pnet9' for
                host-facing clouds (pnet = real NIC bridge on the EVE host).
        """
        result = await client.add_network(lab_path, name, network_type)
        return {"added": name, "id": _new_id(result), "response": result}

    @tool(write=True, destructive=True)
    async def eveng_delete_network(lab_path: str, network_id: int) -> dict:
        """Delete a network from a lab (disconnects attached interfaces).

        Args:
            lab_path: e.g. '/MyLab.unl'.
            network_id: numeric network id (see eveng_list_networks).
        """
        result = await client.delete_network(lab_path, network_id)
        return {"deleted": network_id, "response": result}

    @tool(write=True)
    async def eveng_connect_interface(
        lab_path: str, node_id: int, interface: str, network_id: int
    ) -> dict:
        """Connect one node interface to an existing network. Interface may be
        a slot number ('0') or a real name ('ge-0/0/1') when the node's
        template has a naming pattern in platforms.yaml. Use network_id 0 to
        disconnect.

        Args:
            lab_path: e.g. '/MyLab.unl'.
            node_id: numeric node id.
            interface: slot number or interface name.
            network_id: target network id (0 = disconnect).
        """
        node = await client.get_node(lab_path, node_id)
        slot = platforms.slot_for_interface(node.get("template", ""), interface)
        result = await client.connect_interface(lab_path, node_id, slot, network_id)
        action = (f"disconnected node {node_id} slot {slot}" if network_id == 0
                  else f"connected node {node_id} slot {slot} -> net {network_id}")
        return {"result": action, "response": result}

    @tool(write=True)
    async def eveng_connect_nodes(
        lab_path: str, a: str, b: str
    ) -> dict:
        """Create a point-to-point link between two nodes in one call (makes a
        hidden bridge and connects both ends). Endpoints are 'node_name:iface'
        — 'R1:0' (slot) or 'spine1:et-0/0/0' (name, if template is mapped).

        Args:
            lab_path: e.g. '/MyLab.unl'.
            a: first endpoint, e.g. 'R1:ge-0/0/0'.
            b: second endpoint, e.g. 'R2:ge-0/0/0'.
        """
        nodes = await client.get_nodes(lab_path)
        by_name = {n.get("name"): (int(i), n) for i, n in nodes.items()}
        a_node, a_iface = _parse_endpoint(a)
        b_node, b_iface = _parse_endpoint(b)
        for nm in (a_node, b_node):
            if nm not in by_name:
                return {"error": f"Node {nm!r} not found in {lab_path}",
                        "available": sorted(by_name)}
        a_id, a_obj = by_name[a_node]
        b_id, b_obj = by_name[b_node]
        a_slot = platforms.slot_for_interface(a_obj.get("template", ""), a_iface)
        b_slot = platforms.slot_for_interface(b_obj.get("template", ""), b_iface)
        # visible-then-hide: EVE-NG rejects connections to hidden networks
        resp = await client.add_network(lab_path, f"{a_node}-{b_node}", "bridge", visibility=1)
        net_id = _new_id(resp)
        if net_id is None:
            return {"error": f"Bridge creation returned no id: {resp}"}
        await client.connect_interface(lab_path, a_id, a_slot, net_id)
        await client.connect_interface(lab_path, b_id, b_slot, net_id)
        with contextlib.suppress(EvengError):
            await client.update_network(lab_path, net_id, {"visibility": 0})
        return {
            "linked": f"{a_node}:{a_slot} <-> {b_node}:{b_slot}",
            "network_id": net_id,
        }

    @tool(write=True)
    async def eveng_build_topology(
        lab_path: str, spec: dict, create_lab: bool = False
    ) -> dict:
        """Build a whole topology from one declarative spec — nodes, named
        networks, and links in a single call. Fail-fast with a full report of
        done / failed / not-attempted steps.

        Args:
            lab_path: e.g. '/Fabric.unl'.
            spec: {nodes: [{name, template, image?, left?, top?, ram?, cpu?,
                ethernet?}], networks: [{name, type?}], links: [{a:
                'node:iface', b: 'node:iface'} | {a: 'node:iface', network:
                'name'}]}. Interface refs may be slot numbers or real names
                ('spine1:et-0/0/0') for templates mapped in platforms.yaml.
            create_lab: create lab_path first (name taken from the filename).
        """
        return await build_topology(client, platforms, lab_path, spec, create_lab)
