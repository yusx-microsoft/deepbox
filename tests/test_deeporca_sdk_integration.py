"""Opt-in, offline integration of the REAL SDK through DeepBox's spawn worker.

Run with an interpreter already containing both projects' dependencies::

    set AGENTBRIDGE_DEEPORCA_SOURCE=C:/repos-gim/deeporca
    python -m pytest -q tests/test_deeporca_sdk_integration.py

No SDK/runtime/probe is mocked. Only the OpenAI-compatible LOCAL provider is
scripted. llm_config's on-disk contract is config.yaml's ``llm`` block
(``provider: openai``, model, base_url, explicit context_window), with the fake
``LLM_API_KEY`` in .env. "openai" selects the API dialect, NOT an external
provider: base_url always points at the test-owned loopback server.
The source tree is read-only. All profiles, projects and databases are temporary.
Child interpreters install a socket audit guard BEFORE SDK import, rejecting
DNS/connections except this test's numeric loopback endpoint (and Windows
stdlib socketpair's loopback self-pipe). This is a test
guard, not a security sandbox for arbitrary/untrusted SDK code.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from connector.integrations.deeporca.store import DeepOrcaStore
from connector.local_store import LocalProjectStore
from connector.spool import DiskSpool
from connector.supervisor import SessionSupervisor


SOURCE = os.environ.get("AGENTBRIDGE_DEEPORCA_SOURCE")
pytestmark = pytest.mark.skipif(not SOURCE, reason="set AGENTBRIDGE_DEEPORCA_SOURCE to opt in")
MODEL = "hermetic-local-model"
KEY = "hermetic-fake-key-not-a-secret"
FIRST = "Remember the hermetic context token: ORCA-CONTEXT-731."
SECOND = "Continue the previous conversation after a fresh worker start."
READ = "Exercise a workspace read."
DENY = "Exercise a read requiring approval."


@pytest.fixture(autouse=True)
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


async def _turn(sup, store, prompt, input_id, *, session_id="chat", expected="completed"):
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
async def test_real_sdk_ready_turn_persistence_and_restart(hermetic_sdk):
    env = hermetic_sdk
    sup, local, store, spool = _supervisor(env)
    try:
        first_pid = await _ready(sup, env)
        first = await _turn(sup, store, FIRST, "first-input")
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
        second = await _turn(sup, store, SECOND, "second-input")
        assert "Second native answer with restored context." == "".join(
            e.get("text", "") for e in second if e.get("ev") == "message.delta")
        messages = env.provider.requests[first_request_count]["messages"]
        assert any(m["role"] == "user" and m["content"] == FIRST for m in messages)
        assert any(m["role"] == "assistant" and "ORCA-ANSWER-731" in m.get("content", "") for m in messages)
        assert any(m["role"] == "user" and m["content"] == SECOND for m in messages)
    finally:
        await _close(sup, local, store, spool)


@pytest.mark.asyncio
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
            await _turn(sup, store, prompt, "web-config-" + str(attempt))
            assert KEY not in json.dumps(list(sup.pending))
            assert KEY not in json.dumps(store.worker_binding("hermetic-agent"))
            assert public_key_info(local.path) == public
        finally:
            await _close(sup, local, store, spool)
    assert any(m["role"] == "user" and m["content"] == FIRST
               for m in env.provider.requests[-1]["messages"])


@pytest.mark.asyncio
async def test_real_sdk_deleted_native_chat_rejects_without_replay_and_fresh_chat_works(hermetic_sdk):
    env = hermetic_sdk
    sup, local, store, spool = _supervisor(env)
    try:
        await _ready(sup, env)
        await _turn(sup, store, FIRST, "before-native-loss")
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
        failed = await _turn(sup, store, SECOND, "after-native-loss", expected="error")
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
        await _turn(sup, store, FIRST, "fresh-after-native-loss", session_id="fresh-chat")
        assert len(env.provider.requests) == 2
        assert sum(m["role"] == "user" for m in env.provider.requests[-1]["messages"]) == 1
    finally:
        await _close(sup, local, store, spool)


@pytest.mark.asyncio
async def test_real_sdk_workspace_read_and_failfast_approval_denial(hermetic_sdk):
    env = hermetic_sdk
    sup, local, store, spool = _supervisor(env)
    try:
        await _ready(sup, env)
        events = await _turn(sup, store, READ, "read-input")
        calls = [e for e in events if e.get("ev") == "tool.call"]
        results = [e for e in events if e.get("ev") == "tool.result"]
        assert calls and results and calls[0]["tool_id"] == results[0]["tool_id"] == "call-hermetic-read"
        assert "WORKSPACE-READ-731" in json.dumps(results)
        started = time.monotonic()
        events = await _turn(sup, store, DENY, "deny-input")
        assert time.monotonic() - started < 10, "approval must deny, never wait for UI"
        results = [e for e in events if e.get("ev") == "tool.result"]
        assert results and results[0]["tool_id"] == "call-hermetic-deny"
        assert "DO-NOT-EXPOSE-731" not in json.dumps(events)
        assert any(word in json.dumps(results).lower() for word in ("denied", "blocked", "approval")), results
        assert not any("approval" in e.get("ev", "") or "permission" in e.get("ev", "") for e in events)
    finally:
        await _close(sup, local, store, spool)
