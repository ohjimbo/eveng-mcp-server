# eveng-mcp-server

An [MCP](https://modelcontextprotocol.io) server for [EVE-NG](https://www.eve-ng.net/) — drive your network lab with an AI assistant: build topologies declaratively, configure any vendor's device over its serial console, and keep a live vendor-neutral inventory. Optional integrations reach outward: power-control the bare-metal host (iDRAC), version node configs into GitLab, register Junos nodes with [junos-mcp-server](https://github.com/Juniper/junos-mcp-server), and build labs straight from [NetBox](https://netbox.dev).

## Why another EVE-NG MCP server?

Existing options wrap the EVE-NG REST API: they can draw a topology but can't *configure a device in it*. The interesting work happens on the serial console — which EVE-NG exposes for every node, every vendor, as a telnet port. This server treats the console as a first-class citizen:

- **Tier 1 (driver)** — mapped platforms (Junos, EOS, IOS-XE, NX-OS…) get real semantics via [scrapli](https://github.com/carlmontanari/scrapli): prompt handling, paging off, config mode, commit.
- **Tier 2 (expect)** — anything else with declared prompts: send/expect with regexes.
- **Tier 3 (raw)** — read/write bytes: boot watching, bootloaders, the weird stuff.

Every platform lands on a tier. "Unsupported vendor" isn't an answer this server gives.

## Design principles

- **Derive, don't store.** Inventory is computed live from the EVE-NG API + `platforms.yaml`. No database, no sync drift.
- **Honest tool list.** Integrations register only when configured. If iDRAC isn't set up, `eveng_host_power_on` doesn't exist — instead of failing when called.
- **Skills over frameworks.** Multi-step orchestration (safe teardown, NetBox fabric build, mgmt bootstrap) ships as markdown runbooks served via `skills_list` / `skills_load` — editable without a redeploy, adaptive when a step fails.
- **Fail loudly.** Unmapped templates are reported, not dropped. Failed reads abort writes. Explicitly-enabled-but-unconfigured integrations stop startup.

## EVE-NG compatibility

| Edition | Status |
|---|---|
| **EVE-NG Professional 6.4** | ✅ Certified — full live test plan ([docs/TEST_PLAN.md](docs/TEST_PLAN.md)) including an autonomously built and verified EVPN/VXLAN fabric |
| **EVE-NG Community** | Expected to work, not yet live-tested. The client auto-detects the one known API dialect difference (per-node stop endpoint) and tries the community form first. Reports welcome. |

API realities this server handles for you (found the hard way, both editions get the fixes):

- Template defaults are **not** applied to API-created nodes — the server fetches them and sends ram/cpu/ethernet explicitly, or your vJunos gets 1024 MB and never boots
- Interfaces can't be connected to hidden (`visibility=0`) networks — p2p bridges are created visible, connected, then hidden
- Interface disconnect is an **empty-string** value in the PUT body, not `"0"`
- Aggregate `/nodes/start|stop` endpoints are unreliable — bulk actions iterate per node
- The console proxy replays stale screen content and can drop bytes written immediately after connect — console tools settle before sending

## Quickstart

```bash
cp .env.example .env   # set EVENG_HOST/USER/PASS + MCP_AUTH_TOKEN (required)
docker compose up -d
curl http://localhost:8080/health
```

Connect from Claude Code:

```bash
claude mcp add --transport http eveng http://YOUR_HOST:8080/mcp \
  --header "Authorization: Bearer YOUR_MCP_AUTH_TOKEN"
```

Or any MCP client that speaks Streamable HTTP with a bearer token.

Without Docker: `pip install .` then `eveng-mcp-server` (Python ≥ 3.12).

## Tools

| Area | Tools |
|---|---|
| Labs | `eveng_list_labs` `eveng_get_lab` `eveng_create_lab` `eveng_delete_lab` `eveng_list_templates` `eveng_template_detail` |
| Nodes | `eveng_list_nodes` `eveng_node_detail` `eveng_add_node` `eveng_start_nodes` `eveng_stop_nodes` `eveng_wipe_nodes` `eveng_delete_node` |
| Topology | `eveng_list_networks` `eveng_add_network` `eveng_delete_network` `eveng_connect_interface` `eveng_connect_nodes` `eveng_get_topology` **`eveng_build_topology`** |
| Console | `console_command` `console_get_config` `console_send_config` (tier 1) · `console_expect` (tier 2) · `console_read` `console_write` `console_wait_for` (tier 3) |
| Inventory | `eveng_inventory` — vendor/OS identity, console tier, `reachable_via` per device |
| Meta | `eveng_health` (GO/DEGRADED/CRITICAL gate) `eveng_capabilities` `eveng_api` (raw escape hatch) `skills_list` `skills_load` |

`eveng_build_topology` takes a declarative spec — nodes, networks, links with **real interface names** (`spine1:et-0/0/0`) — and builds the whole lab in one call, fail-fast with a full done/failed/not-attempted report. Pass `rollback_on_failure=true` and a failed build also unwinds whatever it created.

### Risk controls

- Every tool carries MCP-standard **annotations** (`readOnlyHint`, `destructiveHint`) mirroring its `read`/`write`/`destructive` tags, so clients can rank risk without knowing this server.
- `EVENG_MCP_READ_ONLY=true` registers only read tools — unregistered beats registered-but-denied, since the model can't select what it can't see.
- The raw `eveng_api` escape hatch is gated by **`EVENG_RAW_API`**: `read` (GET-only, the default), `full`, or `off` (not registered). Arbitrary `DELETE`/`PUT` passthrough is opt-in, never ambient.

## Integrations (all optional, config-gated)

| Integration | Enables | Config |
|---|---|---|
| `idrac` | Power on/off the EVE-NG bare-metal host | `IDRAC_HOST/USER/PASS` |
| `gitlab_backup` | One-commit backup of running node configs (via console) | `GITLAB_URL`, `GITLAB_TOKEN`, `GITLAB_BACKUP_PROJECT` |
| `junos_mcp` | Register lab Junos devices with a junos-mcp-server instance (devices.json + hot reload) | `JMCP_URL` (+ `JMCP_DEVICE_USER/PASS`, docker.sock mount) |
| `netbox` | **Preview/build labs from a NetBox site**, publish lab devices, drift diff | `NETBOX_URL`, `NETBOX_TOKEN` |

`eveng_capabilities` reports what's enabled and why anything was skipped.

> **⚠ docker.sock warning (junos_mcp):** mounting `/var/run/docker.sock` grants the container the full Docker Engine API — root-equivalent control of the Docker host — even though this integration only uses the container-archive endpoints. Enable it only while you actually use the integration, and prefer pointing `DOCKER_SOCKET` at a filtering proxy such as [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) instead of the raw socket.

### The NetBox flow

```
netbox_preview_lab(site="hq")     # dry run: mapping, unmapped types, RAM feasibility
netbox_build_lab("/HQ.unl", site="hq", rack="r1")
→ bootstrap-mgmt-via-console skill (IPs from NetBox IPAM)
netbox_diff_lab("/HQ.unl", site="hq")   # prove the twin still matches
```

## platforms.yaml — the mapping spine

One file maps EVE-NG template names → vendor/OS, scrapli platform, interface naming (`ge-0/0/{n}` ↔ slot), boot times, console prompts for tier-2, and NetBox device types. Ship defaults cover Junos/EOS/IOS/NX-OS/SR Linux/VyOS; override or extend with your own file:

```bash
EVENG_PLATFORM_MAP=/path/to/mine.yaml   # deep-merged over the shipped map
```

Unknown templates still appear in inventory (`vendor: null`) and are listed in `unmapped_templates` — the fix is a yaml entry, not a code change.

## Skills

Vetted runbooks the model loads on demand: `safe-lab-teardown`, `reset-lab-to-pristine`, `bootstrap-mgmt-via-console`, `build-fabric-from-netbox`, `lab-vs-source-drift-check`. Skills declare which integrations they need; `skills_list` marks unrunnable ones honestly.

## Operational notes

- **Securing the endpoint.** The MCP endpoint speaks plain HTTP; terminate TLS in front of it with a reverse proxy (Caddy/Traefik/nginx) and connect via `https://…/mcp`, or keep the listener on a trusted, isolated network. `MCP_HOST=127.0.0.1` binds only to loopback for proxied setups. By default the static bearer token gates everything — for browser-based clients (e.g. claude.ai custom connectors) set `OIDC_CONFIG_URL` + `OIDC_CLIENT_ID` + `OIDC_CLIENT_SECRET` + `MCP_BASE_URL` and the server proxies OAuth to your IdP (Authentik, Keycloak, …) *in addition to* the bearer token; register `<MCP_BASE_URL>/auth/callback` plus your client's callback at the IdP.
- **Consoles are exclusive.** While a tool holds a node's console, the EVE-NG web UI can't open it (and vice versa) — sessions here are deliberately open→act→close, and "busy" is a distinct error.
- **VNC-only nodes** (Windows, some appliances) can't be text-driven — inventory reports `console.protocol: vnc` up front.
- **Boot times vary wildly** (IOL seconds → vJunos Evolved ~7 min). `console_wait_for` uses per-template `boot_seconds` defaults.

## Development

```bash
pip install -e .[dev]
ruff check . && pytest -q
```

Tests run against a mocked EVE-NG API — no lab required. CI: GitHub Actions (lint+test) here; build/deploy runs on a private GitLab.

## Roadmap (v2 candidates)

- Runtime multi-target (`eveng_connect` to any EVE-NG per session)
- Session-pooled consoles for long interactive sequences
- SSH/volume transports for the junos-mcp integration (currently docker-socket)
- NetBox cable/interface publish
- Config-context → day-0 config rendering

## License

MIT
