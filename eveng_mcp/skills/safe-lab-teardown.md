---
name: safe-lab-teardown
title: Safe lab teardown — verified backup, graceful halt, stop (and optional host power-off)
description: >
  Tear down a running lab without losing work or corrupting disks: back up
  configs (verified) if the gitlab_backup integration is enabled, gracefully
  halt every network OS via console in one call, hard-stop the rest, and
  optionally power off the bare-metal host via idrac. Trigger phrases: "tear
  down the lab", "shut the lab down safely", "shut down EVE-NG", "power
  everything off", "power off eve".
requires_integrations: []
optional_integrations: [gitlab_backup, idrac]
tools: [eveng_inventory, eveng_backup_configs, eveng_halt_nodes, console_halt,
        eveng_stop_nodes, eveng_host_power_off, eveng_host_power_status, eveng_health]
---

# Safe lab teardown

Ask which lab (or "all") if the user didn't say. If the user asked to power
off the host, "all" is implied: run steps 1–4 for EVERY lab that has running
nodes (`eveng_inventory(running_only=true)` lists them), then step 5.

Every tool below finishes inside one call's time budget (~50s) and says so
when it couldn't finish (`incomplete: true` + `pending`): call it again for
the rest. Never assume a step happened because a call timed out — the
result object is the only truth.

## 1. Snapshot what's running
`eveng_inventory(lab_path=<lab>, running_only=true)`. If nothing is running,
skip to step 4. Report the device count and vendors to the user.

## 2. Back up configs — IF gitlab_backup is enabled
If `eveng_backup_configs` is in your tool list, call it for the lab. Report
the commit id and, per file, `lines` + `verified`. Entries under `skipped`
with "capture truncated" were NOT committed — retry those once; if they
still fail, say so plainly. Repeat while `incomplete` is true. If the tool is
absent, say configs are NOT being backed up and continue (`console_get_config`
is the manual alternative; check its `verified` field too).

## 3. Graceful halt — one call per lab
`eveng_halt_nodes(lab_path=<lab>)`. It halts every node whose template has a
`halt` command in platforms.yaml (Junos `request system halt`, vSRX
`request system power-off`, VyOS/Linux `poweroff`), answers the confirm
prompt and waits for the halt marker. Read the result:
- `halted` — done. **Never send anything to these consoles afterwards**: a
  halted Junos/FreeBSD guest reboots on any keypress.
- `failed` — retry that node once with `console_halt(node)`; if it fails
  again, tell the user it will be hard-stopped.
- `skipped` — no graceful halt exists (vpcs, eos, ios, VNC-only nodes such
  as Routing Director or FortiGate). Hard stop is fine for stateless nodes;
  for appliance VMs mention that an ACPI power-down would need host access.
- `incomplete` — call `eveng_halt_nodes` again for `pending`.
Do NOT use `console_command` for halt/reboot commands: they never return a
prompt, the driver waits out its timeout, and the confirm prompt is left
unanswered (the node keeps running while the report says "halted").

## 4. Stop everything
`eveng_stop_nodes(lab_path=<lab>)` — releases the QEMU processes (a halted
guest still holds its vCPUs). Call labs one at a time, not in parallel.

## 5. Host power-off — ONLY if asked and idrac is enabled
If the user asked to power off the server AND `eveng_host_power_off` exists:
call it (graceful, not force), then poll `eveng_host_power_status` until it
reports Off (a graceful OS shutdown takes 1–2 minutes). If the tool is
absent, tell them host power-off isn't configured (IDRAC_* env).

## Output
Short report: backed up (commit id + files with lines/verified, or "not
configured"), halted gracefully (list), hard-stopped (list), console
failures (list), host power state.
