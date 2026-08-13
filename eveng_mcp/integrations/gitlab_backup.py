"""GitLab backup integration — version running node configs into a repo.

Pulls each running, driver-capable node's config over its console and commits
the lot as ONE commit: <lab-name>/configs/<node>.conf on the project's
default branch.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from eveng_mcp.console import ConsoleError, driver_run
from eveng_mcp.inventory import build_inventory
from eveng_mcp.tools._common import make_tool

REQUIRES = ("GITLAB_URL", "GITLAB_TOKEN", "GITLAB_BACKUP_PROJECT")
SUMMARY = "Back up running node configs (via console) to a GitLab project"


def register(mcp, cfg, client, platforms) -> None:
    tool = make_tool(mcp, cfg)
    gitlab_url = cfg.env("GITLAB_URL").rstrip("/")
    token = cfg.env("GITLAB_TOKEN")
    project = quote(cfg.env("GITLAB_BACKUP_PROJECT"), safe="")

    async def _gl(method: str, path: str, body: dict | None = None) -> httpx.Response:
        async with httpx.AsyncClient(
            headers={"PRIVATE-TOKEN": token}, timeout=30
        ) as c:
            return await c.request(
                method, f"{gitlab_url}/api/v4/projects/{project}{path}", json=body
            )

    @tool(write=True)
    async def eveng_backup_configs(
        lab_path: str = "", username: str = "", password: str = ""
    ) -> dict:
        """Back up running node configurations to GitLab. Consoles into every
        running tier-1 node, captures its config (platform show_config
        command), and commits everything as one commit under
        <lab-name>/configs/<node>.conf. Nodes without a driver or without a
        show_config mapping are reported, not silently skipped.

        Args:
            lab_path: one lab like '/MyLab.unl' (empty = ALL labs with
                running nodes; slower).
            username/password: console login (default: CONSOLE_USER/PASS env).
        """
        user = username or cfg.console_user
        pwd = password or cfg.console_pass

        inv = await build_inventory(
            client, platforms, lab_path=lab_path or None, running_only=True
        )
        captured: list[dict] = []
        skipped: list[dict] = []

        for dev in inv["devices"]:
            info = platforms.lookup(dev["template"])
            if not info or not info.scrapli_platform or not info.show_config:
                skipped.append(
                    {"node": dev["name"], "lab": dev["lab_path"],
                     "reason": "no driver/show_config mapping (console tier "
                               f"{dev['console_tier']})"}
                )
                continue
            if dev["console"]["protocol"] != "telnet" or not dev["console"]["port"]:
                skipped.append({"node": dev["name"], "lab": dev["lab_path"],
                                "reason": "no telnet console"})
                continue
            try:
                result = await driver_run(
                    info.scrapli_platform,
                    dev["console"]["host"], dev["console"]["port"],
                    commands=[info.show_config], username=user, password=pwd,
                )
                out = result["results"][0]
                if out.get("failed"):
                    skipped.append({"node": dev["name"], "lab": dev["lab_path"],
                                    "reason": f"command failed: {out.get('output', '')[:200]}"})
                    continue
                lab_name = dev["lab_path"].strip("/").removesuffix(".unl").replace("/", "_")
                captured.append({
                    "node": dev["name"],
                    "node_id": dev["node_id"],
                    "lab": dev["lab_path"],
                    "file_path": f"{lab_name}/configs/{dev['name']}.conf",
                    "content": out.get("output", ""),
                })
            except ConsoleError as e:
                skipped.append({"node": dev["name"], "lab": dev["lab_path"],
                                "reason": str(e)})

        if not captured:
            return {"committed": 0, "skipped": skipped,
                    "error": "No configs captured — nothing to commit"}

        # EVE-NG doesn't enforce unique node names; duplicate paths in one
        # commit are rejected by GitLab (or silently overwrite). Disambiguate
        # EVERY member of a colliding group with its node_id.
        path_counts: dict[str, int] = {}
        for item in captured:
            path_counts[item["file_path"]] = path_counts.get(item["file_path"], 0) + 1
        for item in captured:
            if path_counts[item["file_path"]] > 1:
                item["file_path"] = item["file_path"].removesuffix(".conf") \
                    + f"-{item['node_id']}.conf"

        # Default branch, then create-vs-update per file (commits API rejects
        # 'create' for existing paths).
        proj = await _gl("GET", "")
        if proj.status_code >= 400:
            return {"error": f"GitLab project lookup failed: {proj.status_code} "
                             f"{proj.text[:200]}"}
        branch = proj.json().get("default_branch") or "main"

        actions = []
        for item in captured:
            head = await _gl(
                "GET",
                f"/repository/files/{quote(item['file_path'], safe='')}?ref={branch}",
            )
            actions.append({
                "action": "update" if head.status_code == 200 else "create",
                "file_path": item["file_path"],
                "content": item["content"],
            })

        commit = await _gl("POST", "/repository/commits", {
            "branch": branch,
            "commit_message": f"eveng-mcp backup: {len(actions)} config(s)"
                              + (f" from {lab_path}" if lab_path else ""),
            "actions": actions,
        })
        if commit.status_code >= 400:
            return {"error": f"GitLab commit failed: {commit.status_code} "
                             f"{commit.text[:300]}",
                    "captured": [c["file_path"] for c in captured],
                    "skipped": skipped}

        return {
            "committed": len(actions),
            "commit_id": commit.json().get("short_id"),
            "branch": branch,
            "files": [c["file_path"] for c in captured],
            "skipped": skipped,
        }
