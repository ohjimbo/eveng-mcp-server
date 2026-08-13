---
name: safe-lab-teardown
title: Safe lab teardown — backup, graceful halt, stop (and optional host power-off)
description: >
  Tear down a running lab without losing work or corrupting disks: back up
  configs if the gitlab_backup integration is enabled, gracefully halt each
  network OS via console, hard-stop the rest, and optionally power off the
  bare-metal host via idrac. Trigger phrases: "tear down the lab", "shut the
  lab down safely", "shut down EVE-NG", "power everything off".
requires_integrations: []
optional_integrations: [gitlab_backup, idrac]
tools: [eveng_inventory, eveng_backup_configs, console_command, eveng_stop_nodes,
        eveng_host_power_off, eveng_health]
---

# Safe lab teardown

Ask which lab (or "all") if the user didn't say. Then:

## 1. Snapshot what's running
Call `eveng_inventory(lab_path=<lab>, running_only=true)`. If nothing is
running, skip to step 4. Report the device count and vendors to the user.

## 2. Back up configs — IF gitlab_backup is enabled
If `eveng_backup_configs` is in your tool list, call it for the lab and
report the commit id. If the tool is absent, say configs are NOT being backed
up and continue (mention `console_get_config` as a manual alternative if the
user hesitates).

## 3. Graceful halt, per device
For each running device in the inventory, in any order:
- If it has `os_family: junos` → `console_command(node, "request system halt")`,
  answering the confirm prompt is handled by the driver. Wait ~20s.
- If `os_family: vyos` or `linux` → `console_command`/`console_expect` with
  `poweroff`.
- Anything else (eos, ios, unmapped) has no meaningful graceful halt — leave
  it for step 4 and say so.
A failed console session on one node is NOT a reason to abort: note it and
continue with the others.

## 4. Stop everything
`eveng_stop_nodes(lab_path=<lab>)`. If the user asked for all labs, repeat
per lab from the inventory's lab list.

## 5. Host power-off — ONLY if asked and idrac is enabled
If the user asked to power off the server AND `eveng_host_power_off` exists:
confirm with the user, then call it (graceful, not force). If the tool is
absent, tell them host power-off isn't configured (IDRAC_* env).

## Output
Short report: backed up (commit id or "not configured"), halted gracefully
(list), hard-stopped (list), console failures (list), host power state.
