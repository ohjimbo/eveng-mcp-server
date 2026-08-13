---
name: build-fabric-from-netbox
title: Build a lab from a NetBox site — preview, build, boot, bootstrap
description: >
  The digital-twin build: instantiate a NetBox site slice as a running,
  reachable EVE-NG lab. Trigger phrases: "build the lab from NetBox",
  "spin up site X as a lab", "digital twin of site X", "recreate the fabric
  in EVE-NG".
requires_integrations: [netbox]
tools: [eveng_health, netbox_preview_lab, netbox_build_lab, eveng_start_nodes,
        console_wait_for, netbox_diff_lab]
---

# Build a fabric from NetBox

## 1. Health gate
`eveng_health()`. If DEGRADED/CRITICAL, tell the user and stop — a build on
a starved host fails at the worst moment.

## 2. Preview — always
`netbox_preview_lab(site, ...filters)`. Review with the user:
- **unmapped device types** → offer to narrow with role/tag/rack, or tell
  them which entries to add under `netbox_device_types` in platforms.yaml.
- **feasibility** → if estimated RAM approaches host free memory, propose a
  smaller slice (rack or role filter). A 200-device site does not fit; the
  filters are the interface, not a workaround.
Get an explicit go before building.

## 3. Build
`netbox_build_lab(lab_path, site, ...same filters)`. The result is fail-fast:
if `success` is false, show what was built / failed / not attempted and stop
— don't improvise a half-repair without the user.

## 4. Boot
`eveng_start_nodes(lab_path)`, then `console_wait_for` on a couple of
representative nodes (slowest templates first — vJunos Evolved ≈ 7 min).

## 5. Bootstrap management
The build result's `mgmt_plan` carries each node's intended IP from NetBox
IPAM. Run the **bootstrap-mgmt-via-console** skill with that plan.

## 6. Prove the twin
`netbox_diff_lab(lab_path, site, ...same filters)` — show `in_sync: true` to
the user. This is the closer: the lab demonstrably matches the source of
truth it was built from.
