"""Console access to EVE-NG nodes — vendor-agnostic, three tiers.

Every EVE-NG node exposes its serial console as a telnet port on the host;
the transport is identical for every vendor. What differs is semantics:

  tier 1 — scrapli driver (prompt handling, paging, config mode, commit)
  tier 2 — generic expect (caller supplies send/expect patterns)
  tier 3 — raw read/write (boot watching, bootloaders, anything)

Sessions are stateless: open → act → close per call. EVE-NG consoles are
EXCLUSIVE — while we hold one, the web UI can't open it, and vice versa —
so short-lived connections are a feature, and "busy" is surfaced as a
distinct error rather than a timeout.
"""

from __future__ import annotations

import asyncio
import re
import time

from eveng_mcp.client import EvengError

# telnet protocol bytes
_IAC, _DONT, _DO, _WONT, _WILL, _SB, _SE = 255, 254, 253, 252, 251, 250, 240


class ConsoleError(EvengError):
    pass


class ConsoleBusy(ConsoleError):
    """The console port closed on us immediately — almost always another
    client (EVE-NG web UI, another tool call) holding the exclusive session."""


class _TelnetFilter:
    """Minimal telnet negotiation: strip IAC sequences from the byte stream
    and refuse every option (DO→WONT, WILL→DONT). Robust across chunk splits."""

    def __init__(self) -> None:
        self._state = 0  # 0=data, 1=IAC, 2=IAC+cmd(awaiting option), 3=subneg, 4=subneg+IAC
        self._cmd = 0

    def feed(self, data: bytes) -> tuple[bytes, bytes]:
        clean = bytearray()
        reply = bytearray()
        for b in data:
            if self._state == 0:
                if b == _IAC:
                    self._state = 1
                else:
                    clean.append(b)
            elif self._state == 1:
                if b == _IAC:  # escaped 0xFF data byte
                    clean.append(b)
                    self._state = 0
                elif b in (_DO, _DONT, _WILL, _WONT):
                    self._cmd = b
                    self._state = 2
                elif b == _SB:
                    self._state = 3
                else:
                    self._state = 0
            elif self._state == 2:
                if self._cmd == _DO:
                    reply.extend((_IAC, _WONT, b))
                elif self._cmd == _WILL:
                    reply.extend((_IAC, _DONT, b))
                self._state = 0
            elif self._state == 3:
                if b == _IAC:
                    self._state = 4
            elif self._state == 4:
                self._state = 0 if b == _SE else 3
        return bytes(clean), bytes(reply)


class TelnetConsole:
    """Tier 2/3 console session — plain asyncio TCP with telnet filtering."""

    def __init__(self, host: str, port: int, connect_timeout: float = 10):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._filter = _TelnetFilter()
        # Session-scoped: once ANY data has arrived, a later EOF means the
        # node died/console closed — never "held by another client".
        self._got_data = False

    async def __aenter__(self) -> TelnetConsole:
        await self.open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def open(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.connect_timeout,
            )
        except ConnectionRefusedError as e:
            raise ConsoleError(
                f"Console {self.host}:{self.port} refused connection — node is "
                "stopped, still booting, or the port is not a telnet console"
            ) from e
        except (TimeoutError, OSError) as e:
            raise ConsoleError(f"Console {self.host}:{self.port} unreachable: {e}") from e

    async def close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    async def write(self, data: str) -> None:
        assert self._writer is not None, "console not open"
        raw = data.encode("utf-8").replace(b"\xff", b"\xff\xff")
        self._writer.write(raw)
        await self._writer.drain()

    async def read(self, duration: float = 2.0, max_bytes: int = 65536) -> str:
        """Drain whatever arrives within ``duration`` seconds."""
        assert self._reader is not None, "console not open"
        buf = bytearray()
        deadline = time.monotonic() + duration
        started = time.monotonic()
        while len(buf) < max_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(self._reader.read(4096), timeout=remaining)
            except TimeoutError:
                break
            if not chunk:
                # EOF right after connect = someone else holds the console
                if time.monotonic() - started < 2 and not buf and not self._got_data:
                    raise ConsoleBusy(
                        f"Console {self.host}:{self.port} closed immediately — "
                        "held by another client (EVE-NG web UI or another session)"
                    )
                break
            self._got_data = True
            clean, reply = self._filter.feed(chunk)
            buf.extend(clean)
            if reply:
                self._writer.write(reply)  # type: ignore[union-attr]
                await self._writer.drain()  # type: ignore[union-attr]
        return buf.decode("utf-8", errors="replace")

    async def expect(
        self,
        pattern: str,
        timeout: float = 30,
        initial: str = "",
        keep: int | None = 8000,
    ) -> dict:
        """Read until ``pattern`` (regex, searched across the whole buffer)
        matches or timeout. ``initial`` seeds the match buffer so a caller
        retrying in slices can span a slice boundary. ``keep`` caps the
        returned output to its last N chars (None = whole buffer — needed when
        the output IS the payload, e.g. a config capture).
        Returns {matched, output}."""
        assert self._reader is not None, "console not open"
        rx = re.compile(pattern)
        buf = initial
        deadline = time.monotonic() + timeout
        started = time.monotonic()

        def _tail(s: str) -> str:
            return s if keep is None else s[-keep:]

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"matched": False, "pattern": pattern, "output": _tail(buf)}
            try:
                chunk = await asyncio.wait_for(self._reader.read(4096), timeout=remaining)
            except TimeoutError:
                return {"matched": False, "pattern": pattern, "output": _tail(buf)}
            if not chunk:
                if (time.monotonic() - started < 2 and buf == initial
                        and not self._got_data):
                    raise ConsoleBusy(
                        f"Console {self.host}:{self.port} closed immediately — "
                        "held by another client"
                    )
                return {"matched": False, "pattern": pattern, "output": _tail(buf),
                        "eof": True}
            self._got_data = True
            clean, reply = self._filter.feed(chunk)
            buf += clean.decode("utf-8", errors="replace")
            if reply:
                self._writer.write(reply)  # type: ignore[union-attr]
                await self._writer.drain()  # type: ignore[union-attr]
            if rx.search(buf):
                return {"matched": True, "pattern": pattern, "output": _tail(buf)}


# ── login walk (shared by tier 1 and the prompt-anchored helpers) ──────────

class ConsoleAuthError(ConsoleError):
    """The console rejected our credentials, or its login state is desynced
    from what we see (e.g. our password was echoed in clear). Retrying
    immediately only adds login-backoff failures — callers must NOT retry."""


class ConsoleHalted(ConsoleError):
    """The console shows a halted OS. On Junos/FreeBSD ANY keypress reboots
    it, so we refuse to touch the tty."""


# '(?<!Last )login:' avoids matching a chunk that happens to end at Junos's
# "Last login: ..." banner line.
_LOGIN_RX = r"(?<!Last )[Ll]ogin: ?$"
_PASS_RX = r"[Pp]assword: ?$"
_JUNOS_SHELL_RX = r"(:~ ?# |% )$"
_PROMPT_RX = r"(:~ ?# |% |> |# |\$ )$"
# What a halted/powered-off guest leaves on its console. Shared default for
# platforms.yaml `halt_done`; also what the login walk refuses to type over.
HALTED_RX = (
    r"(press any key to reboot|FINAL System shutdown|operating system has halted"
    r"|Powering system off|reboot: Power down|System halted)"
)
_STATE_RX = f"({_LOGIN_RX}|{_PASS_RX}|{_PROMPT_RX}|Login incorrect|{HALTED_RX})"


async def _login_walk(
    con: TelnetConsole, username: str, password: str, platform: str | None,
    state_timeout: float = 20,
) -> str:
    """Drive an OPEN console to an operational prompt and return that prompt
    line (e.g. 'root@r1>'). EVE-NG console sessions persist across telnet
    connections and any prompt was printed BEFORE we connected, so: drain
    the replayed screen, nudge, then walk login/password/shell states.

    Refuses to type over a halted guest (ConsoleHalted) and gives up after
    two rejected logins (ConsoleAuthError) instead of feeding FreeBSD's
    login backoff — which is exactly how a tty ends up echoing passwords."""
    seen = await con.read(duration=0.4)  # settle: EVE proxy replays the screen
    if re.search(HALTED_RX, seen):
        raise ConsoleHalted(
            f"Console {con.host}:{con.port} shows a halted OS "
            f"({re.search(HALTED_RX, seen).group(0)!r}) — not sending anything, a "
            "keypress would reboot it. Use eveng_stop_nodes."
        )
    await con.write("\r")
    failures = 0
    for _ in range(8):  # bounded state walk
        r = await con.expect(_STATE_RX, timeout=state_timeout)
        out = r["output"]
        if not r["matched"]:
            if password and password in out:
                raise ConsoleAuthError(
                    f"Console {con.host}:{con.port}: the password was echoed in "
                    "clear — the tty was NOT at a password prompt (login backoff "
                    "after repeated failures, or another client is driving this "
                    "console). Wait ~60s before retrying."
                )
            raise ConsoleError(
                f"Console {con.host}:{con.port}: no recognizable prompt after nudge; "
                f"tail: {out[-200:]!r}"
            )
        if re.search(HALTED_RX, out):
            raise ConsoleHalted(
                f"Console {con.host}:{con.port} shows a halted OS — refusing to "
                "send keys (would reboot). Use eveng_stop_nodes."
            )
        if "Login incorrect" in out:
            failures += 1
            if failures >= 2:
                raise ConsoleAuthError(
                    f"Console {con.host}:{con.port}: login rejected twice for "
                    f"user {username!r} — wrong CONSOLE_USER/PASS, or the tty "
                    "login state is desynced (another client typing). Not "
                    "retrying: FreeBSD login backoff grows with each failure."
                )
        # The buffer may hold 'Login incorrect' AND the next prompt in one
        # chunk, so classify on the tail regardless of what matched first.
        if re.search(_LOGIN_RX, out):
            if not username:
                raise ConsoleAuthError(
                    "Console shows a login prompt but no credentials were "
                    "given — pass username/password or set CONSOLE_USER/PASS"
                )
            await con.write(username + "\r")
            continue
        if re.search(_PASS_RX, out):
            await con.write((password or "") + "\r")
            continue
        if platform == "juniper_junos" and re.search(_JUNOS_SHELL_RX, out):
            await con.write("cli\r")  # root shell → operational cli
            continue
        if re.search(_PROMPT_RX, out):
            lines = [ln for ln in out.replace("\r", "").split("\n") if ln.strip()]
            return lines[-1].strip() if lines else ""
        # only 'Login incorrect' so far — the next prompt is still coming
    raise ConsoleError(
        f"Console {con.host}:{con.port}: login loop did not reach a prompt — "
        "wrong credentials, or tty left in an unexpected mode"
    )


async def login_console(
    host: str, port: int, username: str, password: str, platform: str | None = None
) -> tuple[TelnetConsole, str]:
    """Open a console and walk it to an operational prompt. Returns the OPEN
    console and the prompt line; the caller owns closing it."""
    con = TelnetConsole(host, port)
    await con.open()
    try:
        prompt = await _login_walk(con, username, password, platform)
    except BaseException:
        await con.close()
        raise
    return con, prompt


async def _ensure_logged_in(
    host: str, port: int, username: str, password: str, platform: str
) -> None:
    """Bring the console tty to an operational prompt before scrapli takes
    over (scrapli's own telnet auth passively waits for a prompt that was
    printed before we connected, so it times out on a silent console)."""
    con, _ = await login_console(host, port, username, password, platform)
    await con.close()


async def run_at_prompt(
    con: TelnetConsole, prompt: str, command: str, timeout: float = 30
) -> str:
    """Send one command and read until the LITERAL prompt returns. Anchoring
    on the exact prompt (not a vendor regex) is what stops a buffered extra
    prompt from ending the read early — the cause of truncated captures.
    Returns the output with the command echo and trailing prompt removed."""
    await con.write(command + "\r")
    r = await con.expect(re.escape(prompt) + r" ?$", timeout=timeout, keep=None)
    if not r["matched"]:
        raise ConsoleError(
            f"Console {con.host}:{con.port}: no prompt {prompt!r} within {timeout}s "
            f"after {command!r}; tail: {r['output'][-200:]!r}"
        )
    lines = r["output"].replace("\r", "").split("\n")
    # echo of the command (possibly preceded by the prompt we were sitting at)
    while lines and (command in lines[0] or lines[0].strip() in ("", prompt)):
        lines.pop(0)
    while lines and lines[-1].strip() in ("", prompt):
        lines.pop()
    return "\n".join(lines)


async def capture_config(
    info, host: str, port: int, username: str, password: str, timeout: float = 45
) -> dict:
    """Pull a node's config over its console, prompt-anchored, and — when the
    platform has a `show_config_count` command — verify the line count so a
    truncated capture is REPORTED, never silently committed.

    Returns {config, lines, expected_lines, verified}: verified is True/False
    when a count was available, None when the platform can't count."""
    con, prompt = await login_console(host, port, username, password, info.scrapli_platform)
    try:
        if info.paging_off:
            await run_at_prompt(con, prompt, info.paging_off, timeout=10)
        expected: int | None = None
        if info.show_config_count:
            out = await run_at_prompt(con, prompt, info.show_config_count, timeout=20)
            m = re.search(r"(\d+)", out)
            expected = int(m.group(1)) if m else None
        config = await run_at_prompt(con, prompt, info.show_config, timeout=timeout)
    finally:
        await con.close()
    line_rx = re.compile(info.config_line_regex or r"\S")
    lines = sum(1 for ln in config.split("\n") if line_rx.search(ln))
    verified = None if expected is None else lines == expected
    return {"config": config, "lines": lines, "expected_lines": expected,
            "verified": verified}


async def graceful_halt(
    info, host: str, port: int, username: str, password: str, wait: float = 30
) -> dict:
    """Gracefully halt a node over its console: platform `halt` command,
    answer the confirm prompt if the platform has one, then wait for the
    halt marker. Never sends anything after the marker (Junos: 'press any
    key to reboot' means exactly that).

    Returns {halted, output, [eof], [already_halted]}."""
    if not info.halt:
        raise ConsoleError(f"template {info.template!r} has no halt command in platforms.yaml")
    done_rx = info.halt_done or HALTED_RX
    try:
        con, prompt = await login_console(
            host, port, username, password, info.scrapli_platform
        )
    except ConsoleHalted as e:
        return {"halted": True, "already_halted": True, "output": str(e)}
    try:
        await con.write(info.halt + "\r")
        if info.halt_confirm:
            r = await con.expect(
                f"({info.halt_confirm}|{done_rx}|error:[^\\r\\n]*)", timeout=15
            )
            out = r["output"]
            if not r["matched"]:
                raise ConsoleError(
                    f"no confirm prompt after {info.halt!r} within 15s; "
                    f"tail: {out[-200:]!r}"
                )
            err = re.search(r"error:[^\r\n]*", out)
            if err and not re.search(done_rx, out):
                raise ConsoleError(
                    f"halt command {info.halt!r} rejected: {err.group(0).strip()!r} — "
                    "override `halt` for this template in platforms.yaml"
                )
            if re.search(info.halt_confirm, out) and not re.search(done_rx, out):
                await con.write((info.halt_answer or "yes") + "\r")
        r = await con.expect(done_rx, timeout=wait)
        result = {"halted": bool(r["matched"]), "output": r["output"][-600:]}
        if r.get("eof"):
            # console closed mid-wait: the guest powered itself off
            result.update(halted=True, eof=True)
        return result
    finally:
        await con.close()


# ── tier 1: scrapli drivers ────────────────────────────────────────────────

def _scrapli_conn(
    platform: str,
    host: str,
    port: int,
    timeout_ops: float = 60,
):
    from scrapli import AsyncScrapli

    # auth_bypass always: _ensure_logged_in has already authenticated the
    # persistent console session in a separate connection.
    return AsyncScrapli(
        platform=platform,
        host=host,
        port=port,
        transport="asynctelnet",
        auth_bypass=True,
        timeout_socket=15,
        timeout_transport=30,
        timeout_ops=timeout_ops,
    )


async def driver_run(
    platform: str,
    host: str,
    port: int,
    commands: list[str] | None = None,
    configs: list[str] | None = None,
    username: str = "",
    password: str = "",
    timeout_ops: float = 60,
) -> dict:
    """Run operational commands and/or push config lines via a scrapli driver
    over the node's telnet console. Returns per-command results.

    One automatic retry: EVE's console proxy occasionally flakes on
    connect/first-read (seen under multi-node load), and the sessions are
    stateless so a clean second attempt is safe. Auth/halted errors are NOT
    retried — a second attempt only makes the tty state worse."""
    last_error: ConsoleError | None = None
    for attempt in (1, 2):
        try:
            return await _driver_run_once(
                platform, host, port, commands, configs, username, password,
                timeout_ops,
            )
        except (ConsoleAuthError, ConsoleHalted):
            raise
        except ConsoleError as e:
            last_error = e
            if attempt == 1:
                await asyncio.sleep(2)
    raise last_error  # type: ignore[misc]


async def _driver_run_once(
    platform: str,
    host: str,
    port: int,
    commands: list[str] | None,
    configs: list[str] | None,
    username: str,
    password: str,
    timeout_ops: float,
) -> dict:
    await _ensure_logged_in(host, port, username, password, platform)
    conn = _scrapli_conn(platform, host, port, timeout_ops)
    results: list[dict] = []
    try:
        async with conn:
            if commands:
                for cmd in commands:
                    r = await conn.send_command(cmd)
                    results.append(
                        {"command": cmd, "failed": r.failed, "output": r.result}
                    )
            if configs:
                r = await conn.send_configs(configs)
                results.append(
                    {"configs": configs, "failed": r.failed, "output": r.result}
                )
    except ConnectionRefusedError as e:
        raise ConsoleError(
            f"Console {host}:{port} refused connection — node stopped or booting"
        ) from e
    except Exception as e:  # scrapli raises its own exception zoo
        name = type(e).__name__
        raise ConsoleError(f"Driver session failed ({name}): {e}") from e
    return {"platform": platform, "results": results}
