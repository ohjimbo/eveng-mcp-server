---
name: lab-vs-source-drift-check
title: Lab vs source drift check — does the lab still match what it models?
description: >
  Verify a lab still matches the NetBox slice it was built from, and report
  drift in both directions with a verdict. Trigger phrases: "has the lab
  drifted", "does the lab still match NetBox", "drift check", "is the twin
  still accurate".
requires_integrations: [netbox]
tools: [netbox_diff_lab, eveng_inventory, netbox_preview_lab]
---

# Lab vs source drift check

## 1. Establish the source slice
Ask (or recall from conversation) which site + filters the lab was built
from. Diffing against a different slice than the build produces noise, not
drift.

## 2. Diff
`netbox_diff_lab(lab_path, site, ...filters)`.

## 3. Interpret — devices first, links second
- `only_in_netbox`: the real site grew — the lab is stale. Offer
  `netbox_build_lab` of the delta into the same lab, or a fresh rebuild.
- `only_in_lab`: someone extended the lab by hand — that may be intentional
  experimentation. Ask before calling it drift; offer `netbox_publish_lab`
  if the additions should become the documented state.
- Link counts far apart with devices in sync usually means hand-rewiring in
  the EVE-NG GUI; compare `eveng_inventory(include_links=true)` against the
  preview's link list to name the specific cables.

## 4. Verdict
🟢 IN-SYNC (device sets equal, link counts close) /
🟡 DRIFTED (differences exist, explainable) /
🔴 DIVERGED (device sets disagree substantially — the lab no longer models
the site). One paragraph, then the evidence tables.
