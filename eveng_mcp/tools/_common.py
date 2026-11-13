"""Shared plumbing for tool registration.

- ``tool_errors`` converts expected failures into clean {"error": ...} payloads
  so the model sees an actionable message instead of a stack trace.
- ``make_tool`` builds a decorator that tags every tool read/write and skips
  registering write tools entirely when the server runs read-only — an
  unregistered tool is an honest tool list.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from eveng_mcp.client import EvengError
from eveng_mcp.config import Config


def tool_errors(fn: Callable) -> Callable:
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except EvengError as e:
            return {"error": str(e)}
        except httpx.HTTPError as e:
            return {"error": f"HTTP error: {type(e).__name__}: {e}"}

    return wrapper


def make_tool(mcp, cfg: Config):
    """Returns a decorator: @tool() for read tools, @tool(write=True) for
    write tools (optionally destructive=True). Write tools are not registered
    at all in read-only mode."""

    def tool(*, write: bool = False, destructive: bool = False):
        def decorator(fn: Callable):
            if write and cfg.read_only:
                return fn  # honest tool list: not registered, not just blocked
            tags = {"write"} if write else {"read"}
            if destructive:
                tags.add("destructive")
            # Mirror the tags into MCP-standard tool annotations so any
            # client (not just ones reading our tags) can rank risk.
            annotations = {
                "readOnlyHint": not write,
                "destructiveHint": destructive,
            }
            return mcp.tool(tags=tags, annotations=annotations)(tool_errors(fn))

        return decorator

    return tool


# ── bounded fan-out ─────────────────────────────────────────────────────────
#
# MCP clients give a tool call ~60s and then walk away — but the server keeps
# running it, and a second call then fights the first for the same consoles
# (seen 2026-09-29 and 2026-11-13: zombie backups logging each other out).
# So multi-node tools fan out concurrently AND stop at a wall-clock budget,
# returning partial results plus an `incomplete` flag the caller can act on.

DEFAULT_TIME_BUDGET = 50.0  # seconds; under the common 60s client timeout
DEFAULT_CONCURRENCY = 6


def fanout_limits(cfg: Config) -> tuple[int, float]:
    """(concurrency, time budget) from CONSOLE_CONCURRENCY / TOOL_TIME_BUDGET."""
    try:
        conc = int(cfg.env("CONSOLE_CONCURRENCY") or DEFAULT_CONCURRENCY)
    except ValueError:
        conc = DEFAULT_CONCURRENCY
    try:
        budget = float(cfg.env("TOOL_TIME_BUDGET") or DEFAULT_TIME_BUDGET)
    except ValueError:
        budget = DEFAULT_TIME_BUDGET
    return max(1, conc), max(5.0, budget)


async def gather_bounded(
    items: list[Any],
    fn: Callable[[Any], Awaitable[Any]],
    concurrency: int = DEFAULT_CONCURRENCY,
    budget: float = DEFAULT_TIME_BUDGET,
) -> tuple[list[tuple[Any, Any]], list[Any]]:
    """Run ``fn(item)`` for every item, at most ``concurrency`` at once, and
    stop at ``budget`` seconds. Returns (finished, unfinished) where finished
    is [(item, result_or_exception)] and unfinished the items still running
    or never started — those are cancelled, so no zombie console sessions."""
    sem = asyncio.Semaphore(concurrency)

    async def run(item):
        async with sem:
            return await fn(item)

    tasks = {asyncio.create_task(run(item)): item for item in items}
    if not tasks:
        return [], []
    done, pending = await asyncio.wait(tasks, timeout=budget)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    finished = []
    for t in done:
        exc = t.exception()
        finished.append((tasks[t], exc if exc is not None else t.result()))
    # preserve input order for stable output
    order = {id(item): i for i, item in enumerate(items)}
    finished.sort(key=lambda pair: order[id(pair[0])])
    unfinished = sorted((tasks[t] for t in pending), key=lambda item: order[id(item)])
    return finished, unfinished
