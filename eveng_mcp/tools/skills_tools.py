"""Skills — markdown runbooks served through skills_list / skills_load.

Orchestration lives here, not in Python: a skill can adapt when a step fails,
skip steps whose integration isn't configured, and be edited without a
redeploy. skills_list filters to skills whose required integrations are
actually enabled, so the model never loads a runbook that dead-ends.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from eveng_mcp.config import Config
from eveng_mcp.tools._common import make_tool

_SKILLS_DIR = Path(__file__).parent.parent / "skills"


def _parse_skill(path: Path) -> dict | None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    try:
        _, fm, body = text.split("---", 2)
        meta = yaml.safe_load(fm) or {}
    except (ValueError, yaml.YAMLError):
        return None
    meta.setdefault("name", path.stem)
    meta["_body"] = body.strip()
    meta.setdefault("requires_integrations", [])
    return meta


def _load_all() -> list[dict]:
    skills = []
    if _SKILLS_DIR.is_dir():
        for p in sorted(_SKILLS_DIR.glob("*.md")):
            meta = _parse_skill(p)
            if meta:
                skills.append(meta)
    return skills


def register(mcp, cfg: Config, enabled_integrations: set[str]) -> None:
    tool = make_tool(mcp, cfg)

    @tool()
    async def skills_list() -> dict:
        """List available skills — vetted multi-step runbooks (lab teardown,
        NetBox fabric build, pristine reset, mgmt bootstrap, drift check).
        Call this FIRST for any multi-step request; if a skill matches, load
        it with skills_load and follow it instead of improvising. Skills whose
        required integrations aren't configured are listed as not runnable.
        """
        out = []
        for s in _load_all():
            required = set(s.get("requires_integrations", []))
            missing = sorted(required - enabled_integrations)
            out.append({
                "name": s["name"],
                "title": s.get("title", s["name"]),
                "description": s.get("description", ""),
                "requires_integrations": sorted(required),
                "runnable": not missing,
                "missing_integrations": missing,
                "load_with": f"skills_load(name='{s['name']}')",
            })
        return {"count": len(out), "skills": out,
                "next_step": "If a skill matches the request, call skills_load "
                             "and follow its steps — the body is the procedure."}

    @tool()
    async def skills_load(name: str) -> dict:
        """Load one skill's full runbook body. Follow every step, including
        its output format.

        Args:
            name: skill name from skills_list.
        """
        for s in _load_all():
            if s["name"] == name:
                required = set(s.get("requires_integrations", []))
                missing = sorted(required - enabled_integrations)
                return {
                    "name": name,
                    "title": s.get("title", name),
                    "missing_integrations": missing,
                    "warning": (f"Integrations not configured: {missing} — the "
                                "marked steps will be unavailable" if missing else None),
                    "body": s["_body"],
                }
        return {"error": f"Unknown skill: {name!r}",
                "available": [s["name"] for s in _load_all()]}
