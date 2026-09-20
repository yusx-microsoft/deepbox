"""Opt-in existing-profile E2E with the real Server, Connector and native SDK.

AGENTBRIDGE_DEEPORCA_SOURCE selects a read-only SDK checkout. Reuses the real
SDK fixture's loopback provider and spawn-interpreter socket guard. Everything
else (including native profiles and the simulated live native PID) is test-owned;
no user's profiles or server on port 8077 are accessed.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from uuid import uuid4

import httpx
import pytest
import websockets
from websockets.asyncio.client import ClientConnection

from connector.client import Connector
from connector.local_store import LocalProjectStore
from connector.spool import open_spool
from tests.test_deeporca_profile_e2e import REPO, _SERVER, _eventually, _receive_until, _turn
from tests.test_deeporca_sdk_integration import FIRST, SECOND, KEY, hermetic_sdk  # noqa: F401


pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENTBRIDGE_DEEPORCA_SOURCE"),
    reason="opt in with AGENTBRIDGE_DEEPORCA_SOURCE=<real SDK checkout>",
)
_OLD = "UNRELATED-NATIVE-CHAT-MUST-NOT-BECOME-BRIDGE-HISTORY-417"
_HOME = "private-native-home-not-for-server"


@pytest.fixture
def existing_sdk(hermetic_sdk):
    # Inventory probes and workers run concurrently. Windows text append is
    # not interprocess-atomic: keep the SAME socket guard but per-PID logs,
    # then merge before the shared fixture verifies every networking record.
    guard = hermetic_sdk.root / "child-guard" / "sitecustomize.py"
    source = guard.read_text(encoding="utf-8")
    assignment = '_log = os.environ["DEEPBOX_TEST_AUDIT_LOG"]'
    assert assignment in source
    guard.write_text(source.replace(assignment, assignment + ' + "." + str(os.getpid())'), encoding="utf-8")
    try:
        yield hermetic_sdk
    finally:
        logs = sorted(hermetic_sdk.root.glob("network-audit.jsonl.*"))
        assert logs, "No guarded subprocess started"
        hermetic_sdk.audit.write_bytes(b"".join(p.read_bytes() for p in logs))


def _native_profile(home, name, template):
    """Synthetic *native*, not SDK-managed: adoption must not call ensure_profile."""
    profile = home / "agents" / name
    for folder in ("persona", "memory/facts", "memory/episodes", "memory/reflections", "chat"):
        (profile / folder).mkdir(parents=True, exist_ok=True)
    for filename in ("config.yaml", ".env", "security.yaml"):
        shutil.copyfile(template / filename, profile / filename)
    (profile / "mcp.yaml").write_text("# Native MCP settings must survive verbatim\nservers: {}\n", encoding="utf-8")
    (profile / ".state.json").write_text('{"native_fixture": true, "custom": "retain me"}\n', encoding="utf-8")
    (profile / "persona" / "SOUL.md").write_text("# Native persona\nBe concise.\n", encoding="utf-8")
    (profile / "persona" / "USER.md").write_bytes(b"# Original user\r\nPreserve these bytes.\r\n")
    (profile / "chat" / "oldchat.json").write_text(json.dumps({
        "id": "oldchat", "title": "Unrelated native conversation",
        "created_at": 1700000000, "updated_at": 1700000000,
        "messages": [{"role": "user", "content": _OLD}],
    }) + "\n", encoding="utf-8")
    (profile / "chat" / "oldchat.history.jsonl").write_text(json.dumps({
        "role": "user", "content": _OLD,
    }) + "\n", encoding="utf-8")
    return profile


def _snapshot(profile):
    return {str(p.relative_to(profile)): p.read_bytes() for p in profile.rglob("*") if p.is_file()}


def _private_absent(value):
    data = value if isinstance(value, bytes) else (
        value if isinstance(value, str) else json.dumps(value)).encode("utf-8")
    # A distinctive path component catches native Windows paths, slash paths,
    # and JSON-escaped paths alike; labels and opaque refs remain public.
    for private in (_HOME, KEY, _OLD):
        assert private.encode() not in data, "Native private data reached the Server/Bridge history"


@pytest.mark.asyncio
async def test_advertised_native_binding_is_read_only_and_busy_safe(existing_sdk, tmp_path, monkeypatch):
    env = existing_sdk
    root = tmp_path / "existing-e2e"
    root.mkdir()
    empty_bin = root / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))  # Never probe unrelated authenticated CLIs.
    for key in ("XDG_STATE_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(key, str(tmp_path / "machine-state"))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    home = tmp_path / _HOME
    monkeypatch.setenv("AGENTBRIDGE_DEEPORCA_HOME", str(home))
    monkeypatch.setenv("DEEPORCA_HOME", str(home))
    template = Path(os.environ["AGENTBRIDGE_DEEPORCA_TEMPLATE_DIR"])
    profile = _native_profile(home, "native-paused", template)
    busy = _native_profile(home, "native-running", template)
    original = _snapshot(profile)
    # Remove the template selector: existing mode must use only native files.
    monkeypatch.delenv("AGENTBRIDGE_DEEPORCA_TEMPLATE_DIR")
    shutil.rmtree(template)

    # Observe actual outgoing requests/frames, without replacing transport or
    # SDK behavior. This includes inventory/status, not only browser responses.
    sent = {"http": 0, "ws": 0}
    http_send, ws_send = httpx.AsyncClient.send, ClientConnection.send

    async def checked_http(client, request, *args, **kwargs):
        _private_absent(await request.aread())
        sent["http"] += 1
        return await http_send(client, request, *args, **kwargs)

    async def checked_ws(ws, message, *args, **kwargs):
        _private_absent(message)
        sent["ws"] += 1
        return await ws_send(ws, message, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", checked_http)
    monkeypatch.setattr(ClientConnection, "send", checked_ws)
    script = root / "server_runner.py"
    script.write_text(_SERVER, encoding="utf-8")
    server_env = {k: v for k, v in os.environ.items()
                  if not k.startswith(("AGENTBRIDGE_", "DEEPBOX_")) or k.startswith("DEEPBOX_TEST_")}
    server_env.pop("DEEPORCA_HOME", None)  # Native selection is Connector-local.
    server_env.update({
        "AGENTBRIDGE_ENV": "development", "AGENTBRIDGE_AUTH_MODE": "local",
        "AGENTBRIDGE_DATABASE_URL": "sqlite:///" + (root / "server.sqlite3").as_posix(),
        "AGENTBRIDGE_DATA_DIR": str(root / "server-data"),
        "AGENTBRIDGE_SECRET": "test-only-" + uuid4().hex,
        "AGENTBRIDGE_REGISTRATION_ENABLED": "true", "AGENTBRIDGE_COOKIE_SECURE": "false",
        "AGENTBRIDGE_RATE_LIMIT_ENABLED": "false", "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONPATH": os.pathsep.join([str(REPO), server_env.get("PYTHONPATH", "")]),
    })
    local = LocalProjectStore(root / "connector" / "projects.sqlite3")
    project = local.add(str(env.workspace), "Existing profile project")
    log = (root / "server.log").open("w", encoding="utf-8")
    native = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    runtime = busy / ".runtime.json"
    runtime.write_text(json.dumps({"pid": native.pid}), encoding="utf-8")
    busy_original = _snapshot(busy)
    process = subprocess.Popen([sys.executable, str(script), str(root)], cwd=root, env=server_env,
                               stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    connector = task = None
    workers = []
    try:
        async def server_port():
            assert process.poll() is None, (root / "server.log").read_text()
            path = root / "server-port"
            return int(path.read_text()) if path.exists() and path.read_text().strip() else None

        port = await _eventually(server_port, label="owned Server socket")
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base, timeout=10, trust_env=False) as browser:
            async def healthy():
                assert process.poll() is None, (root / "server.log").read_text()
                try:
                    return (await browser.get("/api/health")).status_code == 200
                except httpx.ConnectError:
                    return False

            await _eventually(healthy, label="Server health")
            response = await browser.post("/api/auth/register", json={
                "username": "native-owner", "password": "test-only-native-password-123",
            })
            assert response.status_code == 200, response.text
            response = await browser.post("/api/devboxes", json={"name": "native-machine"})
            assert response.status_code == 200, response.text
            machine = response.json()
            mid = machine["devbox"]["id"]
            connector = Connector(base, machine["token"], local_store=local,
                                  spool=open_spool(base, machine["token"]))
            task = asyncio.create_task(connector.run())

            async def inventory():
                assert not task.done(), task.exception() if task.done() else None
                response = await browser.get("/api/devboxes")
                assert response.status_code == 200, response.text
                _private_absent(response.content)
                item = next(d for d in response.json() if d["id"] == mid)
                caps = item.get("capabilities") or []
                caps = caps.get("runtimes", []) if isinstance(caps, dict) else caps
                if item["online"] and any(p["id"] == project.id for p in item["projects"]):
                    return next((d for d in caps if d["runtime"] == "deeporca"), None)

            descriptor = await _eventually(inventory, label="real SDK profile catalog")
            assert descriptor["compatibility"]["status"] == "compatible", descriptor
            config_descriptor = descriptor["agent_config"]
            assert config_descriptor["profile_modes"] == ["create", "bind"]
            catalog = config_descriptor["existing_profiles"]
            assert {p["label"] for p in catalog} == {"native-paused", "native-running"}
            assert all(set(p) == {"id", "label"} and re.fullmatch(r"native-[0-9a-f]{32}", p["id"]) for p in catalog)
            refs = {p["label"]: p["id"] for p in catalog}

            def config(ref):
                return {"integration_version": 1, "profile": {
                    "mode": "bind", "profile_ref": ref, "native_stopped": True,
                }}

            async def create(handle, ref):
                return await browser.post(f"/api/devboxes/{mid}/agents", json={
                    "handle": handle, "display_name": handle, "runtime": "deeporca",
                    "local_project_id": project.id, "runtime_config": config(ref),
                })

            response = await create("not-advertised", "native-" + "0" * 32)
            assert response.status_code in (400, 409, 422), response.text
            response = await create("native-bound", refs["native-paused"])
            assert response.status_code == 200, response.text
            aid = response.json()["id"]
            assert response.json()["runtime_config"] == config(refs["native-paused"])

            async def settled(agent_id, state):
                response = await browser.get(f"/api/agents/{agent_id}")
                assert response.status_code == 200, response.text
                _private_absent(response.content)
                status = response.json().get("runtime_status") or {}
                if state == "ready":
                    assert status.get("state") not in {"error", "needs_configuration"}, status
                return status if status.get("state") == state else None

            await _eventually(lambda: settled(aid, "ready"), timeout=90, label="existing native profile ready")
            store = connector.supervisor._library_store()
            binding = store.worker_binding(aid)
            assert Path(binding["home"]) == home and binding["profile_name"] == profile.name
            assert binding["config"] == config(refs["native-paused"])
            assert "template_dir" not in binding
            worker = connector.supervisor._deeporca_workers[aid]
            workers.append(worker)
            assert worker.is_alive() and worker._transport._process.pid != os.getpid()
            assert not env.provider.requests

            # Server cannot retarget a bound identity, even to another advertised ref.
            response = await browser.patch(f"/api/agents/{aid}", json={"runtime_config": config(refs["native-running"])})
            assert response.status_code == 409, response.text
            response = await create("duplicate-binding", refs["native-paused"])
            assert response.status_code == 200, response.text
            duplicate = response.json()["id"]
            status = await _eventually(lambda: settled(duplicate, "error"), timeout=90, label="duplicate bind rejected")
            assert status["code"] == "existing_profile_unavailable", status
            assert store.get_binding(duplicate) is None and worker.is_alive()

            response = await create("native-busy", refs["native-running"])
            assert response.status_code == 200, response.text
            busy_aid = response.json()["id"]
            status = await _eventually(lambda: settled(busy_aid, "error"), timeout=90, label="native live PID rejected")
            assert status["code"] == "existing_profile_unavailable", status
            assert native.poll() is None  # SDK must never kill the native owner.
            assert _snapshot(busy) == busy_original
            assert not env.provider.requests  # Neither readiness nor rejection contacted provider.

            response = await browser.get(f"/api/agents/{aid}/sessions")
            assert response.status_code == 200 and response.json() == []
            response = await browser.post(f"/api/agents/{aid}/sessions", json={"surface": "structured"})
            assert response.status_code == 200, response.text
            sid = response.json()["id"]
            options = {"additional_headers": {"Cookie": "deepbox_session=" + browser.cookies["deepbox_session"]},
                       "origin": base, "proxy": None}
            async with websockets.connect(base.replace("http://", "ws://") + "/ws/term", **options) as ws:
                await ws.send(json.dumps({"type": "attach", "session_id": sid, "surface": "structured"}))
                ready, frames = await _receive_until(ws, lambda f: f.get("type") == "session.ready")
                _private_absent(frames)
                events = await _turn(ws, sid, FIRST, "First native answer ORCA-ANSWER-731.", launch_id=ready["launch_id"])
                _private_absent(events)
                events = await _turn(ws, sid, SECOND, "Second native answer with restored context.", launch_id=ready["launch_id"])
                _private_absent(events)
            assert len(env.provider.requests) == 2 and not env.provider.errors
            request = env.provider.requests[-1]
            assert [m["content"] for m in request["messages"] if m["role"] == "user"] == [FIRST, SECOND]
            assert _OLD not in json.dumps(env.provider.requests)
            native_id = store.native_session_id(aid, sid)
            assert native_id != "oldchat"
            assert FIRST in (profile / "chat" / f"{native_id}.json").read_text(encoding="utf-8")
            response = await browser.get(f"/api/sessions/{sid}/recording")
            assert response.status_code == 200, response.text
            _private_absent(response.content)
            assert "ORCA-ANSWER-731" in response.text
            with sqlite3.connect(root / "server.sqlite3") as db:
                _private_absent("\n".join(db.iterdump()))
            assert sent["http"] > 10 and sent["ws"] > 5
    finally:
        try:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if connector is not None:
                await connector.supervisor.aclose()
            local.close()
        finally:
            native.terminate()  # Only the test cleans up its own live-PID sentinel.
            await asyncio.to_thread(native.wait, timeout=10)
            (root / "stop-server").touch()
            try:
                await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=15)
            except TimeoutError:
                process.kill()
                await asyncio.to_thread(process.wait)
                pytest.fail("Owned test Server failed graceful shutdown")
            finally:
                log.close()
        assert process.returncode == 0, (root / "server.log").read_text()
        assert all(not w.is_alive() for w in workers)
        # After complete SDK teardown, every original file (not just parsed
        # settings) must retain its bytes. New Bridge-native chat files are OK.
        assert {name: (profile / name).read_bytes() for name in original} == original
        assert _snapshot(busy) == busy_original
        for path in [root / "server.log", *root.glob("server.sqlite3*"),
                     *(p for p in (root / "server-data").rglob("*") if p.is_file())]:
            _private_absent(path.read_bytes())
