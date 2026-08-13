"""Integration loader — config-gated optional modules.

An integration is a module in eveng_mcp/integrations/ with:
    REQUIRES = ("ENV_VAR", ...)   # env vars that must be non-empty
    SUMMARY  = "one line"
    def register(mcp, cfg, client, platforms): ...

Rules:
  - EVENG_MCP_INTEGRATIONS set        → exactly those (unmet REQUIRES = hard
    startup error: you asked for it and didn't get it)
  - EVENG_MCP_DISABLE                 → subtract
  - otherwise                         → auto-enable when REQUIRES are all set

A skipped integration's tools are never registered — the tool list is an
honest statement of what this deployment can do.
"""

from __future__ import annotations

import importlib
import pkgutil

from eveng_mcp.config import Config, ConfigError


def load_integrations(mcp, cfg: Config, client, platforms) -> dict:
    import eveng_mcp.integrations as pkg

    names = sorted(m.name for m in pkgutil.iter_modules(pkg.__path__)
                   if not m.name.startswith("_"))
    enabled: list[dict] = []
    skipped: list[dict] = []

    for name in names:
        mod = importlib.import_module(f"eveng_mcp.integrations.{name}")
        requires: tuple = getattr(mod, "REQUIRES", ())
        summary: str = getattr(mod, "SUMMARY", name)
        missing = [v for v in requires if not cfg.env(v)]

        if cfg.integrations_allow is not None and name not in cfg.integrations_allow:
            skipped.append({"name": name, "reason": "not in EVENG_MCP_INTEGRATIONS"})
            continue
        if name in cfg.integrations_disable:
            skipped.append({"name": name, "reason": "disabled via EVENG_MCP_DISABLE"})
            continue
        if missing:
            if cfg.integrations_allow is not None:
                raise ConfigError(
                    f"Integration '{name}' is explicitly enabled in "
                    f"EVENG_MCP_INTEGRATIONS but missing: {', '.join(missing)}"
                )
            skipped.append({"name": name, "reason": f"{', '.join(missing)} not set"})
            continue

        mod.register(mcp, cfg, client, platforms)
        enabled.append({"name": name, "summary": summary})

    if cfg.integrations_allow is not None:
        unknown = set(cfg.integrations_allow) - set(names)
        if unknown:
            raise ConfigError(
                f"EVENG_MCP_INTEGRATIONS names unknown integration(s): "
                f"{', '.join(sorted(unknown))}. Available: {', '.join(names)}"
            )

    return {"enabled": enabled, "skipped": skipped}
