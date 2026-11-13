"""Console login walk, prompt-anchored capture and graceful halt against a
scripted fake console — the failure modes seen on real EVE-NG consoles
(stale password prompt, login backoff echoing the password, truncated
captures, halted guests) are reproduced here so they stay fixed."""

from __future__ import annotations

import asyncio

import pytest

from eveng_mcp.console import (
    ConsoleAuthError,
    ConsoleError,
    ConsoleHalted,
    capture_config,
    graceful_halt,
    login_console,
)
from eveng_mcp.inventory import PlatformMap

CONFIG = ["set system host-name r1", "set interfaces ge-0/0/0 unit 0", "set protocols lldp"]


class FakeJunosConsole:
    """A persistent-tty Junos lookalike: login → Password → shell → cli.

    ``state`` is the tty state the console is sitting in when a client
    connects (nothing is printed on connect — EVE-NG consoles are silent
    until nudged). ``replay`` is screen content the EVE proxy replays."""

    def __init__(self, state="login", password="Juniper1!", count=None,
                 lines=None, replay="", vsrx=False):
        self.state = state
        self.password = password
        self.count = count if count is not None else len(CONFIG)
        self.lines = lines if lines is not None else CONFIG
        self.replay = replay
        self.vsrx = vsrx
        self.failures = 0
        self.received: list[str] = []
        self.server = None
        self.port = 0

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    def _prompt(self):
        return {"shell": "root@r1:~ # ", "cli": "root@r1> ",
                "login": "login: ", "password": "Password:"}[self.state]

    async def _handle(self, reader, writer):
        if self.replay:
            writer.write(self.replay.encode())
        try:
            while True:
                data = await reader.readuntil(b"\r")
                line = data.decode(errors="replace").rstrip("\r\n")
                self.received.append(line)
                out = await self._respond(line)
                if out is None:
                    break
                writer.write(out.encode())
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()

    async def _respond(self, line: str) -> str | None:
        if self.state == "login":
            if line == "":
                return "\r\nlogin: "
            self.state = "password"
            return f"{line}\r\nPassword:"
        if self.state == "password":
            if line == self.password:
                self.failures = 0
                self.state = "shell"
                return "\r\nLast login: Thu Nov 12 on ttyu0\r\n--- JUNOS 22.4R3\r\nroot@r1:~ # "
            self.failures += 1
            self.state = "login"
            if self.failures >= 2:
                # FreeBSD login backoff: the next prompt comes much later, and
                # whatever is typed meanwhile is echoed in clear.
                await asyncio.sleep(1.5)
                return "\r\nLogin incorrect\r\n"
            return "\r\nLogin incorrect\r\nlogin: "
        if self.state == "shell":
            if line == "cli":
                self.state = "cli"
                return "cli\r\r\nroot@r1> "
            return f"{line}\r\nroot@r1:~ # "
        # cli
        echo = f"{line} \r\n"
        if line == "":
            return "\r\nroot@r1> "
        if line.startswith("show configuration | display set | count"):
            return echo + f"Count: {self.count} lines\r\n\r\nroot@r1> "
        if line.startswith("show configuration | display set"):
            body = "".join(f"{ln}\r\n" for ln in self.lines)
            return echo + body + "\r\nroot@r1> "
        if line == "request system halt":
            if self.vsrx:
                return echo + "error: command is not valid on the vsrx\r\n\r\nroot@r1> "
            self.state = "confirm"
            return echo + "Halt the system ? [yes,no] (no) "
        if line == "request system power-off":
            self.state = "confirm"
            return echo + "Power Off the system ? [yes,no] (no) "
        return echo + f"error: unknown command: {line}\r\n\r\nroot@r1> "

    async def _respond_confirm(self, line):  # pragma: no cover - folded below
        raise NotImplementedError


# fold the confirm state into _respond without a giant if-chain above
_orig_respond = FakeJunosConsole._respond


async def _respond_with_confirm(self, line):
    if self.state == "confirm":
        if line == "yes":
            self.state = "halted"
            return ("yes \r\n\r\n*** FINAL System shutdown ***\r\n"
                    "The operating system has halted.\r\n"
                    "Please press any key to reboot.\r\n")
        self.state = "cli"
        return "\r\nroot@r1> "
    if self.state == "halted":
        # a keypress reboots — the thing we must never trigger
        self.state = "login"
        return "\r\nRebooting...\r\nlogin: "
    return await _orig_respond(self, line)


FakeJunosConsole._respond = _respond_with_confirm  # type: ignore[method-assign]


@pytest.fixture
def junos_info():
    return PlatformMap().lookup("vjunosrouter")


@pytest.fixture
def vsrx_info():
    return PlatformMap().lookup("vsrxng")


async def _with_console(**kw):
    fake = FakeJunosConsole(**kw)
    await fake.start()
    return fake


async def test_login_walk_from_login_prompt_reaches_cli():
    fake = await _with_console(state="login")
    try:
        con, prompt = await login_console("127.0.0.1", fake.port, "root", "Juniper1!",
                                          "juniper_junos")
        await con.close()
        assert prompt == "root@r1>"
        assert fake.received[:3] == ["", "root", "Juniper1!"]
        assert "cli" in fake.received
    finally:
        await fake.stop()


async def test_login_walk_recovers_from_stale_password_prompt():
    """The 2026-11-13 vSRX case: tty left at 'Password:' by an earlier
    client. The nudge fails that login; the walk must then log in cleanly."""
    fake = await _with_console(state="password")
    try:
        con, prompt = await login_console("127.0.0.1", fake.port, "root", "Juniper1!",
                                          "juniper_junos")
        await con.close()
        assert prompt == "root@r1>"
        assert fake.state == "cli"
    finally:
        await fake.stop()


async def test_login_walk_gives_up_after_two_rejections():
    fake = await _with_console(state="login", password="other")
    try:
        with pytest.raises(ConsoleAuthError, match="rejected twice"):
            await login_console("127.0.0.1", fake.port, "root", "Juniper1!", "juniper_junos")
        # exactly two attempts — no credential spraying into login backoff
        assert fake.received.count("Juniper1!") == 2
    finally:
        await fake.stop()


async def test_login_walk_refuses_to_touch_halted_console():
    fake = await _with_console(
        state="halted",
        replay="*** FINAL System shutdown ***\r\nPlease press any key to reboot.\r\n",
    )
    try:
        with pytest.raises(ConsoleHalted):
            await login_console("127.0.0.1", fake.port, "root", "Juniper1!", "juniper_junos")
        assert fake.received == []  # nothing sent — it would have rebooted
    finally:
        await fake.stop()


async def test_capture_config_is_verified(junos_info):
    fake = await _with_console(state="login")
    try:
        res = await capture_config(junos_info, "127.0.0.1", fake.port, "root", "Juniper1!")
        assert res["verified"] is True
        assert res["lines"] == res["expected_lines"] == len(CONFIG)
        assert res["config"].split("\n") == CONFIG
    finally:
        await fake.stop()


async def test_capture_config_flags_truncation(junos_info):
    """Device says N lines, we got fewer — must be reported, not trusted."""
    fake = await _with_console(state="login", count=82, lines=CONFIG[:2])
    try:
        res = await capture_config(junos_info, "127.0.0.1", fake.port, "root", "Juniper1!")
        assert res["verified"] is False
        assert res["lines"] == 2 and res["expected_lines"] == 82
    finally:
        await fake.stop()


async def test_graceful_halt_confirms_and_sees_marker(junos_info):
    fake = await _with_console(state="cli")
    try:
        res = await graceful_halt(junos_info, "127.0.0.1", fake.port, "root", "Juniper1!",
                                  wait=5)
        assert res["halted"] is True
        assert fake.received[-2:] == ["request system halt", "yes"]
        assert fake.state == "halted"  # nothing typed after the marker
    finally:
        await fake.stop()


async def test_graceful_halt_uses_vsrx_power_off(vsrx_info):
    fake = await _with_console(state="cli", vsrx=True)
    try:
        res = await graceful_halt(vsrx_info, "127.0.0.1", fake.port, "root", "Juniper1!",
                                  wait=5)
        assert res["halted"] is True
        assert "request system power-off" in fake.received
        assert "request system halt" not in fake.received
    finally:
        await fake.stop()


async def test_graceful_halt_reports_rejected_command(junos_info):
    """platform says 'request system halt' but the box is a vSRX — surface
    the device's error instead of waiting for a marker that never comes."""
    fake = await _with_console(state="cli", vsrx=True)
    try:
        with pytest.raises(ConsoleError, match="rejected"):
            await graceful_halt(junos_info, "127.0.0.1", fake.port, "root", "Juniper1!",
                                wait=5)
    finally:
        await fake.stop()


async def test_graceful_halt_on_already_halted_console(junos_info):
    fake = await _with_console(state="halted",
                               replay="The operating system has halted.\r\n")
    try:
        res = await graceful_halt(junos_info, "127.0.0.1", fake.port, "root", "Juniper1!")
        assert res["halted"] is True and res["already_halted"] is True
        assert fake.received == []
    finally:
        await fake.stop()


async def test_gather_bounded_returns_partial_on_budget():
    from eveng_mcp.tools._common import gather_bounded

    async def work(n):
        await asyncio.sleep(n)
        return n

    finished, unfinished = await gather_bounded([0.01, 5.0, 0.02], work, concurrency=3,
                                                budget=0.5)
    assert [r for _, r in finished] == [0.01, 0.02]
    assert unfinished == [5.0]
