"""Centralised configuration — environment-driven, no inline secret defaults.

Unlike a homelab config, nothing here ships with a working credential. Missing
required values are a startup error with a clear message, not a silent
degradation discovered at call time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(Exception):
    """Raised when configuration is missing or inconsistent."""


_REQUIRED = ("EVENG_HOST", "EVENG_USER", "EVENG_PASS", "MCP_AUTH_TOKEN")


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    """Runtime configuration. ``env`` holds the raw environment snapshot so
    integrations can declare REQUIRES by variable name without every knob
    needing a dataclass field."""

    eveng_host: str
    eveng_user: str
    eveng_pass: str
    auth_token: str
    eveng_protocol: str = "https"
    eveng_verify_ssl: bool = False
    port: int = 8080
    host: str = "0.0.0.0"  # bind 127.0.0.1 when a local reverse proxy fronts TLS
    read_only: bool = False
    integrations_allow: list[str] | None = None  # None = auto-enable by config presence
    integrations_disable: set[str] = field(default_factory=set)
    platform_map_path: str | None = None
    console_user: str = ""
    console_pass: str = ""
    _env: dict[str, str] = field(default_factory=dict, repr=False)

    def env(self, name: str, default: str = "") -> str:
        """Read a raw environment value (integration knobs live here)."""
        return self._env.get(name, default)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Config:
        env = dict(environ if environ is not None else os.environ)

        missing = [v for v in _REQUIRED if not env.get(v)]
        if missing:
            raise ConfigError(
                "Missing required configuration: "
                + ", ".join(missing)
                + ". See .env.example — every required value must be set explicitly."
            )

        allow_raw = env.get("EVENG_MCP_INTEGRATIONS", "").strip()
        allow = [s.strip() for s in allow_raw.split(",") if s.strip()] if allow_raw else None
        disable_raw = env.get("EVENG_MCP_DISABLE", "").strip()
        disable = {s.strip() for s in disable_raw.split(",") if s.strip()}

        protocol = env.get("EVENG_PROTOCOL", "https").strip().lower()
        if protocol not in ("http", "https"):
            raise ConfigError(f"EVENG_PROTOCOL must be http or https, got {protocol!r}")

        try:
            port = int(env.get("MCP_PORT", "8080"))
        except ValueError as e:
            raise ConfigError(f"MCP_PORT must be an integer: {e}") from e

        return cls(
            eveng_host=env["EVENG_HOST"],
            eveng_user=env["EVENG_USER"],
            eveng_pass=env["EVENG_PASS"],
            auth_token=env["MCP_AUTH_TOKEN"],
            eveng_protocol=protocol,
            eveng_verify_ssl=_as_bool(env.get("EVENG_VERIFY_SSL"), False),
            port=port,
            host=env.get("MCP_HOST", "0.0.0.0"),
            read_only=_as_bool(env.get("EVENG_MCP_READ_ONLY"), False),
            integrations_allow=allow,
            integrations_disable=disable,
            platform_map_path=env.get("EVENG_PLATFORM_MAP") or None,
            console_user=env.get("CONSOLE_USER", ""),
            console_pass=env.get("CONSOLE_PASS", ""),
            _env=env,
        )
