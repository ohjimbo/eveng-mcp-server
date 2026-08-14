# eveng-mcp-server — Comprehensive Test Plan

Two layers: **CI tests** (pytest against a mocked EVE-NG API — run on every
push) and **live tests** (executed against a deployed server + real EVE-NG).
Re-run this plan after any change to client.py, console.py, or topology.py.

**Certification run: 2026-08-13.** Server `eveng-mcp` @ Unraid :8090 (host
network), EVE-NG **6.4.0-78-PRO** bare metal, 13 pre-existing labs / 110
devices. All mutations confined to scratch labs `/MCP-Test.unl` +
`/MCP-Stop.unl` (both deleted afterward); pre-existing labs read, never
touched. The run surfaced **5 live-only defects**, all fixed and re-verified
(commits 43555ea…d998120) — see "Defects found live" at the bottom.

Legend: ✅ pass · ⚠ pass-with-finding (fixed) · ⏸ blocked · ➖ n/a

## Phase 0 — Preflight & auth

| # | Test | Expected | Result |
|---|---|---|---|
| 0.1 | `GET /health` unauthenticated | 200 `ok` | ✅ |
| 0.2 | MCP initialize handshake | session id issued | ✅ |
| 0.3 | `tools/list` | 36 tools (33 core + 3 junos_mcp) | ✅ 36 |
| 0.4 | `eveng_capabilities` | junos_mcp enabled; others skipped with reasons | ✅ |
| 0.5 | `eveng_health` | verdict GO, real version/cpu/mem | ✅ GO, 6.4.0-78-PRO |
| 0.6 | MCP call with **no** token | 401 | ✅ 401 |
| 0.7 | MCP call with **wrong** token | 401/403 | ✅ 401 |

## Phase 1 — Read-only recon (no mutations)

| # | Test | Expected | Result |
|---|---|---|---|
| 1.1 | `eveng_list_labs('/')` | existing labs listed | ✅ 12 labs + 4 folders |
| 1.2 | `eveng_list_templates` | template map | ✅ — surfaced mixed-case `vJunos-switch` (defect L1) |
| 1.3 | `eveng_template_detail('vJunos-switch')` | images/ram/ethernet | ✅ 4 images, 5120MB/4cpu/11eth |
| 1.4 | `eveng_inventory()` all labs | mapping, console endpoints, counts consistent | ✅ 110 devices, **0 unmapped**, 12 running (−3 VPCS = 9 = health qemu count), labs-with-spaces OK |
| 1.5 | `skills_list` | 5 skills; netbox ones runnable=false | ✅ |
| 1.6 | `skills_load('safe-lab-teardown')` | full runbook body | ✅ |
| 1.7 | `eveng_api('GET','/status')` | parity with eveng_health | ✅ |

## Phase 2 — Scratch lab lifecycle (`/MCP-Test.unl`)

| # | Test | Expected | Result |
|---|---|---|---|
| 2.1 | `eveng_create_lab` | created | ✅ |
| 2.2 | `eveng_get_lab` | metadata | ✅ |
| 2.3 | `eveng_build_topology` 2×vpcs + vJunos-switch, p2p + named net | success, junos slot offset | ⚠→✅ found defect L2 (hidden-bridge 20033); after fix: full build, `ge-0/0/0`→slot 1 |
| 2.4 | `eveng_get_topology` + `eveng_list_nodes` | correct links + resources | ⚠→✅ found defect L3 (r1 created 1024MB/4eth); after fix: 5120MB/4cpu/11eth |
| 2.5 | negative: unknown node in link | validation error, nothing created | ✅ |
| 2.6 | negative: node missing template | clean structured error | ✅ |
| 2.7 | disconnect via `network_id=0` then reconnect | empty-string convention works on real API | ✅ (validates the pre-push review fix) |
| 2.8 | `eveng_add_node` single vpcs | added, non-qemu skips defaults fetch | ✅ |
| 2.9 | `eveng_start_nodes` all | started | ⚠→✅ found defect L4 (aggregate endpoints 400/500 on PRO); per-node iteration works |
| 2.10 | lab-scoped inventory | tiers/boot_seconds/reachable_via correct | ✅ vpcs tier2/10s, r1 tier1/300s |
| 2.11 | `console_wait_for(pc1)` | fast boot detected | ✅ VPCS banner + prompt |
| 2.12 | `console_read(pc2)` | boot text | ✅ |
| 2.13 | `console_expect(pc1)` send+expect | matched | ✅ |
| 2.14 | `console_wait_for(r1)` vJunos boot | booted | ✅ two-stage: Wind River shim login @~5min, inner Junos `ttyd0` later — pattern choice documented |
| 2.15 | bootstrap fresh Junos via expect chain (login→cli→configure→hostname→root password→commit) | every step matches | ✅ JUNOS 25.2R1.9, `commit complete` |
| 2.16 | tier-1 `console_command('show version')` | driver output | ⚠→✅ found defect L5 (scrapli telnet auth vs persistent console); after fix: `Hostname: mcp-r1, Model: ex9214` |
| 2.17 | tier-1 `console_get_config` | display-set config | ✅ 21 lines |
| 2.18 | tier-1 `console_send_config` | committed change | ⚠→✅ found defect L6 (no auto-commit on junos); after fix: `commit complete`, verified via 2.17 |

## Phase 3 — junos_mcp integration (non-destructive)

| # | Test | Expected | Result |
|---|---|---|---|
| 3.1 | `jmcp_list_registered_devices` | snapshot, no passwords | ✅ (docker-socket archive read works) |
| 3.2 | register w/o creds configured | clean credentials-required error, file untouched | ✅ |
| 3.2b | register with per-call creds, no mgmt IPs | all skipped with reasons, devices.json untouched | ✅ os_family filter + no-mgmt-IP skip both exercised |

## Phase 4 — Credential-gated integrations

| # | Test | Expected | Result |
|---|---|---|---|
| 4.1 | CI vars (CONSOLE_*, IDRAC_PASS, BACKUP_GITLAB_*, JMCP_DEVICE_*) | set + redeploy | ⏸ automation policy blocks credential-value writes — set manually in GitLab → project 25 → Settings → CI/CD → Variables, then re-run pipeline |
| 4.2 | capabilities post-redeploy | idrac + gitlab_backup enabled | ⏸ depends on 4.1 |
| 4.3 | `eveng_host_power_status` | PowerState On | ⏸ depends on 4.1 |
| 4.4 | `eveng_backup_configs` | commit in root/eveng-backups | ⏸ depends on 4.1 |

## Phase 5 — Teardown & destructive (scratch labs only)

| # | Test | Expected | Result |
|---|---|---|---|
| 5.1 | `eveng_stop_nodes` | stopped | ⚠→✅ found defect L7 (PRO wants `/stop/stopmode=3`); dialect auto-detect added + re-verified |
| 5.2 | `eveng_wipe_nodes` | wiped | ✅ (also force-kills running nodes on PRO) |
| 5.3 | `eveng_delete_node` | removed | ✅ |
| 5.4 | `eveng_delete_lab` + verify gone | 404 on re-get | ✅ both scratch labs; pre-existing labs confirmed untouched |

## Phase 6 — Error handling & edges

| # | Test | Expected | Result |
|---|---|---|---|
| 6.1 | get nonexistent lab | clean structured error | ✅ `EVE-NG API 404: Lab does not exist (60000).` |
| 6.2 | console tool on stopped node | actionable message | ✅ "is stopped — start it first" |
| 6.3 | wrong-pattern interface name | names the expected pattern | ✅ |
| 6.4 | `skills_load('nonexistent')` | error + available list | ✅ |
| 6.5 | unmapped templates surfaced | listed, not dropped | ➖ live env had zero unmapped; covered by unit tests |

## Defects found live (all fixed + re-verified same day)

| # | Defect | Fix |
|---|---|---|
| L1 | Mixed-case template names (`vJunos-switch`) bypassed exact lookup, silently degrading to interface-less prefix match | case-insensitive template matching (43555ea) |
| L2 | p2p links failed 20033 — EVE-NG rejects connecting to `visibility=0` networks | create visible → connect → hide (06ba110) |
| L3 | EVE-NG does **not** apply template defaults to API-created nodes — vJunos got 1024MB and could never boot | fetch template defaults, send ram/cpu/ethernet explicitly (cd94e62) |
| L4 | Aggregate `/nodes/start\|stop` endpoints return 400/500 on PRO 6.4 | bulk ops iterate per node (cd94e62) |
| L5 | scrapli's passive telnet auth times out on EVE's persistent consoles (prompt printed before connect) | own the login walk via expect, then scrapli with auth_bypass (cbfdd86) |
| L6 | junos config changes sat uncommitted (scrapli doesn't commit) | `config_commit` family directive, auto-appended (f06d92d) |
| L7 | Per-node stop rejected on PRO — wants `/stop/stopmode=3` | dialect auto-detect with fallback + memo (d998120) |

Observation (not a defect): on this EVE-NG PRO build, consoles are
**last-connect-wins** — a new telnet connection steals the console from the
previous holder rather than being refused. The ConsoleBusy path still covers
builds that refuse; sessions here are short-lived by design either way.

## Known coverage gaps (accepted for v0.1)

- Read-only mode live (unit-tested; needs a second deployment)
- NetBox integration live (no NetBox instance here; first live run against a demo NetBox)
- VNC-node refusal path (no VNC nodes in scratch labs)
- Phase 4 integrations (blocked on CI variables — see above)
