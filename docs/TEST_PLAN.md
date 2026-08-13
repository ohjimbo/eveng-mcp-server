# eveng-mcp-server — Comprehensive Test Plan

Two layers: **CI tests** (pytest against a mocked EVE-NG API — run on every
push) and **live tests** (executed against a deployed server + real EVE-NG).
Live results below are from the initial certification run; re-run this plan
after any change to client.py, console.py, or topology.py.

**Live environment of record:** server `eveng-mcp` @ Unraid :8090 (host
network), EVE-NG 6.4.0-78-PRO bare metal. All mutations confined to a
scratch lab `/MCP-Test.unl`; pre-existing labs are read, never touched.

Legend: ✅ pass · ❌ fail · ⚠ pass-with-finding · ⏸ blocked · ➖ n/a

## Phase 0 — Preflight & auth

| # | Test | Expected | Result |
|---|---|---|---|
| 0.1 | `GET /health` unauthenticated | 200 `ok` | pending |
| 0.2 | MCP initialize handshake | session id issued | pending |
| 0.3 | `tools/list` | 36 tools (33 core + 3 junos_mcp) | pending |
| 0.4 | `eveng_capabilities` | junos_mcp enabled; gitlab_backup/idrac/netbox skipped with reasons | pending |
| 0.5 | `eveng_health` | verdict GO, real version/cpu/mem | pending |
| 0.6 | MCP call with **no** token | 401 | pending |
| 0.7 | MCP call with **wrong** token | 401/403 | pending |

## Phase 1 — Read-only recon (no mutations)

| # | Test | Expected | Result |
|---|---|---|---|
| 1.1 | `eveng_list_labs('/')` | existing labs listed | pending |
| 1.2 | `eveng_list_templates` | template map incl. vJunos + vpcs | pending |
| 1.3 | `eveng_template_detail(<junos tpl>)` | options with images/ram/ethernet | pending |
| 1.4 | `eveng_inventory()` all labs | devices w/ vendor+os mapping, console endpoints parsed, running count consistent with eveng_health qemu count; unmapped templates surfaced not dropped | pending |
| 1.5 | `skills_list` | 5 skills; netbox-dependent ones runnable=false | pending |
| 1.6 | `skills_load('safe-lab-teardown')` | full runbook body | pending |
| 1.7 | `eveng_api('GET','/status')` | parity with eveng_health raw data | pending |

## Phase 2 — Scratch lab lifecycle (`/MCP-Test.unl` only)

| # | Test | Expected | Result |
|---|---|---|---|
| 2.1 | `eveng_create_lab` | lab created | pending |
| 2.2 | `eveng_get_lab` | metadata back | pending |
| 2.3 | `eveng_build_topology`: 2× vpcs + 1× vJunos, p2p link pc1:0↔pc2:0, named net `mgmt`, r1:ge-0/0/0→mgmt | success:true; junos slot offset (ge-0/0/0→slot 1) applied; ethernet NOT shrunk below template default | pending |
| 2.4 | verify: `eveng_get_topology` + `eveng_list_nodes` | 3 nodes, 2 networks (1 hidden bridge + mgmt), links correct | pending |
| 2.5 | negative: build with unknown node in link | validation error, **nothing created** | pending |
| 2.6 | negative: build with node missing template | clean structured error | pending |
| 2.7 | **disconnect fix**: `eveng_connect_interface(r1, 'ge-0/0/0', 0)` then reconnect | disconnect succeeds (empty-string convention against real API), reconnect succeeds | pending |
| 2.8 | `eveng_add_node` single vpcs (pc3) | added with template defaults | pending |
| 2.9 | `eveng_start_nodes` all | started | pending |
| 2.10 | lab-scoped `eveng_inventory` | all running, reachable_via=[console], console ports real | pending |
| 2.11 | `console_wait_for(pc1)` | booted:true fast (vpcs) | pending |
| 2.12 | `console_read(pc1)` | prompt/boot text | pending |
| 2.13 | tier-2 `console_expect(pc1, send='show ip', expect='>')` | matched, VPCS output | pending |
| 2.14 | `console_wait_for(r1)` vJunos boot (long) | booted:true within boot_seconds | pending |
| 2.15 | bootstrap flow on fresh r1 via `console_expect`: login root → shell → cli → configure → set root password → commit | each step matches expected prompt (validates bootstrap-mgmt-via-console skill mechanics) | pending |
| 2.16 | tier-1 `console_command(r1, 'show version')` with creds | driver session, structured output, failed:false | pending |
| 2.17 | tier-1 `console_get_config(r1)` | `display set` config text | pending |
| 2.18 | tier-1 `console_send_config(r1, [hostname line])` | commit succeeds; visible in 2.17 re-run | pending |

## Phase 3 — junos_mcp integration (non-destructive)

| # | Test | Expected | Result |
|---|---|---|---|
| 3.1 | `jmcp_list_registered_devices` | current devices.json snapshot, no passwords in output | pending |
| 3.2 | `eveng_register_junos_devices('/MCP-Test.unl')` with no mgmt IPs | all skipped with reasons; **devices.json untouched** (3.1 re-run identical) | pending |

## Phase 4 — Credential-gated integrations

| # | Test | Expected | Result |
|---|---|---|---|
| 4.1 | CI vars set (CONSOLE_*, IDRAC_PASS, BACKUP_GITLAB_*, JMCP_DEVICE_*) | vars present, redeploy picks them up | pending |
| 4.2 | `eveng_capabilities` post-redeploy | idrac + gitlab_backup enabled | pending |
| 4.3 | `eveng_host_power_status` | PowerState On (read-only) | pending |
| 4.4 | `eveng_backup_configs('/MCP-Test.unl')` | one commit in root/eveng-backups with r1 config | pending |

## Phase 5 — Teardown & destructive (scratch lab only)

| # | Test | Expected | Result |
|---|---|---|---|
| 5.1 | `eveng_stop_nodes` | stopped | pending |
| 5.2 | `eveng_wipe_nodes` | wiped (2.17's config gone if restarted) | pending |
| 5.3 | `eveng_delete_node(pc3)` | removed | pending |
| 5.4 | `eveng_delete_lab('/MCP-Test.unl')` | gone from eveng_list_labs | pending |

## Phase 6 — Error handling & edges

| # | Test | Expected | Result |
|---|---|---|---|
| 6.1 | `eveng_get_lab('/Nope.unl')` | clean `{"error": "EVE-NG API 4xx …"}` not a stack trace | pending |
| 6.2 | console tool on stopped node | "is stopped — start it first" | pending |
| 6.3 | `eveng_connect_interface` with wrong-pattern iface name | PlatformMapError message naming the pattern | pending |
| 6.4 | `skills_load('nonexistent')` | error + available list | pending |
| 6.5 | `eveng_inventory` includes labs with unmapped templates | listed in unmapped_templates, vendor:null device rows | pending |

## Known coverage gaps (accepted for v0.1)

- Read-only mode live (covered by unit tests; needs a second deployment to test live)
- NetBox integration live (no NetBox instance in this environment — covered by unit-level review only; first live test happens against a demo NetBox)
- Console-busy contention (needs a deliberate second console holder; verified by code review + unit-level behavior)
- VNC-node refusal path (no VNC nodes in scratch lab)
