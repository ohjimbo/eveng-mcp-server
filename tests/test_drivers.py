"""Custom scrapli platforms — Junos Evolved console privilege handling.

Regression cover for a live defect: tier-1 console tools against a `vjunosevo`
node (image vjunosevo-25.4R1) failed with

    ScrapliPrivilegeError: Failed to acquire requested privilege level configuration

because scrapli's stock juniper_junos platform treats a root shell as a level
*above* the CLI (leave it with `exit`), while Junos Evolved's console *starts*
in a Linux shell that `exit` logs you out of.

No lab is touched: scrapli drivers do not connect at construction time, so the
privilege graph and its transitions are inspectable offline.
"""

from __future__ import annotations

import re

import pytest

from eveng_mcp.drivers import (
    JUNOS_EVO_PLATFORM,
    JUNOS_EVO_SHELL_PROMPT,
    async_driver,
    junos_evo_privilege_levels,
)

# First entry is verbatim from the vjunosevo-25.4R1 console; the rest exercise
# tolerance for other vrf names, users, hostnames and working directories.
EVO_SHELL_PROMPTS = [
    "[vrf:none] root@re0:~#",
    "[vrf:none] root@re0:~# ",
    "[vrf:mgmt_junos] lab@ptx-a:~#",
    "[vrf:none] root@spine1:/var/log# ",
]

JUNOS_CLI_PROMPTS = ["root@re0>", "root@re0# ", "lab@vsrx>", "lab@ex4400#"]


def _stock_junos_privs() -> dict:
    from scrapli.driver.core.juniper_junos.base_driver import PRIVS

    return PRIVS


# ── prompt recognition ──────────────────────────────────────────────────────


@pytest.mark.parametrize("prompt", EVO_SHELL_PROMPTS)
def test_evo_shell_prompt_matches(prompt: str):
    assert re.search(JUNOS_EVO_SHELL_PROMPT, prompt)


@pytest.mark.parametrize("prompt", JUNOS_CLI_PROMPTS)
def test_evo_shell_prompt_ignores_cli_prompts(prompt: str):
    """The shell pattern must not claim the operational/config prompts, or the
    driver would try to escalate out of a CLI it is already in."""
    assert not re.search(JUNOS_EVO_SHELL_PROMPT, prompt)


def test_stock_junos_root_shell_claims_the_evolved_prompt():
    """The defect itself: scrapli core sees the Evolved shell as `root_shell`,
    whose deescalation is `exit` — a logout on Evolved, not a step into the
    CLI. Documented here so a scrapli upgrade that fixes it is noticed."""
    root_shell = _stock_junos_privs()["root_shell"]
    assert re.search(root_shell.pattern, EVO_SHELL_PROMPTS[0], flags=re.M | re.I)
    assert root_shell.deescalate == "exit"
    assert root_shell.previous_priv == "exec"  # i.e. scrapli thinks shell > cli


# ── privilege graph ─────────────────────────────────────────────────────────


def test_evo_privilege_graph_shape():
    privs = junos_evo_privilege_levels()
    assert set(privs) == {"shell", "exec", "configuration"}

    # the Linux shell is the BASE level — nothing to deescalate to
    assert privs["shell"].previous_priv == ""
    assert privs["shell"].deescalate == ""

    # ...and the CLI is reached by escalating into it
    assert privs["exec"].previous_priv == "shell"
    assert privs["exec"].escalate == "cli"
    assert privs["exec"].deescalate == "exit"

    assert privs["configuration"].previous_priv == "exec"
    assert privs["configuration"].escalate == "configure"
    assert privs["configuration"].deescalate == "exit configuration-mode"


def test_config_prompt_resolves_unambiguously():
    """Dropping scrapli's shell/root_shell levels also removes the overlap that
    let `root@re0#` match both `configuration` and `root_shell`."""
    matching = [
        name
        for name, priv in junos_evo_privilege_levels().items()
        if re.search(priv.pattern, "root@re0# ", flags=re.M | re.I)
    ]
    assert matching == ["configuration"]


# ── transitions, driven through scrapli's own resolver ──────────────────────


def _evo_driver():
    return async_driver(JUNOS_EVO_PLATFORM, "127.0.0.1", 65000)


@pytest.mark.parametrize(
    ("prompt", "destination", "expected_action", "expected_input"),
    [
        (EVO_SHELL_PROMPTS[0], "exec", "escalate", "cli"),
        (EVO_SHELL_PROMPTS[0], "configuration", "escalate", "cli"),
        ("root@re0>", "configuration", "escalate", "configure"),
        ("root@re0# ", "exec", "deescalate", "exit configuration-mode"),
    ],
)
def test_evo_priv_transitions(prompt, destination, expected_action, expected_input):
    from scrapli.driver.network.base_driver import PrivilegeAction

    action, priv = _evo_driver()._process_acquire_priv(
        destination_priv=destination, current_prompt=prompt
    )
    assert action == PrivilegeAction(expected_action)
    got = priv.escalate if expected_action == "escalate" else priv.deescalate
    assert got == expected_input


def test_stock_junos_driver_would_log_the_console_out():
    """Contrast case — the same prompt on the stock platform deescalates with
    `exit`, which is what produced the privilege-level loop in the field."""
    from scrapli.driver.network.base_driver import PrivilegeAction

    conn = async_driver("juniper_junos", "127.0.0.1", 65000)
    action, priv = conn._process_acquire_priv(
        destination_priv="configuration", current_prompt=EVO_SHELL_PROMPTS[0]
    )
    assert action == PrivilegeAction.DEESCALATE
    assert priv.name == "root_shell"
    assert priv.deescalate == "exit"


def test_classic_junos_still_uses_the_stock_platform():
    conn = async_driver("juniper_junos", "127.0.0.1", 65000)
    assert "root_shell" in conn.privilege_levels
    assert conn.default_desired_privilege_level == "exec"


def test_evo_driver_defaults():
    conn = _evo_driver()
    assert conn.default_desired_privilege_level == "exec"
    assert conn.auth_bypass is True
    # commit semantics still come from platforms.yaml (config_commit), not here
    assert "syntax error" in conn.failed_when_contains
