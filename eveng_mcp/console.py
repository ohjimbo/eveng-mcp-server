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


def _scrapli_conn(
    platform: str,
    host: str,
    port: int,
    username: str = "",
    password: str = "",
    timeout_ops: float = 60,
):
    from scrapli import AsyncScrapli

    kwargs: dict = {
        "platform": platform,
        "host": host,
        "port": port,
        "transport": "asynctelnet",
        "timeout_socket": 15,
        "timeout_transport": 30,
        "timeout_ops": timeout_ops,
    }
    if username or password:
        kwargs["auth_username"] = username
        kwargs["auth_password"] = password
    else:
        # No creds: assume the console is already at a prompt (common for
        # lab images) and skip scrapli's auth phase entirely.
        kwargs["auth_bypass"] = True
    return AsyncScrapli(**kwargs)


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
    over the node's telnet console. Returns per-command results."""
    conn = _scrapli_conn(platform, host, port, username, password, timeout_ops)
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
