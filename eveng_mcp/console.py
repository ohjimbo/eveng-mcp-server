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

    async def expect(self, pattern: str, timeout: float = 30, initial: str = "") -> dict:
        """Read until ``pattern`` (regex, searched across the whole buffer)
        matches or timeout. ``initial`` seeds the match buffer so a caller
        retrying in slices can span a slice boundary. Returns {matched, output}."""
        assert self._reader is not None, "console not open"
        rx = re.compile(pattern)
        buf = initial
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"matched": False, "pattern": pattern, "output": buf[-8000:]}
            try:
                chunk = await asyncio.wait_for(self._reader.read(4096), timeout=remaining)
            except TimeoutError:
                return {"matched": False, "pattern": pattern, "output": buf[-8000:]}
            if not chunk:
                if (time.monotonic() - started < 2 and buf == initial
                        and not self._got_data):
                    raise ConsoleBusy(
                        f"Console {self.host}:{self.port} closed immediately — "
                        "held by another client"
                    )
                return {"matched": False, "pattern": pattern, "output": buf[-8000:],
                        "eof": True}
            self._got_data = True
            clean, reply = self._filter.feed(chunk)
            buf += clean.decode("utf-8", errors="replace")
            if reply:
                self._writer.write(reply)  # type: ignore[union-attr]
                await self._writer.drain()  # type: ignore[union-attr]
            if rx.search(buf):
                return {"matched": True, "pattern": pattern, "output": buf[-8000:]}


# ── tier 1: scrapli drivers ────────────────────────────────────────────────

# Login-state walk for _ensure_logged_in. '(?<!Last )login:' avoids matching
# a chunk that happens to end at Junos's "Last login: ..." banner line.
_STATE_RX = (
    r"((?<!Last )[Ll]ogin: ?$|[Pp]assword: ?$|:~ ?# $|% $|> $|# $)"
)


async def _ensure_logged_in(
    host: str, port: int, username: str, password: str, platform: str
) -> None:
    """Bring the console tty to an operational prompt before scrapli takes
    over. EVE-NG console sessions persist across telnet connections, and any
    login prompt was printed BEFORE we connected — scrapli's own telnet auth
    (which passively waits for a prompt) times out on a silent console. So:
    nudge, walk the login/password states ourselves, and for Junos step from
    the root shell into the cli."""
    con = TelnetConsole(host, port)
    await con.open()
    try:
        # settle: some EVE console backends drop bytes written during proxy
        # attach; drain the replayed screen before nudging
        await con.read(duration=0.4)
        await con.write("\r")
        for _ in range(6):  # bounded state walk
            r = await con.expect(_STATE_RX, timeout=20)
            if not r["matched"]:
                raise ConsoleError(
                    f"Console {host}:{port}: no recognizable prompt after nudge; "
                    f"tail: {r['output'][-200:]!r}"
                )
            out = r["output"]
            if re.search(r"(?<!Last )[Ll]ogin: ?$", out):
                if not username:
                    raise ConsoleError(
                        "Console shows a login prompt but no credentials were "
                        "given — pass username/password or set CONSOLE_USER/PASS"
                    )
                await con.write(username + "\r")
                continue
            if re.search(r"[Pp]assword: ?$", out):
                await con.write((password or "") + "\r")
                continue
            if re.search(r"(:~ ?# |% )$", out) and platform == "juniper_junos":
                await con.write("cli\r")  # root shell → operational cli
                continue
            return  # at an operational prompt
        raise ConsoleError(
            f"Console {host}:{port}: login loop did not reach a prompt — "
            "wrong credentials, or tty left in an unexpected mode"
        )
    finally:
        await con.close()


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
    stateless so a clean second attempt is safe."""
    last_error: ConsoleError | None = None
    for attempt in (1, 2):
        try:
            return await _driver_run_once(
                platform, host, port, commands, configs, username, password,
                timeout_ops,
            )
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
