"""Custom scrapli platform definitions — where a real device disagrees with
scrapli's stock platform.

scrapli's core drivers cover the common cases; this module holds the handful
that need their own privilege graph, and hands `console.py` a single factory
so the console layer stays a thin dispatcher.

── juniper_junos_evo ──────────────────────────────────────────────────────

Junos **Evolved** is Linux-based, not FreeBSD. Its serial console lands in a
Linux shell — usually already logged in as root — whose prompt carries a vrf
tag::

    [vrf:none] root@re0:~#

``cli`` enters the Junos operational CLI (``root@re0>``); ``configure`` enters
config mode (``root@re0#``); ``exit`` from the CLI drops **back to that Linux
shell**, and a second ``exit`` logs the console out.

scrapli's stock ``juniper_junos`` platform models the shell the FreeBSD way:
a level *above* ``exec``, entered with ``start shell`` and left with ``exit``.
Its ``root_shell`` pattern — ``^.*root@(?:\\S*:?\\S*\\s?)?[%\\#]\\s?$`` — matches
the Evolved prompt, so scrapli concludes it must **deescalate** and sends
``exit``. On Evolved that logs the console out, the console auto-logs-in
again, scrapli sees the same shell prompt, sends ``exit`` again… until
``acquire_priv`` gives up after ``2 * len(privilege_levels)`` attempts with::

    ScrapliPrivilegeError: Failed to acquire requested privilege level configuration

which is exactly what tier-1 console tools reported against a ``vjunosevo``
node. Evolved therefore gets its own privilege graph, with the Linux shell as
the **base** level and ``cli`` as the escalation into ``exec``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, scrapli is imported lazily
    from scrapli.driver import AsyncNetworkDriver
    from scrapli.driver.network.base_driver import PrivilegeLevel

#: platforms.yaml value that selects the Junos Evolved driver below.
JUNOS_EVO_PLATFORM = "juniper_junos_evo"

#: Junos Evolved's Linux shell prompt, as a stream-searchable regex (anchored
#: at the end of the buffer, not at a line start — the login walk in
#: ``console.py`` matches it against raw console output). Tolerant of the vrf
#: name (``[vrf:none]``, ``[vrf:mgmt_junos]``), the user@host part
#: (``root@re0``, ``lab@ptx-a:re1``) and the working directory.
JUNOS_EVO_SHELL_PROMPT = r"\[vrf:\S+\]\s*\S+@\S+:\S*\s?#\s?$"

# scrapli matches privilege patterns with re.M against the prompt alone, so the
# same expression is used line-anchored.
_EVO_SHELL_PRIV_PATTERN = "^" + JUNOS_EVO_SHELL_PROMPT

# Operational / configuration prompts are identical to Junos classic; these are
# scrapli's own core-junos patterns, reused so behaviour stays consistent.
_JUNOS_EXEC_PATTERN = r"^({\w+(:(\w+){0,1}\d){0,1}}\n){0,1}[\w\-@()/:\.]{1,63}>\s?$"
_JUNOS_CONFIG_PATTERN = (
    r"^({\w+(:(\w+){0,1}\d){0,1}}\[edit\]\n){0,1}[\w\-@()/:\.]{1,63}#\s?$"
)


def junos_evo_privilege_levels() -> dict[str, PrivilegeLevel]:
    """Privilege graph for Junos Evolved: shell (base) → exec → configuration.

    Deliberately drops scrapli's ``shell`` / ``root_shell`` levels: on Evolved
    there is exactly one shell and it sits *below* the CLI, so keeping the
    FreeBSD-shaped levels is what makes the stock driver oscillate. Removing
    them also de-duplicates prompt matching — ``root@re0#`` now resolves to
    ``configuration`` and nothing else.
    """
    from scrapli.driver.network.base_driver import PrivilegeLevel

    return {
        "shell": PrivilegeLevel(
            pattern=_EVO_SHELL_PRIV_PATTERN,
            name="shell",
            previous_priv="",  # the console starts here — this is the base level
            deescalate="",
            escalate="",
            escalate_auth=False,
            escalate_prompt="",
        ),
        "exec": PrivilegeLevel(
            pattern=_JUNOS_EXEC_PATTERN,
            name="exec",
            previous_priv="shell",
            deescalate="exit",  # cli → Linux shell
            escalate="cli",  # Linux shell → cli
            escalate_auth=False,
            escalate_prompt="",
        ),
        "configuration": PrivilegeLevel(
            pattern=_JUNOS_CONFIG_PATTERN,
            name="configuration",
            previous_priv="exec",
            deescalate="exit configuration-mode",
            escalate="configure",
            escalate_auth=False,
            escalate_prompt="",
        ),
    }


async def junos_evo_on_open(conn: AsyncNetworkDriver) -> None:
    """Reach the CLI and turn paging off.

    Same shape as scrapli's junos on_open. It is safe to run against a console
    whose first prompt read was stale (EVE's proxy replays screen content on
    attach): ``acquire_priv`` re-reads the prompt every round, and both
    escalations are non-destructive if we were already there — ``cli`` at a CLI
    prompt is a plain "unknown command", not a state change.
    """
    await conn.acquire_priv(desired_priv=conn.default_desired_privilege_level)
    for cmd in (
        "set cli screen-length 0",
        "set cli screen-width 511",
        "set cli complete-on-space off",
    ):
        await conn.send_command(command=cmd)


async def junos_evo_on_close(conn: AsyncNetworkDriver) -> None:
    """Leave the tty in the Junos CLI.

    Unlike scrapli's junos on_close this does **not** send ``exit``: on Evolved
    that drops to the Linux shell, and EVE-NG consoles persist between
    connections — the next session would find a shell where it expected a CLI.
    """
    await conn.acquire_priv(desired_priv=conn.default_desired_privilege_level)


def async_driver(
    platform: str,
    host: str,
    port: int,
    timeout_ops: float = 60,
) -> AsyncNetworkDriver:
    """Build an (unconnected) async scrapli driver for ``platform``.

    ``auth_bypass`` is always on: the caller has already walked the login on
    the persistent console session in a separate connection.
    """
    common = {
        "host": host,
        "port": port,
        "transport": "asynctelnet",
        "auth_bypass": True,
        "timeout_socket": 15,
        "timeout_transport": 30,
        "timeout_ops": timeout_ops,
    }

    if platform == JUNOS_EVO_PLATFORM:
        from scrapli.driver.core.juniper_junos.async_driver import AsyncJunosDriver
        from scrapli.driver.core.juniper_junos.base_driver import FAILED_WHEN_CONTAINS

        return AsyncJunosDriver(
            privilege_levels=junos_evo_privilege_levels(),
            default_desired_privilege_level="exec",
            on_open=junos_evo_on_open,
            on_close=junos_evo_on_close,
            failed_when_contains=list(FAILED_WHEN_CONTAINS),
            **common,  # type: ignore[arg-type]
        )

    from scrapli import AsyncScrapli

    return AsyncScrapli(platform=platform, **common)  # type: ignore[arg-type]
