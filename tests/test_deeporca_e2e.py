"""Opt-in, offline live-transport integration; no external model verification.

Run with AGENTBRIDGE_DEEPORCA_SOURCE pointing at a real SDK checkout:
    python -m pytest tests/test_deeporca_e2e.py -q

Only the OpenAI-compatible provider is fake. The HTTP/WebSocket server,
all-in-one Connector, worker subprocesses, and embedded SDK are real. Reuse the
SDK suite's sandbox and child-process network guard rather than user profiles.
Normal all-in-one shutdown and a fresh Connector resume the native conversation
through explicit browser attach, not through display-only recording restore.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import httpx
import pytest
import websockets

from connector.client import Connector
from connector.local_store import LocalProjectStore
from connector.spool import open_spool
from tests.test_deeporca_sdk_integration import FIRST, SECOND, hermetic_sdk  # noqa: F401


pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENTBRIDGE_DEEPORCA_SOURCE"),
    reason="opt in with AGENTBRIDGE_DEEPORCA_SOURCE=<real SDK checkout>",
)
REPO = Path(__file__).resolve().parents[1]

# A separate interpreter avoids mutating Server module globals/database engines
# used by the other suites. The child selects and retains its own ephemeral
# loopback socket; the parent never guesses a free port or contacts port 8077.
_SERVER = r'''
import asyncio
from pathlib import Path
import socket
import sys
import uvicorn

async def main():
    root = Path(sys.argv[1])
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    server = uvicorn.Server(uvicorn.Config(
        "server.app.main:app", log_level="warning", access_log=False,
        timeout_graceful_shutdown=5))
    async def stop_when_owned_flag_exists():
        while not (root / "stop-server").exists():
            await asyncio.sleep(0.05)
        server.should_exit = True
    watcher = asyncio.create_task(stop_when_owned_flag_exists())
    (root / "server-port").write_text(str(sock.getsockname()[1]))
    try:
        await server.serve(sockets=[sock])
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        sock.close()

asyncio.run(main())
'''


async def _eventually(check, *, timeout=45, label="condition"):
    last = None
    try:
        async with asyncio.timeout(timeout):
            while True:
                last = await check()
                if last:
                    return last
                await asyncio.sleep(0.05)
    except TimeoutError:
        pytest.fail(f"Timed out waiting for {label}; last result: {last!r}")


async def _receive_until(ws, predicate, *, timeout=45):
    seen = []
    try:
        async with asyncio.timeout(timeout):
            while True:
                frame = json.loads(await ws.recv())
                seen.append(frame)
                assert frame.get("type") not in {"error", "runtime.unavailable"}, frame
                if predicate(frame):
                    return frame, seen
    except TimeoutError:
        pytest.fail(f"Browser WS timed out; frames received: {seen!r}")


@pytest.mark.asyncio
async def test_real_connector_sdk_browser_roundtrip(hermetic_sdk, tmp_path, monkeypatch):
    provider = hermetic_sdk.provider
    root = tmp_path / "live-e2e"
    root.mkdir()
    # Default Connector bootstrap probes every installed runtime family. Hide
    # unrelated user CLIs so their auth/model-discovery commands cannot access
    # user installations or remote services. SDK children use sys.executable.
    empty_bin = root / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    script = root / "server_runner.py"
    script.write_text(_SERVER, encoding="utf-8")
    # Never use ambient Server account/auth/database configuration, including its
    # legacy aliases. Registration and enrollment below mint test-only secrets.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AGENTBRIDGE_", "DEEPBOX_")) or key.startswith("DEEPBOX_TEST_")}
    env.update({
        "AGENTBRIDGE_ENV": "development",
        "AGENTBRIDGE_AUTH_MODE": "local",
        "AGENTBRIDGE_DATABASE_URL": "sqlite:///" + (root / "server.sqlite3").as_posix(),
        "AGENTBRIDGE_DATA_DIR": str(root / "server-data"),
        "AGENTBRIDGE_SECRET": "test-only-" + uuid4().hex,
        "AGENTBRIDGE_REGISTRATION_ENABLED": "true",
        "AGENTBRIDGE_COOKIE_SECURE": "false",
        "AGENTBRIDGE_RATE_LIMIT_ENABLED": "false",
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONPATH": os.pathsep.join([str(REPO), env.get("PYTHONPATH", "")]),
    })
    local_store = LocalProjectStore(root / "connector" / "projects.sqlite3")
    project = local_store.add(str(hermetic_sdk.workspace), "Offline E2E")
    log = (root / "server.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(script), str(root)], cwd=root, env=env,
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
    )
    connector = None
    connector_task = None
    worker = None
    try:
        async def server_port():
            assert process.poll() is None, (root / "server.log").read_text()
            path = root / "server-port"
            value = path.read_text().strip() if path.exists() else ""
            return int(value) if value else None

        port = await _eventually(server_port, label="owned Server socket")
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=10) as browser:
            async def server_ready():
                assert process.poll() is None, (root / "server.log").read_text()
                try:
                    return (await browser.get("/api/health")).status_code == 200
                except httpx.ConnectError:
                    return False

            await _eventually(server_ready, label="Server health")
            response = await browser.post("/api/auth/register", json={
                "username": "offline-e2e-owner", "password": "test-only-e2e-password-123",
            })
            assert response.status_code == 200, response.text
            response = await browser.post("/api/devboxes", json={"name": "offline-e2e-machine"})
            assert response.status_code == 200, response.text
            machine = response.json()
            machine_id = machine["devbox"]["id"]
            connector = Connector(base, machine["token"], local_store=local_store,
                                  spool=open_spool(base, machine["token"]))
            # Exercise the public default all-in-one path, not Supervisor control
            # methods or injected workers/transport channels.
            connector_task = asyncio.create_task(connector.run())

            async def inventory_ready():
                assert not connector_task.done(), "Connector stopped before enrollment"
                response = await browser.get("/api/devboxes")
                assert response.status_code == 200, response.text
                registered = next(item for item in response.json() if item["id"] == machine_id)
                return registered["online"] and any(item["id"] == project.id for item in registered["projects"])

            await _eventually(inventory_ready, label="Connector project inventory")
            response = await browser.post(f"/api/devboxes/{machine_id}/agents", json={
                "handle": "offline-orca", "display_name": "Offline DeepOrca",
                "runtime": "deeporca", "local_project_id": project.id,
                "runtime_config": {},
            })
            assert response.status_code == 200, response.text
            agent = response.json()
            aid = agent["id"]
            assert agent["renderer"] == "deeporca-chat-v1"

            async def agent_ready():
                response = await browser.get(f"/api/agents/{aid}")
                assert response.status_code == 200, response.text
                observed = response.json()
                status = observed.get("runtime_status") or {}
                assert status.get("state") not in {"error", "needs_configuration"}, observed
                return observed if status.get("state") == "ready" else None

            observed = await _eventually(agent_ready, timeout=90, label="DeepOrca observed ready")
            assert observed["runtime_status"]["revision"]
            assert connector.connect_count == 1
            worker = connector.supervisor._deeporca_workers[aid]
            assert worker._transport._process.pid and worker.is_alive()
            first_worker_pid = worker._transport._process.pid
            private_binding = connector.supervisor._library_store().worker_binding(aid)
            assert Path(private_binding["workspace"]) == hermetic_sdk.workspace
            assert Path(private_binding["home"]).is_relative_to(root)
            assert provider.requests == []  # startup is not an LLM turn

            response = await browser.post(f"/api/agents/{aid}/sessions", json={"surface": "structured"})
            assert response.status_code == 200, response.text
            sid = response.json()["id"]
            ws_url = base.replace("http://", "ws://") + "/ws/term"
            headers = {"Cookie": "deepbox_session=" + browser.cookies["deepbox_session"]}
            connect_options = dict(additional_headers=headers, origin=base, proxy=None)
            async with websockets.connect(ws_url, **connect_options) as ws:
                await ws.send(json.dumps({"type": "attach", "session_id": sid, "surface": "structured"}))
                ready, initial = await _receive_until(ws, lambda f: f.get("type") == "session.ready")
                assert ready["surface"] == "structured"
                first_pty = ready["pty_instance_id"]
                assert any(f.get("type") == "restore" and f.get("kind") == "event" for f in initial)
                input_id = str(uuid4())
                user_input = {"type": "input", "session_id": sid,
                              "launch_id": ready["launch_id"],
                              "client_input_id": input_id, "data": FIRST}
                await ws.send(json.dumps(user_input))
                def completed(frame):
                    return (frame.get("type") == "output" and frame.get("kind") == "event"
                            and any(json.loads(line).get("ev") == "turn.end"
                                    for line in frame["data"].splitlines() if line.strip()))
                _, frames = await _receive_until(ws, completed)
                acks = [f for f in frames if f.get("type") == "input_ack"]
                assert any(f["client_input_id"] == input_id and f["status"] == "delivered" for f in acks), frames
                events = [json.loads(line) for f in frames if f.get("type") == "output"
                          for line in f["data"].splitlines() if line.strip()]
                assert "".join(e.get("text", "") for e in events if e.get("ev") == "message.delta") == "First native answer ORCA-ANSWER-731."
                assert events[-1]["ev"] == "turn.end"
                assert not any(e.get("ev") == "error" for e in events), events
                assert len(provider.requests) == 1

                # Re-deliver the same browser operation over the real wire.
                await ws.send(json.dumps(user_input))
                ack, duplicate_frames = await _receive_until(
                    ws, lambda f: f.get("type") == "input_ack" and f.get("client_input_id") == input_id)
                assert ack["status"] == "delivered" and ack["duplicate"] is True, ack
                assert not any(f.get("type") == "output" for f in duplicate_frames)
                await asyncio.sleep(0.2)
                assert len(provider.requests) == 1

            # A fresh browser socket gets canonical JSONL from the durable
            # recording, not merely the previous socket's live output buffer.
            async with websockets.connect(ws_url, **connect_options) as ws:
                await ws.send(json.dumps({"type": "attach", "session_id": sid, "surface": "structured"}))
                restore, _ = await _receive_until(ws, lambda f: f.get("type") == "restore")
                assert restore["kind"] == "event"
                restored = [json.loads(line) for line in restore["data"].splitlines()]
                assert restored[-len(events):] == events
                assert sum(e.get("ev") == "turn.end" for e in restored) == 1
            response = await browser.get(f"/api/sessions/{sid}/recording")
            assert response.status_code == 200, response.text
            cast = [json.loads(line) for line in response.text.splitlines()]
            assert cast[0]["version"] == 2
            recorded = [json.loads(row[2]) for row in cast[1:] if row[1] == "event"]
            assert recorded == restored
            assert len(provider.requests) == 1

            # Full normal all-in-one shutdown, not merely a transport reconnect
            # or direct Supervisor.open_pty call. This is the CLI lifecycle:
            # run() detaches the transport, then main() closes its Supervisor.
            connector_task.cancel()
            await asyncio.gather(connector_task, return_exceptions=True)
            connector_task = None
            await connector.supervisor.aclose()
            assert not worker.is_alive()
            connector = None
            local_store.close()

            async def machine_offline():
                response = await browser.get("/api/devboxes")
                assert response.status_code == 200, response.text
                return not next(item for item in response.json() if item["id"] == machine_id)["online"]

            await _eventually(machine_offline, label="normal Connector shutdown")
            local_store = LocalProjectStore(root / "connector" / "projects.sqlite3")
            connector = Connector(base, machine["token"], local_store=local_store,
                                  spool=open_spool(base, machine["token"]))
            connector_task = asyncio.create_task(connector.run())
            await _eventually(inventory_ready, label="fresh Connector project inventory")
            await _eventually(agent_ready, timeout=90, label="fresh DeepOrca worker ready")
            worker = connector.supervisor._deeporca_workers[aid]
            assert worker.is_alive()
            assert worker._transport._process.pid != first_worker_pid
            assert connector.connect_count == 1
            assert connector.supervisor._library_store().worker_binding(aid) == private_binding

            # Discover the existing public reference through the authenticated
            # API, then explicitly continue THAT conversation with its expected
            # launch generation. Ordinary attach remains passive for history.
            response = await browser.get(f"/api/agents/{aid}/sessions")
            assert response.status_code == 200, response.text
            sessions = response.json()
            assert [session["id"] for session in sessions] == [sid]
            assert sessions[0]["state"] == "inactive", sessions
            assert len(provider.requests) == 1  # restart never replays model input
            async with websockets.connect(ws_url, **connect_options) as ws:
                await ws.send(json.dumps({"type": "resume", "session_id": sid, "surface": "structured",
                                          "launch_id": sessions[0]["launch_id"]}))
                ready, resumed_frames = await _receive_until(ws, lambda f: f.get("type") == "session.ready")
                assert ready["launch_id"] != sessions[0]["launch_id"]
                assert ready["session_id"] == sid
                assert ready["pty_instance_id"] != first_pty
                assert ready["surface"] == "structured"
                replay = next(f for f in resumed_frames if f.get("type") == "restore")
                assert replay["kind"] == "event"
                replay_events = [json.loads(line) for line in replay["data"].splitlines()]
                assert sum(e.get("ev") == "turn.end" for e in replay_events) == 1
                assert not any(f.get("type") == "input_ack" for f in resumed_frames)
                await asyncio.sleep(0.2)
                assert len(provider.requests) == 1  # display restore is not model context

                second_input_id = str(uuid4())
                await ws.send(json.dumps({"type": "input", "session_id": sid,
                    "launch_id": ready["launch_id"],
                    "client_input_id": second_input_id, "data": SECOND}))
                _, second_frames = await _receive_until(ws, completed)
                assert any(f.get("type") == "input_ack" and f.get("client_input_id") == second_input_id
                           and f.get("status") == "delivered" for f in second_frames), second_frames
                second_events = [json.loads(line) for f in second_frames if f.get("type") == "output"
                                 for line in f["data"].splitlines() if line.strip()]
                assert not any(e.get("ev") == "error" for e in second_events), second_events
                assert "".join(e.get("text", "") for e in second_events if e.get("ev") == "message.delta") == "Second native answer with restored context."

            assert len(provider.requests) == 2
            # The provider, not DeepBox's display recording, observes the SDK's
            # original native user/assistant context exactly once in order.
            messages = provider.requests[1]["messages"]
            assert [m["content"] for m in messages if m["role"] == "user"] == [FIRST, SECOND]
            assert any(m["role"] == "assistant" and m["content"] == "First native answer ORCA-ANSWER-731."
                       for m in messages), messages
            response = await browser.get(f"/api/agents/{aid}/sessions")
            assert response.status_code == 200, response.text
            assert [session["id"] for session in response.json()] == [sid]
            assert response.json()[0]["state"] == "live"
    finally:
        # Transport cancellation alone intentionally does not own workers.
        # Always close this test's Supervisor before releasing its sandbox.
        try:
            if connector_task is not None:
                connector_task.cancel()
                await asyncio.gather(connector_task, return_exceptions=True)
            if connector is not None:
                await connector.supervisor.aclose()
            local_store.close()
        finally:
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
        if worker is not None:
            assert not worker.is_alive()
