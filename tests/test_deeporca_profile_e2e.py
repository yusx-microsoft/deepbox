"""Opt-in offline web configuration E2E: real Server, Connector and native SDK.

Run with AGENTBRIDGE_DEEPORCA_SOURCE=<real SDK checkout> and Node on PATH.
Only the loopback OpenAI-compatible provider is fake. The actual browser module
seals a dummy API key against metadata obtained over HTTP; no private Connector
API is used to configure the Agent. Native configuration is inspected only in
the disposable profile. Reuses the SDK subprocess network guard, never a user's
profile, template, provider, or server on port 8077.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from uuid import uuid4

from dotenv import dotenv_values
import httpx
import pytest
import websockets
import yaml

from agentbridge.integrations.deeporca.contract import binding_revision
from connector.client import Connector
from connector.local_store import LocalProjectStore
from connector.spool import open_spool
from tests.test_deeporca_e2e import REPO, _SERVER, _eventually, _receive_until
from tests.test_deeporca_sdk_integration import (  # noqa: F401
    FIRST, SECOND, KEY, MODEL, hermetic_sdk,
)


pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENTBRIDGE_DEEPORCA_SOURCE"),
    reason="opt in with AGENTBRIDGE_DEEPORCA_SOURCE=<real SDK checkout>",
)

_BROWSER_CONFIG = r'''
const fs = require('node:fs');
// Use Node's real WebCrypto, not a stand-in for the browser sealing algorithm.
Object.defineProperty(globalThis, 'crypto', {value: require('node:crypto').webcrypto});
const ui = require(process.argv[1]);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
ui.configuredPayload(input.values, input.descriptor, input.agent).then(
  config => process.stdout.write(JSON.stringify(config)),
  () => { process.stderr.write('Actual browser configuration helper failed'); process.exitCode = 1; }
);
'''


def _secret_absent(value, label):
    data = value if isinstance(value, bytes) else (
        value if isinstance(value, str) else json.dumps(value)).encode("utf-8")
    assert KEY.encode("utf-8") not in data, f"Dummy provider credential leaked into {label}"


async def _browser_config(node, values, descriptor, agent=None):
    # Pass the dummy secret in memory over stdin, not argv, env, or a script file.
    result = await asyncio.to_thread(
        subprocess.run,
        [node, "-e", _BROWSER_CONFIG, str(REPO / "web/integrations/deeporca/agent-ui.js")],
        input=json.dumps({"values": values, "descriptor": descriptor, "agent": agent}),
        text=True, encoding="utf-8", capture_output=True, timeout=15,
    )
    _secret_absent(result.stdout + result.stderr, "browser helper output")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _profile_settings(binding, llm, sandbox):
    home = Path(binding["home"])
    assert home.resolve().is_relative_to(sandbox.resolve())
    profile = home / "agents" / binding["profile_name"]
    config = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    assert {key: config["llm"][key] for key in llm} == llm
    assert config["llm"]["api_key"] == "${LLM_API_KEY}"
    _secret_absent(config, "native YAML (key belongs only in local .env)")
    # The fixture's KNOWN FAKE key is the sole intentional plaintext exception.
    assert dotenv_values(profile / ".env", interpolate=False)["LLM_API_KEY"] == KEY
    return profile


async def _turn(ws, sid, prompt, answer):
    input_id = str(uuid4())
    await ws.send(json.dumps({"type": "input", "session_id": sid,
                             "client_input_id": input_id, "data": prompt}))

    def completed(frame):
        return (frame.get("type") == "output" and frame.get("kind") == "event"
                and any(json.loads(line).get("ev") == "turn.end"
                        for line in frame["data"].splitlines() if line.strip()))

    _, frames = await _receive_until(ws, completed)
    _secret_absent(frames, "canonical WebSocket events and acknowledgements")
    assert any(f.get("type") == "input_ack" and f.get("client_input_id") == input_id
               and f.get("status") == "delivered" for f in frames), frames
    events = [json.loads(line) for f in frames if f.get("type") == "output"
              for line in f["data"].splitlines() if line.strip()]
    assert not any(e.get("ev") == "error" for e in events), events
    assert sum(e.get("ev") == "turn.end" for e in events) == 1
    assert "".join(e.get("text", "") for e in events if e.get("ev") == "message.delta") == answer
    return events


@pytest.mark.asyncio
async def test_web_profile_without_template_update_and_restart(hermetic_sdk, tmp_path, monkeypatch, caplog):
    provider = hermetic_sdk.provider
    # Capture Node before hiding unrelated installed/authenticated runtime CLIs.
    node = shutil.which("node")
    assert node, "This opt-in browser WebCrypto E2E requires Node on PATH"
    root = tmp_path / "profile-e2e"
    root.mkdir()
    empty_bin = root / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    # The reused SDK fixture normally supplies a local configured template.
    # REMOVE it as well as both selectors, before constructing the Connector.
    template = Path(os.environ["AGENTBRIDGE_DEEPORCA_TEMPLATE_DIR"])
    assert template.is_relative_to(tmp_path)
    shutil.rmtree(template)
    for key in ("AGENTBRIDGE_DEEPORCA_TEMPLATE_DIR", "DEEPBOX_DEEPORCA_TEMPLATE_DIR",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    script = root / "server_runner.py"
    script.write_text(_SERVER, encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AGENTBRIDGE_", "DEEPBOX_")) or key.startswith("DEEPBOX_TEST_")}
    env.update({
        "AGENTBRIDGE_ENV": "development", "AGENTBRIDGE_AUTH_MODE": "local",
        "AGENTBRIDGE_DATABASE_URL": "sqlite:///" + (root / "server.sqlite3").as_posix(),
        "AGENTBRIDGE_DATA_DIR": str(root / "server-data"),
        "AGENTBRIDGE_SECRET": "test-only-" + uuid4().hex,
        "AGENTBRIDGE_REGISTRATION_ENABLED": "true", "AGENTBRIDGE_COOKIE_SECURE": "false",
        "AGENTBRIDGE_RATE_LIMIT_ENABLED": "false", "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONPATH": os.pathsep.join([str(REPO), env.get("PYTHONPATH", "")]),
    })
    local_store = LocalProjectStore(root / "connector" / "projects.sqlite3")
    project = local_store.add(str(hermetic_sdk.workspace), "Manual profile E2E")
    log = (root / "server.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(script), str(root)], cwd=root, env=env,
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
    )
    connector = connector_task = None
    workers = []
    statuses = []
    try:
        async def server_port():
            assert process.poll() is None, (root / "server.log").read_text()
            path = root / "server-port"
            value = path.read_text().strip() if path.exists() else ""
            return int(value) if value else None

        port = await _eventually(server_port, label="owned Server socket")
        base = f"http://127.0.0.1:{port}"

        async def check_request(request):
            _secret_absent(await request.aread(), "outbound browser HTTP body")

        async def check_response(response):
            _secret_absent(await response.aread(), "Server HTTP response")

        async with httpx.AsyncClient(
            base_url=base, trust_env=False, timeout=10,
            event_hooks={"request": [check_request], "response": [check_response]},
        ) as browser:
            async def server_ready():
                assert process.poll() is None, (root / "server.log").read_text()
                try:
                    return (await browser.get("/api/health")).status_code == 200
                except httpx.ConnectError:
                    return False

            await _eventually(server_ready, label="Server health")
            response = await browser.post("/api/auth/register", json={
                "username": "profile-e2e-owner", "password": "test-only-profile-password-123",
            })
            assert response.status_code == 200, response.text
            response = await browser.post("/api/devboxes", json={"name": "profile-e2e-machine"})
            assert response.status_code == 200, response.text
            machine = response.json()
            machine_id = machine["devbox"]["id"]

            def start_connector():
                instance = Connector(base, machine["token"], local_store=local_store,
                                     spool=open_spool(base, machine["token"]))
                return instance, asyncio.create_task(instance.run())

            connector, connector_task = start_connector()

            def advertised_runtimes(inventory):
                capabilities = inventory.get("capabilities") or []
                # /api/devboxes currently serializes the runtime array directly;
                # accept the wrapped inventory shape as well, never re-probe it.
                return capabilities.get("runtimes", []) if isinstance(capabilities, dict) else capabilities

            async def inventory_ready():
                assert not connector_task.done(), "Connector stopped before enrollment"
                response = await browser.get("/api/devboxes")
                assert response.status_code == 200, response.text
                registered = next(item for item in response.json() if item["id"] == machine_id)
                if (registered["online"]
                        and any(d.get("runtime") == "deeporca" for d in advertised_runtimes(registered))
                        and any(p["id"] == project.id for p in registered["projects"])):
                    return registered

            registered = await _eventually(inventory_ready, label="Connector project inventory")

            def runtime_descriptor(inventory):
                descriptor = next(d for d in advertised_runtimes(inventory) if d["runtime"] == "deeporca")
                assert descriptor["compatibility"]["status"] == "compatible", descriptor
                public = descriptor["agent_config"].get("credential_key")
                assert public, "Real Connector did not advertise its credential sealing key"
                assert public["version"] == 1 and public["algorithm"] == "RSA-OAEP-256+A256GCM"
                assert len(public["key_id"]) == 64 and "d" not in public["public_key"]
                return descriptor

            descriptor = runtime_descriptor(registered)
            llm = {"provider": "openai", "base_url": f"http://127.0.0.1:{provider.server.server_port}/v1",
                   "model": MODEL, "context_window": 32768, "reasoning_effort": "low"}
            config = await _browser_config(node, {**llm, "auth_mode": "api_key", "api_key": KEY}, descriptor)
            assert config["llm"] == llm
            sealed = config["credential"]
            assert sealed["mode"] == "sealed"
            assert sealed["key_id"] == descriptor["agent_config"]["credential_key"]["key_id"]
            response = await browser.post(f"/api/devboxes/{machine_id}/agents", json={
                "handle": "manual-orca", "display_name": "Manually configured DeepOrca",
                "runtime": "deeporca", "local_project_id": project.id, "runtime_config": config,
            })
            assert response.status_code == 200, response.text
            agent = response.json()
            aid = agent["id"]
            assert agent["renderer"] == "deeporca-chat-v1"
            revision = binding_revision(aid, project.id, config)

            async def agent_ready():
                response = await browser.get(f"/api/agents/{aid}")
                assert response.status_code == 200, response.text
                observed = response.json()
                status = observed.get("runtime_status") or {}
                statuses.append(status)
                assert status.get("state") not in {"error", "needs_configuration"}, observed
                return observed if status.get("state") == "ready" and status.get("revision") == revision else None

            agent = await _eventually(agent_ready, timeout=90, label="manual profile ready without template")
            assert connector.connect_count == 1 and not provider.requests
            binding = connector.supervisor._library_store().worker_binding(aid)
            assert "template_dir" not in binding and not template.exists()
            assert Path(binding["workspace"]) == hermetic_sdk.workspace
            profile = _profile_settings(binding, llm, tmp_path)
            identity = {k: binding[k] for k in ("home", "profile_name", "workspace")}
            key_path = Path(binding["credential_key_path"])
            assert key_path.resolve().is_relative_to(tmp_path.resolve())
            private_key_digest = hashlib.sha256(key_path.read_bytes()).hexdigest()
            worker = connector.supervisor._deeporca_workers[aid]
            workers.append(worker)
            assert worker.is_alive() and worker._transport._process.pid != os.getpid()

            updated_llm = {**llm, "context_window": 65536, "reasoning_effort": "high"}
            # Actual browser Keep-existing-key path omits the credential entirely.
            update = await _browser_config(node, {**updated_llm, "auth_mode": "keep"}, descriptor, agent)
            assert "credential" not in update and update["llm"] == updated_llm
            ws_url = base.replace("http://", "ws://") + "/ws/term"
            options = {"additional_headers": {"Cookie": "deepbox_session=" + browser.cookies["deepbox_session"]},
                       "origin": base, "proxy": None}

            async def new_session():
                response = await browser.post(f"/api/agents/{aid}/sessions", json={"surface": "structured"})
                assert response.status_code == 200, response.text
                return response.json()["id"]

            async def attach(ws, sid):
                await ws.send(json.dumps({"type": "attach", "session_id": sid, "surface": "structured"}))
                ready, frames = await _receive_until(ws, lambda f: f.get("type") == "session.ready")
                _secret_absent(frames, "canonical readiness and restored events")
                assert ready["surface"] == "structured" and ready["session_id"] == sid
                return ready, frames

            first_sid = await new_session()
            async with websockets.connect(ws_url, **options) as ws:
                await attach(ws, first_sid)
                await _turn(ws, first_sid, FIRST, "First native answer ORCA-ANSWER-731.")
                assert len(provider.requests) == 1 and not provider.errors
                assert provider.requests[0]["model"] == MODEL
                assert provider.requests[0]["reasoning_effort"] == "low"
                # An idle but active conversation is fenced, not silently killed.
                response = await browser.patch(f"/api/agents/{aid}", json={"runtime_config": update})
                assert response.status_code == 409, response.text
                unchanged = (await browser.get(f"/api/agents/{aid}")).json()
                assert unchanged["runtime_config"]["llm"] == llm
                assert unchanged["runtime_status"]["revision"] == revision
                _profile_settings(binding, llm, tmp_path)
                await ws.send(json.dumps({"type": "terminate", "session_id": first_sid}))
                _, frames = await _receive_until(ws, lambda f: f.get("type") == "exit")
                _secret_absent(frames, "session termination")

            response = await browser.patch(f"/api/agents/{aid}", json={"runtime_config": update})
            assert response.status_code == 200, response.text
            patched = response.json()
            assert patched["runtime_config"]["llm"] == updated_llm
            # Keeping the credential must preserve the original envelope bytes.
            assert patched["runtime_config"]["credential"] == sealed
            previous_revision = revision
            revision = binding_revision(aid, project.id, {**update, "credential": sealed})
            assert revision != previous_revision
            assert patched["runtime_status"]["revision"] == revision
            assert patched["runtime_status"]["state"] == "pending"
            agent = await _eventually(agent_ready, timeout=90, label="updated model settings ready")
            updated_binding = connector.supervisor._library_store().worker_binding(aid)
            assert {k: updated_binding[k] for k in identity} == identity
            assert _profile_settings(updated_binding, updated_llm, tmp_path) == profile
            assert hashlib.sha256(key_path.read_bytes()).hexdigest() == private_key_digest
            updated_worker = connector.supervisor._deeporca_workers[aid]
            workers.append(updated_worker)
            assert not worker.is_alive() and updated_worker.is_alive()
            updated_worker_pid = updated_worker._transport._process.pid
            assert len(provider.requests) == 1  # readiness does not authenticate a provider

            sid = await new_session()
            async with websockets.connect(ws_url, **options) as ws:
                ready, _ = await attach(ws, sid)
                original_pty = ready["pty_instance_id"]
                await _turn(ws, sid, FIRST, "First native answer ORCA-ANSWER-731.")
            assert len(provider.requests) == 2 and not provider.errors
            assert provider.requests[1]["reasoning_effort"] == "high"
            native_id = connector.supervisor._library_store().native_session_id(aid, sid)
            native_context = profile / "chat" / f"{native_id}.json"
            assert FIRST in native_context.read_text(encoding="utf-8")

            # Full normal all-in-one lifecycle; same enrollment, disk stores and
            # key, fresh worker/process. Do not revive an explicitly ended chat.
            connector_task.cancel()
            await asyncio.gather(connector_task, return_exceptions=True)
            connector_task = None
            await connector.supervisor.aclose()
            assert not updated_worker.is_alive()
            connector = None
            local_store.close()

            async def machine_offline():
                response = await browser.get("/api/devboxes")
                assert response.status_code == 200, response.text
                return not next(d for d in response.json() if d["id"] == machine_id)["online"]

            await _eventually(machine_offline, label="normal Connector shutdown")
            local_store = LocalProjectStore(root / "connector" / "projects.sqlite3")
            connector, connector_task = start_connector()
            registered = await _eventually(inventory_ready, label="restarted Connector inventory")
            assert runtime_descriptor(registered)["agent_config"]["credential_key"] == descriptor["agent_config"]["credential_key"]
            await _eventually(agent_ready, timeout=90, label="restarted native profile ready")
            resumed_binding = connector.supervisor._library_store().worker_binding(aid)
            assert resumed_binding == updated_binding
            assert _profile_settings(resumed_binding, updated_llm, tmp_path) == profile
            assert hashlib.sha256(key_path.read_bytes()).hexdigest() == private_key_digest
            assert connector.supervisor._library_store().native_session_id(aid, sid) == native_id
            resumed_worker = connector.supervisor._deeporca_workers[aid]
            workers.append(resumed_worker)
            assert resumed_worker.is_alive()
            assert resumed_worker._transport._process.pid != updated_worker_pid
            assert len(provider.requests) == 2
            sessions = (await browser.get(f"/api/agents/{aid}/sessions")).json()
            assert next(s for s in sessions if s["id"] == sid)["state"] == "inactive"
            async with websockets.connect(ws_url, **options) as ws:
                ready, frames = await attach(ws, sid)
                assert ready["pty_instance_id"] != original_pty
                restore = next(f for f in frames if f.get("type") == "restore")
                assert restore["kind"] == "event" and "ORCA-ANSWER-731" in restore["data"]
                assert len(provider.requests) == 2  # display restore is not a model turn
                await _turn(ws, sid, SECOND, "Second native answer with restored context.")
            assert len(provider.requests) == 3 and not provider.errors
            request = provider.requests[2]
            assert request["model"] == MODEL and request["reasoning_effort"] == "high"
            # Provider validates Authorization against KEY on EVERY actual turn.
            # Its native request context, not the browser recording, proves resume.
            assert [m["content"] for m in request["messages"] if m["role"] == "user"] == [FIRST, SECOND]
            assert any(m["role"] == "assistant" and m["content"] == "First native answer ORCA-ANSWER-731."
                       for m in request["messages"])

            for recorded_sid, turns in ((first_sid, 1), (sid, 2)):
                response = await browser.get(f"/api/sessions/{recorded_sid}/recording")
                assert response.status_code == 200, response.text
                cast = [json.loads(line) for line in response.text.splitlines()]
                assert cast[0]["version"] == 2
                events = [json.loads(row[2]) for row in cast[1:] if row[1] == "event"]
                assert sum(e.get("ev") == "turn.end" for e in events) == turns
                _secret_absent(events, "durable canonical recording")
            with sqlite3.connect(root / "server.sqlite3") as db:
                stored = json.loads(db.execute("SELECT runtime_config FROM agent WHERE id=?", (aid,)).fetchone()[0])
                assert stored["credential"] == sealed and stored["llm"] == updated_llm
                _secret_absent("\n".join(db.iterdump()), "entire logical Server database")
            assert {s["revision"] for s in statuses if s.get("state") == "ready"} == {previous_revision, revision}
            _secret_absent(statuses, "canonical runtime statuses")
    finally:
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
        assert all(not w.is_alive() for w in workers)
        _secret_absent(caplog.text, "Connector/test logs")
        # Include SQLite journals and every recording/log file after graceful
        # shutdown, not just the current API serialization of desired settings.
        server_files = [root / "server.log", *root.glob("server.sqlite3*")]
        server_files.extend(p for p in (root / "server-data").rglob("*") if p.is_file())
        for path in server_files:
            _secret_absent(path.read_bytes(), f"Server-owned file {path.name}")
