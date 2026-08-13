"""junos_mcp integration: the wipe-on-error class of bug must be dead."""

from __future__ import annotations

import io
import json
import tarfile

import pytest
from fastmcp import FastMCP

from eveng_mcp.integrations import junos_mcp
from eveng_mcp.integrations.junos_mcp import DockerFileTransport, JmcpError
from eveng_mcp.loader import load_integrations
from tests.conftest import make_config


class FakeTransport:
    """In-memory stand-in for DockerFileTransport."""

    def __init__(self, content: str | None = None, read_error: bool = False):
        self.content = content
        self.read_error = read_error
        self.written: list[str] = []

    async def read(self) -> str | None:
        if self.read_error:
            raise JmcpError("Docker socket error reading devices.json: boom")
        return self.content

    async def write(self, content: str) -> None:
        self.written.append(content)


@pytest.fixture
def jmcp_cfg():
    return make_config(
        JMCP_URL="http://localhost:30030",
        JMCP_DEVICE_USER="labuser",
        JMCP_DEVICE_PASS="labpass",
    )


async def test_read_failure_aborts_write(
    monkeypatch, eveng_api, client, platforms, jmcp_cfg
):
    """A transport error reading devices.json must abort — NEVER treated as
    an empty file and overwritten (the original jmcp_bridge defect)."""
    fake = FakeTransport(read_error=True)
    monkeypatch.setattr(
        junos_mcp, "DockerFileTransport", lambda **kw: fake
    )
    mcp = FastMCP(name="t")
    load_integrations(mcp, jmcp_cfg, client, platforms)
    result = await mcp.call_tool(
        "eveng_register_junos_devices", {"lab_path": "/Test.unl"}
    )
    payload = result.structured_content
    body = payload.get("result", payload) if isinstance(payload, dict) else payload
    assert "error" in body
    assert fake.written == [], "write must not happen after a failed read"


async def test_unregister_with_read_failure_never_writes_empty(
    monkeypatch, eveng_api, client, platforms, jmcp_cfg
):
    fake = FakeTransport(read_error=True)
    monkeypatch.setattr(junos_mcp, "DockerFileTransport", lambda **kw: fake)
    mcp = FastMCP(name="t")
    load_integrations(mcp, jmcp_cfg, client, platforms)
    result = await mcp.call_tool(
        "eveng_unregister_junos_devices", {"clear_all": True}
    )
    payload = result.structured_content
    body = payload.get("result", payload) if isinstance(payload, dict) else payload
    assert "error" in body
    assert fake.written == []


async def test_invalid_json_refuses_overwrite(
    monkeypatch, eveng_api, client, platforms, jmcp_cfg
):
    fake = FakeTransport(content="{not json!!")
    monkeypatch.setattr(junos_mcp, "DockerFileTransport", lambda **kw: fake)
    mcp = FastMCP(name="t")
    load_integrations(mcp, jmcp_cfg, client, platforms)
    result = await mcp.call_tool(
        "eveng_register_junos_devices", {"lab_path": "/Test.unl"}
    )
    payload = result.structured_content
    body = payload.get("result", payload) if isinstance(payload, dict) else payload
    assert "error" in body and "refusing" in body["error"]
    assert fake.written == []


async def test_register_merges_and_filters(
    monkeypatch, eveng_api, client, platforms, jmcp_cfg
):
    """Only running junos nodes WITH mgmt IPs are added; existing manual
    entries survive; creds come from config; no reload attempted here."""
    existing = json.dumps({"manual-R9": {"ip": "10.9.9.9", "port": 22,
                                         "username": "x",
                                         "auth": {"type": "password",
                                                  "password": "y"}}})
    fake = FakeTransport(content=existing)
    monkeypatch.setattr(junos_mcp, "DockerFileTransport", lambda **kw: fake)

    async def fake_reload(url, path):
        return "reload skipped (test)"

    monkeypatch.setattr(junos_mcp, "_trigger_reload", fake_reload)

    mcp = FastMCP(name="t")
    load_integrations(mcp, jmcp_cfg, client, platforms)
    result = await mcp.call_tool(
        "eveng_register_junos_devices", {"lab_path": "/Test.unl"}
    )
    payload = result.structured_content
    body = payload.get("result", payload) if isinstance(payload, dict) else payload

    assert [r["name"] for r in body["registered"]] == ["lab-spine1"]
    reasons = {s["node"]: s["reason"] for s in body["skipped"]}
    assert "leaf1" in reasons  # eos, not junos
    written = json.loads(fake.written[-1])
    assert set(written) == {"manual-R9", "lab-spine1"}
    assert written["lab-spine1"]["username"] == "labuser"


async def test_missing_creds_is_error(
    monkeypatch, eveng_api, client, platforms
):
    cfg = make_config(JMCP_URL="http://localhost:30030")  # no device creds
    fake = FakeTransport(content=None)
    monkeypatch.setattr(junos_mcp, "DockerFileTransport", lambda **kw: fake)
    mcp = FastMCP(name="t")
    load_integrations(mcp, cfg, client, platforms)
    result = await mcp.call_tool(
        "eveng_register_junos_devices", {"lab_path": "/Test.unl"}
    )
    payload = result.structured_content
    body = payload.get("result", payload) if isinstance(payload, dict) else payload
    assert "error" in body and "credentials" in body["error"].lower()
    assert fake.written == []


def test_docker_transport_tar_roundtrip():
    """The tar payload we PUT must contain exactly the file content — this is
    the no-shell-injection guarantee."""
    t = DockerFileTransport("c", "/app/config/devices.json", "/tmp/nope.sock")
    content = json.dumps({"evil": "'; rm -rf / #\n$(boom)"})
    buf = io.BytesIO()
    data = content.encode()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=t.file_path.name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with tarfile.open(fileobj=io.BytesIO(buf.getvalue())) as tar:
        member = tar.getmembers()[0]
        assert member.name == "devices.json"
        assert tar.extractfile(member).read().decode() == content
