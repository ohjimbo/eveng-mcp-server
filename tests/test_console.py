"""Console login walk — state recognition and the settle-drain seed.

These exercise the pure pieces of `eveng_mcp.console`: no sockets, no lab.
"""

from __future__ import annotations

import re

import pytest

from eveng_mcp.console import _JUNOS_SHELL_RX, _STATE_RX, TelnetConsole
from tests.test_drivers import EVO_SHELL_PROMPTS


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Amnesiac (ttyu0)\n\nlogin: ", True),
        ("Password: ", True),
        ("root@re0> ", True),
        ("root@re0:~# ", True),
        ("root% ", True),
        ("[vrf:none] root@re0:~# ", True),
        ("...booting, no prompt yet\n", False),
    ],
)
def test_state_rx_recognises_console_states(output: str, expected: bool):
    assert bool(re.search(_STATE_RX, output)) is expected


@pytest.mark.parametrize("prompt", EVO_SHELL_PROMPTS)
def test_evolved_shell_is_recognised_as_a_junos_shell(prompt: str):
    """...so the login walk types `cli` instead of leaving the tty in Linux."""
    assert re.search(_STATE_RX, prompt)
    assert re.search(_JUNOS_SHELL_RX, prompt)


def test_last_login_banner_is_not_a_login_prompt():
    banner = "Last login: Mon Aug 25 09:12:01 2026 on ttyu0\n"
    assert not re.search(r"(?<!Last )[Ll]ogin: ?$", banner.rstrip("\n"))


@pytest.mark.parametrize("prompt", ["root@re0> ", "root@re0# "])
def test_junos_cli_prompts_are_not_treated_as_shells(prompt: str):
    assert not re.search(_JUNOS_SHELL_RX, prompt)


async def test_expect_matches_a_seeded_buffer_without_reading():
    """The settle-drain may already hold the current prompt; a console sitting
    at a prompt sends nothing more, so `expect` must not wait for it."""
    con = TelnetConsole("127.0.0.1", 65000)
    con._reader = object()  # never touched: the seed matches first
    result = await con.expect(_STATE_RX, timeout=30, initial="[vrf:none] root@re0:~# ")
    assert result["matched"] is True
    assert result["output"].endswith("root@re0:~# ")
