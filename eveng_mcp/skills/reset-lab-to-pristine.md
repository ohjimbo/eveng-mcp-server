---
name: reset-lab-to-pristine
title: Reset a lab to pristine — repeatable-demo reset
description: >
  Return a lab to factory state so a demo can be run again from scratch:
  optional config backup, stop all nodes, wipe all state, optionally restart.
  Trigger phrases: "reset the lab", "wipe the lab", "back to pristine",
  "run the demo again from scratch".
requires_integrations: []
optional_integrations: [gitlab_backup]
tools: [eveng_inventory, eveng_backup_configs, eveng_stop_nodes,
        eveng_wipe_nodes, eveng_start_nodes, console_wait_for]
---

# Reset lab to pristine

WIPE DELETES ALL NODE CONFIGS AND DISK STATE. Confirm with the user before
step 3 unless they explicitly said wipe/pristine/factory.

## 1. Offer a backup
If `eveng_backup_configs` is available, offer it ("last chance to keep the
configs"); run it if accepted. If unavailable, warn that current configs are
about to be lost permanently.

## 2. Stop
`eveng_stop_nodes(lab_path)` — wipe requires stopped nodes.

## 3. Wipe
`eveng_wipe_nodes(lab_path)` — all nodes back to factory.

## 4. Restart (only if the user wants the lab running again)
`eveng_start_nodes(lab_path)`, then for the nodes the user will touch first,
`console_wait_for(node)` — boot times vary wildly (IOL seconds, vJunos
minutes; the tool already uses per-template boot_seconds).

## Output
One line per step, plus total boot wait if restarted.
