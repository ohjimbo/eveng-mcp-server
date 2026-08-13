"""Shared plumbing for tool registration.

- ``tool_errors`` converts expected failures into clean {"error": ...} payloads
  so the model sees an actionable message instead of a stack trace.
- ``make_tool`` builds a decorator that tags every tool read/write and skips
  registering write tools entirely when the server runs read-only — an
  unregistered tool is an honest tool list.
"""

from __future__ import annotations

import functools
from collections.abc import Callable

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
            return mcp.tool(tags=tags)(tool_errors(fn))

        return decorator

    return tool
