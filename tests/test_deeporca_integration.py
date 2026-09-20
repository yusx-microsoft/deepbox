"""Opt-in offline integration coverage using a real DeepOrca SDK checkout.

AGENTBRIDGE_DEEPORCA_SOURCE selects the real SDK. The suites below keep native
profiles, providers, servers, sockets, workers, and browser traffic test-owned;
no user profile, live service, or external model is used.
"""
from __future__ import annotations

import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

from dotenv import dotenv_values
import httpx
import pytest
import websockets
from websockets.asyncio.client import ClientConnection
import yaml

from agentbridge.integrations.deeporca.contract import binding_revision
from connector.client import Connector
from connector.integrations.deeporca.store import DeepOrcaStore
from connector.local_store import LocalProjectStore
from connector.spool import DiskSpool, open_spool
from connector.supervisor import SessionSupervisor


SOURCE = os.environ.get("AGENTBRIDGE_DEEPORCA_SOURCE")
pytestmark = pytest.mark.skipif(
    not SOURCE,
    reason="opt in with AGENTBRIDGE_DEEPORCA_SOURCE=<real SDK checkout>",
)


# ---------------------------------------------------------------------------
# Shared hermetic real-SDK sandbox and spawn-worker integration tests
# ---------------------------------------------------------------------------
MODEL = "hermetic-local-model"
KEY = "hermetic-fake-key-not-a-secret"
FIRST = "Remember the hermetic context token: ORCA-CONTEXT-731."
SECOND = "Continue the previous conversation after a fresh worker start."
READ = "Exercise a workspace read."
DENY = "Exercise a read requiring approval."


@pytest.fixture
def isolate_connector_key_state(tmp_path, monkeypatch):
    # The real SDK now advertises web-configuration support. Its descriptor
    # provisions a sealing key, which must never touch the developer Connector.
    for name in ("LOCALAPPDATA", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(name, str(tmp_path / "machine-state"))
    # Discovery must never enumerate the developer's native profiles.
    monkeypatch.setenv("AGENTBRIDGE_DEEPORCA_HOME", str(tmp_path / "catalog-home"))
    monkeypatch.setenv("DEEPORCA_HOME", str(tmp_path / "catalog-home"))


# PYTHONPATH makes this execute in the real multiprocessing spawn interpreter
# and in the real availability probe. It never imports or replaces SDK modules.
_SITECUSTOMIZE = r'''
import json, os, sys, socket
_port = int(os.environ["DEEPBOX_TEST_PROVIDER_PORT"])
_log = os.environ["DEEPBOX_TEST_AUDIT_LOG"]
def _record(value):
    with open(_log, "a", encoding="utf-8") as f:
        f.write(json.dumps(dict(pid=os.getpid(), **value)) + "\n")
_record({"event": "guard_installed"})
def _audit(event, args):
    if event == "socket.connect":
        address = args[1]
        allowed = isinstance(address, tuple) and address[:2] == ("127.0.0.1", _port)
        # On Windows asyncio builds its wakeup pipe with stdlib socketpair(),
        # which internally connects two freshly bound loopback sockets.
        frame = sys._getframe(1)
        while not allowed and frame is not None:
            if (frame.f_code.co_name in ("socketpair", "_fallback_socketpair") and frame.f_code.co_filename == socket.__file__
                    and isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1")):
                allowed = True
            frame = frame.f_back
    elif event == "socket.getaddrinfo":
        allowed = args[0] == "127.0.0.1" and int(args[1]) == _port
    else:
        return
    _record({"event": event, "allowed": allowed, "address": repr(args[1] if event == "socket.connect" else args[:2])})
    if not allowed:
        raise RuntimeError("hermetic integration test blocked non-provider networking")
sys.addaudithook(_audit)
# Windows asyncio's ConnectEx bypasses socket.connect's audit event. Guard
# the proactor boundary too, otherwise an audit-only test gives false comfort.
if sys.platform == "win32":
    from asyncio.windows_events import IocpProactor
    _connect = IocpProactor.connect
    def _guarded_connect(self, conn, address):
        allowed = isinstance(address, tuple) and address[:2] == ("127.0.0.1", _port)
        _record({"event": "proactor.connect", "allowed": allowed, "address": repr(address)})
        if not allowed:
            raise RuntimeError("hermetic integration test blocked non-provider ConnectEx")
        return _connect(self, conn, address)
    IocpProactor.connect = _guarded_connect
'''


class _Provider:
    def __init__(self, workspace, denied):
        self.requests = []
        self.errors = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    if self.path in ("/v1/messages/count_tokens", "/v1/tokenize", "/tokenize"):
                        # SDK probes optional token-count APIs on the SAME
                        # local provider before falling back to estimation.
                        self.send_error(404, "optional token-count API not implemented")
                        return
                    owner.requests.append(body)
                    assert self.path == "/v1/chat/completions", self.path
                    assert self.headers.get("Authorization") == "Bearer " + KEY
                    assert body["model"] == MODEL
                    assert body["stream"] is True
                    messages = body["messages"]
                    prompt = next(m["content"] for m in reversed(messages) if m["role"] == "user")
                    call = None
                    if prompt in (READ, DENY) and messages[-1]["role"] != "tool":
                        assert any(t["function"]["name"] == "read" for t in body["tools"])
                        path = workspace / "fixture.txt" if prompt == READ else denied
                        call = {"index": 0, "id": "call-hermetic-read" if prompt == READ else "call-hermetic-deny",
                                "type": "function", "function": {"name": "read",
                                "arguments": json.dumps({"path": str(path)})}}
                    answer = {FIRST: "First native answer ORCA-ANSWER-731.",
                              SECOND: "Second native answer with restored context.",
                              READ: "Workspace read complete.", DENY: "Denied read complete."}[prompt]
                    deltas = [{"role": "assistant"}]
                    if call:
                        deltas.append({"tool_calls": [call]})
                    else:
                        # Multiple actual SSE chunks exercise native streamed events.
                        mid = len(answer) // 2
                        deltas.extend([{"content": answer[:mid]}, {"content": answer[mid:]}])
                    chunks = [{"id": "chatcmpl-hermetic", "object": "chat.completion.chunk",
                               "created": 1, "model": MODEL,
                               "choices": [{"index": 0, "delta": d, "finish_reason": None}]}
                              for d in deltas]
                    chunks.append({"id": "chatcmpl-hermetic", "object": "chat.completion.chunk",
                                   "created": 1, "model": MODEL,
                                   "choices": [{"index": 0, "delta": {},
                                                "finish_reason": "tool_calls" if call else "stop"}],
                                   "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}})
                    payload = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                               + "data: [DONE]\n\n").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    self.wfile.flush()
                except Exception as exc:
                    owner.errors.append(repr(exc))
                    self.send_error(500, "hermetic provider assertion failed")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def hermetic_sdk(tmp_path, monkeypatch):
    source = Path(SOURCE).resolve()
    assert (source / "deeporca" / "embedded.py").is_file(), "invalid real SDK source checkout"
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "fixture.txt").write_text("WORKSPACE-READ-731\n", encoding="utf-8")
    denied = tmp_path / "outside-project.txt"
    denied.write_text("DO-NOT-EXPOSE-731\n", encoding="utf-8")
    provider = _Provider(workspace, denied)
    try:
        # Do not inherit a user's profile selection, API keys, proxies, or SDK
        # override paths. HOME/USERPROFILE also isolate dotenv/home discovery.
        for name in list(os.environ):
            upper = name.upper()
            if (upper.startswith(("DEEPORCA", "DEEPBOX_", "AGENTBRIDGE_", "OPENAI_", "ANTHROPIC_"))
                    or any(s in upper for s in ("API_KEY", "APIKEY", "TOKEN", "SECRET", "PROXY"))):
                monkeypatch.delenv(name, raising=False)
        home = tmp_path / "user-home"
        home.mkdir()
        for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
            monkeypatch.setenv(name, str(home))
        monkeypatch.setenv("PYTHONNOUSERSITE", "1")
        monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
        monkeypatch.chdir(workspace)
        monkeypatch.setenv("AGENTBRIDGE_DEEPORCA_SOURCE", str(source))
        template = tmp_path / "LOCAL"
        template.mkdir()
        monkeypatch.setenv("AGENTBRIDGE_DEEPORCA_TEMPLATE_DIR", str(template))
        port = provider.server.server_port
        (template / ".env").write_text("LLM_API_KEY=" + KEY + "\n", encoding="utf-8")
        (template / "config.yaml").write_text(json.dumps({
            "llm": {"provider": "openai", "model": MODEL,
                    "base_url": f"http://127.0.0.1:{port}/v1",
                    "api_key": "${LLM_API_KEY}", "context_window": 32768},
        }), encoding="utf-8")
        (template / "security.yaml").write_text(json.dumps({
            "level": "standard",
            "filesystem": {"auto_approve_dirs": [str(workspace)],
                           "write_safe_root": str(workspace), "read_deny_list": [],
                           "write_deny_list": [], "follow_symlinks": False},
            "approval": {"mode": "ask", "tool_allowlist": ["read"],
                         "dangerous_pattern_enabled": True, "scope": "session"},
        }), encoding="utf-8")
        guard = tmp_path / "child-guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
        monkeypatch.setenv("PYTHONPATH", str(guard))
        monkeypatch.setenv("DEEPBOX_TEST_PROVIDER_PORT", str(port))
        audit = tmp_path / "network-audit.jsonl"
        monkeypatch.setenv("DEEPBOX_TEST_AUDIT_LOG", str(audit))
        yield SimpleNamespace(root=tmp_path, workspace=workspace, provider=provider, audit=audit)
        assert not provider.errors, provider.errors
        records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        assert any(r["event"] == "guard_installed" for r in records)
        assert any(r["event"] in ("socket.connect", "proactor.connect")
                   and r.get("address") == repr(("127.0.0.1", port)) for r in records), records
        assert not [r for r in records if r.get("allowed") is False], records
    finally:
        provider.close()


def _supervisor(env):
    local = LocalProjectStore(env.root / "projects.sqlite3")
    project = local.get_by_path(str(env.workspace)) or local.add(str(env.workspace), "Hermetic project")
    store = DeepOrcaStore(env.root / "deeporca.sqlite3", native_root=env.root / "native")
    spool = DiskSpool(str(env.root / "spool.sqlite3"))
    agent = {"id": "hermetic-agent", "name": "Hermetic SDK", "runtime": "deeporca",
             "local_project_id": project.id, "runtime_config": getattr(env, "runtime_config", {}), "enabled": True}
    sup = SessionSupervisor({agent["id"]: agent}, local_store=local,
                            deeporca_store=store, spool=spool)
    sup.set_enrollment("http://127.0.0.1", "hermetic-machine")
    return sup, local, store, spool


def _events(sup):
    return [json.loads(f["data"]) for f in sup.pending if f.get("type") == "output"]


async def _ready(sup, env, session_id="chat"):
    await asyncio.wait_for(sup.open_pty("hermetic-agent", session_id, surface="structured"), 60)
    ready = [f for f in sup.pending if f.get("type") == "ready" and f.get("session_id") == session_id]
    assert ready, sup.pending
    worker = sup._deeporca_workers["hermetic-agent"]
    pid = worker._transport._process.pid
    assert worker.is_alive() and pid != os.getpid()
    assert worker._startup_info["configured"] is True
    records = [json.loads(line) for line in env.audit.read_text(encoding="utf-8").splitlines()]
    assert any(r["event"] == "guard_installed" and r["pid"] == pid for r in records)
    return pid


async def _sdk_turn(sup, store, prompt, input_id, *, session_id="chat", expected="completed"):
    input_id = str(uuid5(NAMESPACE_URL, input_id))
    await sup.handle_control({"type": "input", "agent_id": "hermetic-agent", "session_id": session_id,
                              "client_input_id": input_id, "data": prompt})
    ack = next(f for f in sup.pending if f.get("type") == "input_ack" and f.get("client_input_id") == input_id)
    assert ack["status"] == "delivered", ack
    async def settled():
        while True:
            receipt = store.input_receipt("hermetic-agent", session_id, input_id)
            if receipt and receipt.get("result") not in (None, "pending"):
                return receipt
            await asyncio.sleep(0.02)
    receipt = await asyncio.wait_for(settled(), 30)
    assert receipt["result"] == expected, (receipt, _events(sup))
    events = [e for e in _events(sup) if e.get("client_input_id") == input_id]
    assert len([e for e in events if e.get("ev") == "turn.end"]) == 1, events
    return events


async def _close(sup, local, store, spool):
    await sup.aclose()
    local.close()
    store.close()
    spool.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolate_connector_key_state")
async def test_real_sdk_ready_turn_persistence_and_restart(hermetic_sdk):
    env = hermetic_sdk
    sup, local, store, spool = _supervisor(env)
    try:
        first_pid = await _ready(sup, env)
        first = await _sdk_turn(sup, store, FIRST, "first-input")
        assert "First native answer ORCA-ANSWER-731." == "".join(
            e.get("text", "") for e in first if e.get("ev") == "message.delta")
        native_events = [e["native"] for e in first if "native" in e]
        assert native_events and all(e["schema"] == "deeporca.turn.v1" for e in native_events)
        assert len({(e["turn_id"], e["sequence"]) for e in native_events}) == len(native_events)
        binding = store.get_binding("hermetic-agent")
        native_id = store.native_session_id("hermetic-agent", "chat")
        chat_dir = Path(binding["home"]) / "agents" / binding["profile_name"] / "chat"
        context = json.loads((chat_dir / f"{native_id}.json").read_text(encoding="utf-8"))
        assert FIRST in json.dumps(context)
        assert "ORCA-ANSWER-731" in json.dumps(context)
        assert (chat_dir / f"{native_id}.history.jsonl").is_file()
        first_request_count = len(env.provider.requests)
    finally:
        await _close(sup, local, store, spool)

    # Reopen all host stores as well as the worker. The provider's captured
    # request, not a fake answer, proves native prior messages were restored.
    sup, local, store, spool = _supervisor(env)
    try:
        first_id = str(uuid5(NAMESPACE_URL, "first-input"))
        assert any(e.get("client_input_id") == first_id for e in _events(sup))
        assert store.input_receipt("hermetic-agent", "chat", first_id)["result"] == "completed"
        assert await _ready(sup, env) != first_pid
        assert store.native_session_id("hermetic-agent", "chat") == native_id
        second = await _sdk_turn(sup, store, SECOND, "second-input")
        assert "Second native answer with restored context." == "".join(
            e.get("text", "") for e in second if e.get("ev") == "message.delta")
        messages = env.provider.requests[first_request_count]["messages"]
        assert any(m["role"] == "user" and m["content"] == FIRST for m in messages)
        assert any(m["role"] == "assistant" and "ORCA-ANSWER-731" in m.get("content", "") for m in messages)
        assert any(m["role"] == "user" and m["content"] == SECOND for m in messages)
    finally:
        await _close(sup, local, store, spool)


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolate_connector_key_state")
async def test_real_sdk_sealed_web_configuration_and_restart(hermetic_sdk):
    from connector.integrations.deeporca.credentials import public_key_info
    from test_deeporca_configuration import seal
    env = hermetic_sdk
    url = f"http://127.0.0.1:{env.provider.server.server_port}/v1"
    public = public_key_info(env.root / "projects.sqlite3")
    env.runtime_config = {
        "llm": {"provider": "openai", "base_url": url, "model": MODEL,
                "context_window": 32768, "reasoning_effort": "low"},
        "credential": seal(public, secret=KEY, base_url=url),
    }
    # No usable model/key is available in the template. The web configuration
    # must be applied by the real SDK, not just accepted by a worker mock.
    template = Path(os.environ["AGENTBRIDGE_DEEPORCA_TEMPLATE_DIR"])
    (template / ".env").unlink()
    import yaml
    template_config = yaml.safe_load((template / "config.yaml").read_text())
    template_config["llm"] = {"provider": "openai", "model": "", "base_url": "", "api_key": ""}
    (template / "config.yaml").write_text(yaml.safe_dump(template_config, sort_keys=False))
    initial_profile = None
    for attempt, prompt in enumerate((FIRST, SECOND)):
        if attempt:
            env.runtime_config["llm"] = {**env.runtime_config["llm"],
                                         "context_window": 65536, "reasoning_effort": "high"}
        sup, local, store, spool = _supervisor(env)
        try:
            await _ready(sup, env)
            binding = store.get_binding("hermetic-agent")
            profile = (binding["home"], binding["profile_name"])
            if initial_profile is not None:
                assert profile == initial_profile
            initial_profile = profile
            await _sdk_turn(sup, store, prompt, "web-config-" + str(attempt))
            assert KEY not in json.dumps(list(sup.pending))
            assert KEY not in json.dumps(store.worker_binding("hermetic-agent"))
            assert public_key_info(local.path) == public
        finally:
            await _close(sup, local, store, spool)
    assert any(m["role"] == "user" and m["content"] == FIRST
               for m in env.provider.requests[-1]["messages"])


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolate_connector_key_state")
async def test_real_sdk_deleted_native_chat_rejects_without_replay_and_fresh_chat_works(hermetic_sdk):
    env = hermetic_sdk
    sup, local, store, spool = _supervisor(env)
    try:
        await _ready(sup, env)
        await _sdk_turn(sup, store, FIRST, "before-native-loss")
        binding = store.get_binding("hermetic-agent")
        native_id = store.native_session_id("hermetic-agent", "chat")
        profile = Path(binding["home"]) / "agents" / binding["profile_name"]
        manifest = profile / ".embedded-contexts.json"
        before = manifest.read_bytes()
        assert native_id in json.loads(before)["sessions"]
        assert len(env.provider.requests) == 1
    finally:
        await _close(sup, local, store, spool)
    # Delete ONLY this fixture's native chat tree, after its process is closed.
    # Ownership marker/SDK ledger and all host display history stay intact.
    chat = profile / "chat"
    assert chat.resolve().is_relative_to(env.root.resolve())
    assert not chat.is_symlink() and not profile.is_symlink()
    shutil.rmtree(chat)
    assert manifest.read_bytes() == before and (profile / ".state.json").is_file()
    sup, local, store, spool = _supervisor(env)
    try:
        pid = await _ready(sup, env)
        first_id = str(uuid5(NAMESPACE_URL, "before-native-loss"))
        assert any(e.get("client_input_id") == first_id for e in _events(sup))
        failed = await _sdk_turn(sup, store, SECOND, "after-native-loss", expected="error")
        assert len(env.provider.requests) == 1  # no model dispatch, much less tools
        assert not (chat / f"{native_id}.json").exists()
        assert not (chat / f"{native_id}.history.jsonl").exists()
        errors = [e for e in failed if e["ev"] == "error"]
        ends = [e for e in failed if e["ev"] == "turn.end"]
        assert len(errors) == 1 and errors[0]["code"] == "native_context_unavailable"
        assert ends[0]["status"] == "error" and ends[0]["code"] == "native_context_unavailable"
        assert not any(e["ev"] in ("message.delta", "tool.start") for e in failed)
        assert str(profile) not in json.dumps(failed) and FIRST not in json.dumps(failed)
        assert await _ready(sup, env, "fresh-chat") == pid  # nonfatal to the worker
        await _sdk_turn(sup, store, FIRST, "fresh-after-native-loss", session_id="fresh-chat")
        assert len(env.provider.requests) == 2
        assert sum(m["role"] == "user" for m in env.provider.requests[-1]["messages"]) == 1
    finally:
        await _close(sup, local, store, spool)


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolate_connector_key_state")
async def test_real_sdk_workspace_read_and_failfast_approval_denial(hermetic_sdk):
    env = hermetic_sdk
    sup, local, store, spool = _supervisor(env)
    try:
        await _ready(sup, env)
        events = await _sdk_turn(sup, store, READ, "read-input")
        calls = [e for e in events if e.get("ev") == "tool.call"]
        results = [e for e in events if e.get("ev") == "tool.result"]
        assert calls and results and calls[0]["tool_id"] == results[0]["tool_id"] == "call-hermetic-read"
        assert "WORKSPACE-READ-731" in json.dumps(results)
        started = time.monotonic()
        events = await _sdk_turn(sup, store, DENY, "deny-input")
        assert time.monotonic() - started < 10, "approval must deny, never wait for UI"
        results = [e for e in events if e.get("ev") == "tool.result"]
        assert results and results[0]["tool_id"] == "call-hermetic-deny"
        assert "DO-NOT-EXPOSE-731" not in json.dumps(events)
        assert any(word in json.dumps(results).lower() for word in ("denied", "blocked", "approval")), results
        assert not any("approval" in e.get("ev", "") or "permission" in e.get("ev", "") for e in events)
    finally:
        await _close(sup, local, store, spool)


# ---------------------------------------------------------------------------
# Real Server/Connector/browser transport helpers and round-trip E2E
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Browser-configured native-profile E2E
# ---------------------------------------------------------------------------
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


async def _browser_turn(ws, sid, prompt, answer, *, launch_id):
    input_id = str(uuid4())
    await ws.send(json.dumps({"type": "input", "session_id": sid,
                             "launch_id": launch_id,
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
                if connector_task.done():
                    connector_task.result()
                    pytest.fail("Connector stopped before profile readiness")
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

            async def attach(ws, sid, *, resume=False, launch_id=None):
                await ws.send(json.dumps({"type": "resume" if resume else "attach",
                                          "session_id": sid, "surface": "structured",
                                          "launch_id": launch_id}))
                ready, frames = await _receive_until(ws, lambda f: f.get("type") == "session.ready")
                _secret_absent(frames, "canonical readiness and restored events")
                assert ready["surface"] == "structured" and ready["session_id"] == sid
                return ready, frames

            first_sid = await new_session()
            async with websockets.connect(ws_url, **options) as ws:
                ready, _ = await attach(ws, first_sid)
                await _browser_turn(ws, first_sid, FIRST, "First native answer ORCA-ANSWER-731.", launch_id=ready["launch_id"])
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
                await ws.send(json.dumps({"type": "terminate", "session_id": first_sid,
                                          "launch_id": ready["launch_id"]}))
                _, frames = await _receive_until(ws, lambda f: f.get("type") == "status" and f.get("state") == "ended")
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
                await _browser_turn(ws, sid, FIRST, "First native answer ORCA-ANSWER-731.", launch_id=ready["launch_id"])
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
                expected_launch = next(s for s in sessions if s["id"] == sid)["launch_id"]
                ready, frames = await attach(ws, sid, resume=True, launch_id=expected_launch)
                assert ready["launch_id"] != expected_launch
                assert ready["pty_instance_id"] != original_pty
                restore = next(f for f in frames if f.get("type") == "restore")
                assert restore["kind"] == "event" and "ORCA-ANSWER-731" in restore["data"]
                assert len(provider.requests) == 2  # display restore is not a model turn
                await _browser_turn(ws, sid, SECOND, "Second native answer with restored context.", launch_id=ready["launch_id"])
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


# ---------------------------------------------------------------------------
# Existing native-profile adoption E2E
# ---------------------------------------------------------------------------
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
                events = await _browser_turn(ws, sid, FIRST, "First native answer ORCA-ANSWER-731.", launch_id=ready["launch_id"])
                _private_absent(events)
                events = await _browser_turn(ws, sid, SECOND, "Second native answer with restored context.", launch_id=ready["launch_id"])
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
