---
name: bootstrap-mgmt-via-console
title: Bootstrap management access via console — solve the chicken-and-egg
description: >
  Freshly built nodes have consoles but no management IPs, so nothing
  IP-based (SSH, junos-mcp registration, Ansible) can reach them. This skill
  pushes day-0 management config over each console, then verifies
  reachable_via includes mgmt. Trigger phrases: "bootstrap the lab", "set up
  management", "give the nodes IPs", "make the nodes reachable".
requires_integrations: []
tools: [eveng_inventory, eveng_health, console_wait_for, console_send_config,
        console_command, console_expect]
---

# Bootstrap management via console

## 1. Establish the plan
Get the mgmt addressing from (in order of preference): the user's explicit
list, a NetBox build's `mgmt_plan` output earlier in the conversation, or ask.
You need: per-node IP/mask, and optionally a gateway. Identify the mgmt
interface per platform from the inventory device's template mapping
(junos: fxp0 / re0:mgmt-0, eos: Management1, vios: Gi0/0 …).

## 2. Verify boot state
`eveng_inventory(lab_path, running_only=true)` — every target must be
running. For any node the user just started, `console_wait_for(node)` before
touching it.

## 3. Push day-0 config, per node
Tier-1 nodes: `console_send_config` with the platform-native lines, e.g.

- junos: `set system root-authentication plain-text-password ...` (if fresh),
  `set interfaces fxp0.0 family inet address <ip>/<mask>`,
  `set system services ssh root-login allow`
- eos: `interface Management1` / `ip address <ip>/<mask>`, `username ...`,
  `management ssh` defaults are on
- ios: `interface Gi0/0` / `ip address <ip> <mask>` / `no shut`, plus
  `ip ssh version 2`, local user, `line vty` login local

Tier-2/3 nodes: use `console_expect` step-by-step (send one line, expect the
prompt). A fresh Junos node needs root login + `cli` + `configure` first.

## 4. Verify
Re-run `eveng_inventory(lab_path)`. EVE-NG only learns some node IPs — the
real test is the user pinging/SSHing, or a downstream registration
succeeding. Report per node: config pushed OK / failed, and whether
reachable_via now shows mgmt.

## 5. Hand off
If the junos_mcp integration is enabled and the lab has Junos nodes, offer
`eveng_register_junos_devices`. Mention Ansible/SSH for the rest.
