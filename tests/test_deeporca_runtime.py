# ---------------------------------------------------------------------------
# Runtime registration and protocol metadata
# ---------------------------------------------------------------------------

import json
from pathlib import Path
import subprocess
import sys

import pytest

from connector import runtimes
from connector.runtime_probe import ProbeResult, availability, probe_family
from connector.integrations.deeporca.store import BindingError, DeepOrcaStore, enrollment_namespace


def test_library_descriptor_has_no_cli_or_approval_arguments():
    from connector.integrations.deeporca.probe import probe

    adapter = runtimes.get("deeporca")
    assert adapter.capability_probe is probe
    assert adapter.backend == "python-library"
    assert adapter.base_argv == ()
    assert adapter.surface_id == "structured"
    assert adapter.permission_modes == {}
    with pytest.raises(runtimes.InvalidCommandError, match="library"):
        runtimes.build_command("deeporca")


def test_integration_imports_and_worker_pickling_do_not_import_native_sdk():
    # Spawn imports the target by its module name. A clean interpreter verifies
    # both the relocated import boundary and the real pickled entrypoint, with
    # no compatibility aliases and no import of an installed/user-local SDK.
    code = "\n".join((
        "import pickle, sys",
        "from connector import runtime_probe, runtimes, supervisor",
        "from connector.integrations.deeporca import adapter, events, probe, session, store, worker",
        "assert worker._worker_main.__module__ == 'connector.integrations.deeporca.worker'",
        "assert pickle.loads(pickle.dumps(worker._worker_main)) is worker._worker_main",
        "assert adapter.create_adapter().capability_probe is probe.probe",
        "assert not any(n == 'deeporca' or n.startswith('deeporca.') for n in sys.modules)",
        "assert not any(n.startswith('connector.deeporca_') for n in sys.modules)",
    ))
    result = subprocess.run([sys.executable, "-c", code],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("data, installed, compatible", [
    ({"installed": False}, False, False),
    ({"installed": True, "api": 1, "version": "0.8.0"}, True, True),
    ({"installed": True, "api": 2}, True, False),
    ({"installed": True, "api": True}, True, False),
    ({"installed": True, "error": "embedded_api_unavailable"}, True, False),
])
def test_optional_library_probe(data, installed, compatible):
    calls = []
    def runner(argv, timeout):
        calls.append((argv, timeout))
        return ProbeResult(0, json.dumps(data))
    cap = probe_family("deeporca", runner=runner)
    assert len(calls) == 1 and calls[0][0][1] == "-c"
    assert (cap["installation"]["status"] == "installed") is installed
    assert cap["installation"]["guidance"] == {"url": "https://aka.ms/deeporca"}
    assert availability(cap, "structured")[0] is compatible
    assert cap["agent_config"]["profile_modes"] == ["create"]
    features = cap["surfaces"][0]["features"]
    assert features["renderer"] == "deeporca-chat-v1"
    assert features["interactive_approval"] is False
    assert features["permission_modes"] == []


def test_probe_never_publishes_raw_diagnostics():
    raw = {"installed": True, "api": 1, "version": "C:/private/token=secret"}
    cap = probe_family("deeporca", runner=lambda *_: ProbeResult(0, json.dumps(raw)))
    assert "secret" not in json.dumps(cap)
    assert cap["installation"]["version"] is None
    broken = probe_family("deeporca", runner=lambda *_: ProbeResult(1, "private error"))
    assert broken["installation"]["status"] == "missing"
    assert "private error" not in json.dumps(broken)


@pytest.fixture
def store(tmp_path):
    value = DeepOrcaStore(tmp_path / "bindings.db", namespace="enrollment-one")
    yield value
    value.close()


def agent(tmp_path):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return {"id": "agent-1", "local_project_id": "project-1", "runtime": "deeporca",
            "cwd": str(project), "runtime_config": {}}


def test_bindings_idempotent_private_and_namespace_isolated(store, tmp_path):
    record = agent(tmp_path)
    first = store.ensure_binding(record)
    assert first == store.ensure_binding({**record, "display_name": "renamed"})
    assert first["profile_name"].startswith("dbx-")
    store.set_status(record["id"], "ready")
    public = store.public_status(record["id"])
    assert public["runtime_status"]["state"] == "ready"
    assert "workspace" not in json.dumps(public) and str(tmp_path) not in json.dumps(public)
    store.set_namespace("enrollment-two")
    second = store.ensure_binding(record)
    assert first["home"] != second["home"]
    assert first["profile_name"] != second["profile_name"]


def test_binding_identity_cannot_be_changed_or_silently_reactivated(store, tmp_path):
    record = agent(tmp_path)
    first = store.ensure_binding(record)
    with pytest.raises(BindingError, match="binding_identity_conflict"):
        store.ensure_binding({**record, "local_project_id": "other-project"})
    store.retire(record["id"])
    assert store.public_status(record["id"]) is None
    assert store.get_binding(record["id"])["home"] == first["home"]
    with pytest.raises(BindingError, match="binding_identity_conflict"):
        store.ensure_binding(record)


def test_native_identity_survives_reopen(tmp_path):
    path = tmp_path / "bindings.db"
    store = DeepOrcaStore(path, namespace="one")
    sid = store.native_session_id("a", "s")
    assert store.native_session_id("b", "s") != sid
    store.close()
    store = DeepOrcaStore(path, namespace="one")
    assert store.native_session_id("a", "s") == sid
    store.set_namespace("two")
    assert store.native_session_id("a", "s") != sid
    store.close()


def test_crash_admission_is_uncertain_not_replayed(tmp_path):
    path = tmp_path / "bindings.db"
    store = DeepOrcaStore(path)
    store.admit_input("a", "s", "input-1", "do not run twice")
    assert store.input_receipt("a", "s", "input-1")["state"] == "running"
    store.close()
    store = DeepOrcaStore(path)
    assert store.recover_interrupted() == [{"agent_id": "a", "session_id": "s", "input_id": "input-1"}]
    assert store.input_receipt("a", "s", "input-1")["result"] == "uncertain"
    assert store.recover_interrupted() == []
    assert store.session_recovery("a", "s")[0]["input_id"] == "input-1"
    # Receipt is retained, but user input is purged after settlement.
    assert store._conn.execute("SELECT text, options FROM inputs").fetchone()["text"] == ""
    store.close()


def test_receipts_are_scoped_and_completed_input_is_not_uncertain(store):
    store.admit_input("a", "s", "i", "hello")
    store.admit_input("b", "s", "i", "different agent")
    store.settle_input("a", "s", "i", "completed")
    assert store.input_receipt("a", "s", "i")["result"] == "completed"
    assert store.input_receipt("a", "other", "i") is None
    assert store.recover_interrupted() == [{"agent_id": "b", "session_id": "s", "input_id": "i"}]


def test_enrollment_identity_is_not_a_machine_token():
    assert enrollment_namespace("http://localhost:80/", "machine-a") == enrollment_namespace("http://localhost", "machine-a")
    assert enrollment_namespace("http://localhost", "machine-a") != enrollment_namespace("http://localhost", "machine-b")


def test_spool_ownership_is_private_per_spool_and_ack_drained(store, tmp_path):
    record = agent(tmp_path)
    old = store.ensure_binding(record)
    store.guard_spool_enrollment("spool-a", store.namespace, pending=True, native_use=True)
    with pytest.raises(BindingError, match="^enrollment_identity_conflict$"):
        store.guard_spool_enrollment("spool-a", "new-enrollment", pending=True)
    store.guard_spool_enrollment("spool-a", store.namespace, pending=True)  # token rotation
    # An independent empty spool cannot flush the old spool's private data.
    store.guard_spool_enrollment("spool-b", "new-enrollment", pending=False)
    store.guard_spool_enrollment("spool-a", "new-enrollment", pending=False)
    store.set_namespace("new-enrollment")
    assert store.ensure_binding(record)["profile_name"] != old["profile_name"]
    assert "spool" not in json.dumps(store.public_status(record["id"]))


@pytest.mark.parametrize("legacy_table", ["bindings", "native_sessions", "inputs"])
def test_unpinned_legacy_data_is_not_assigned_to_new_pending_scope(store, tmp_path, legacy_table):
    if legacy_table == "bindings":
        store.ensure_binding(agent(tmp_path))
    elif legacy_table == "native_sessions":
        store.native_session_id("a", "s")
    else:
        store.admit_input("a", "s", "i", "private")
    with pytest.raises(BindingError, match="^enrollment_identity_conflict$"):
        store.guard_spool_enrollment("spool-a", "new-enrollment", pending=True)
    assert not store._conn.execute("SELECT * FROM spool_owners").fetchall()
    # Sole known old scope can be resumed to receive genuine ACKs.
    store.guard_spool_enrollment("spool-a", store.namespace, pending=True)


def test_empty_ledger_does_not_pin_cli_only_spool(store):
    store.guard_spool_enrollment("spool", "one", pending=True)
    store.guard_spool_enrollment("spool", "two", pending=True)
    assert not store._conn.execute("SELECT * FROM spool_owners").fetchall()


def test_default_spool_is_url_token_scoped_but_not_devbox_scoped(tmp_path):
    from connector.spool import spool_path
    # Normal CLI URL/token changes already use different physical spools. The
    # extra guard protects shared/injected paths and same-token devbox changes.
    old = spool_path("http://old", "token", str(tmp_path))
    assert old == spool_path("http://OLD:80/", "token", str(tmp_path))
    assert old != spool_path("http://new", "token", str(tmp_path))
    assert old != spool_path("http://old", "rotated-token", str(tmp_path))


# ---------------------------------------------------------------------------
# Worker process and profile ownership
# ---------------------------------------------------------------------------

"""Protocol and real spawn/Pipe tests using temporary, non-network SDK fixtures."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from connector.integrations.deeporca.events import IntegrationError
from connector.integrations.deeporca.session import DeepOrcaSession
from connector.integrations.deeporca.worker import (
    DeepOrcaWorker, MAX_FRAME_BYTES, decode_frame, encode_frame,
)


SDK_FIXTURE = '''
import asyncio
import os
from pathlib import Path
EMBEDDED_API_VERSION = 1

def ensure_profile(name, *, home=None, template_dir=None):
    assert os.environ['DEEPORCA_HOME'] == home
    assert os.environ['DEEPORCA_AGENT_NAME'] == name
    assert os.getcwd() == os.environ['DEEPORCA_WORKSPACE']
    assert home is not None
    print('not IPC: SDK writes arbitrary stdout')
    if name == 'missing':
        raise SystemExit('/private/profile/token=SECRET')
    return {'profile': name, 'created': True, 'configured': name != 'unconfigured'}

class EmbeddedRuntime:
    def __init__(self, profile_name, workspace, *, home=None):
        assert home is not None
        self.workspace = workspace
        self.task = None
    async def start(self):
        Path(self.workspace, 'runtime-started').write_text('yes')
    async def run_turn(self, session_id, text, *, message_id, model=None, on_event):
        self.task = asyncio.current_task()
        await on_event({'type':'text', 'turn_id':'native-turn', 'sequence':1,
                        'content': 'hello ' + session_id})
        if text == 'approval':
            try:
                await on_event({'type':'approval_request', 'turn_id':'native-turn', 'sequence':2})
            except Exception:
                # Even an incorrectly swallowed sink error must fail closed.
                await asyncio.Event().wait()
        if text == 'wait':
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                Path(self.workspace, 'native-saved').write_text('cancelled')
                return {'status':'cancelled', 'turn_id':'native-turn'}
        await on_event({'type':'done', 'turn_id':'native-turn', 'sequence':2})
        if text == 'save-error':
            raise RuntimeError('private credential SECRET in exception')
        Path(self.workspace, 'native-saved').write_text('completed')
        return {'status':'completed', 'turn_id':'native-turn'}
    async def interrupt(self):
        if self.task and not self.task.done():
            self.task.cancel()
    async def close(self):
        await self.interrupt()
'''


def make_binding(tmp_path, sdk=SDK_FIXTURE, name="fixture"):
    source = tmp_path / "source"
    package = source / "deeporca"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "embedded.py").write_text(sdk, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "isolated-home"
    home.mkdir()
    return {"agent_id": "agent-1", "profile_name": name, "home": str(home),
            "workspace": str(workspace), "source_path": str(source)}


async def worker_until(predicate):
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def make_session(worker, events, settlements):
    async def output(text):
        events.append(json.loads(text))
    async def exited(code):
        pass
    async def settled(message_id, status):
        assert events[-1]["ev"] == "turn.end"
        settlements.append(status)
    session = DeepOrcaSession(worker, "chat-1", output, exited, on_turn_settled=settled)
    await session.start()
    return session


@pytest.mark.parametrize("payload", [
    b'pickle-not-json', b'[]', b'{"v":2}', b'{"v":true}', b'{"v":1,"v":1}',
    b'{"v":1,"bad":NaN}', b'\xff', b'{"v":1,"bad":Infinity}',
])
def test_bad_frames_are_rejected_without_deserialization(payload):
    with pytest.raises(IntegrationError):
        decode_frame(payload)


def test_protocol_bounds_and_json_only():
    with pytest.raises(IntegrationError, match="output_limit"):
        encode_frame({"v": 1, "data": "x" * MAX_FRAME_BYTES})
    with pytest.raises(IntegrationError, match="output_limit"):
        decode_frame(b"x" * (MAX_FRAME_BYTES + 1))
    with pytest.raises(IntegrationError, match="invalid_protocol"):
        encode_frame({"v": 1, "object": object()})
    frame = {"v": 1, "kind": "event", "content": "\U0001f40b"}
    assert decode_frame(encode_frame(frame)) == frame


def test_binding_requires_explicit_local_absolute_paths(tmp_path):
    with pytest.raises(IntegrationError, match="invalid_binding"):
        DeepOrcaWorker({"agent_id": "a", "profile_name": "default", "workspace": str(tmp_path)})
    with pytest.raises(IntegrationError, match="invalid_binding"):
        DeepOrcaWorker({"agent_id": "a", "profile_name": "../escape", "home": str(tmp_path),
                        "workspace": str(tmp_path)})


def test_real_spawn_runtime_events_and_environment_isolation(tmp_path):
    binding = make_binding(tmp_path)
    env, cwd = dict(os.environ), os.getcwd()
    async def case():
        worker = DeepOrcaWorker(binding)
        events, settlements = [], []
        try:
            session = await make_session(worker, events, settlements)
            assert worker.is_alive()
            ack = await session.submit_input("input-1", "hello")
            assert ack["status"] == "delivered"
            await worker_until(lambda: bool(settlements))
            assert settlements == ["completed"]
            assert Path(binding["workspace"], "native-saved").read_text() == "completed"
            assert [e["text"] for e in events if e["ev"] == "message.delta"] == ["hello chat-1"]
            assert sum(e["ev"] == "turn.end" for e in events) == 1
        finally:
            await worker.close()
        assert not worker.is_alive()
    asyncio.run(case())
    assert os.getcwd() == cwd and dict(os.environ) == env


def test_provision_is_lightweight_and_does_not_construct_runtime(tmp_path):
    binding = make_binding(tmp_path, name="unconfigured")
    async def case():
        worker = DeepOrcaWorker(binding)
        info = await worker.provision()
        assert info == {"profile": "unconfigured", "created": True, "configured": False,
                        "embedded_api_version": 1, "status": "configuration_required"}
        assert not worker.is_alive()
        assert not Path(binding["workspace"], "runtime-started").exists()
    asyncio.run(case())


@pytest.mark.parametrize("name,code", [("missing", "profile_unavailable"),
                                      ("unconfigured", "configuration_required")])
def test_missing_profile_and_config_failures_are_sanitized(tmp_path, name, code):
    binding = make_binding(tmp_path, name=name)
    async def case():
        worker = DeepOrcaWorker(binding)
        with pytest.raises(IntegrationError, match=code) as caught:
            await worker.start()
        assert str(caught.value) == code
        assert "SECRET" not in str(caught.value)
        assert not worker.is_alive()
        await worker.close()
    asyncio.run(case())


@pytest.mark.parametrize("sdk,code", [
    ("raise ImportError('private credential SECRET')", "sdk_missing"),
    ("EMBEDDED_API_VERSION = 99", "sdk_incompatible"),
    ("raise RuntimeError('private credential SECRET')", "startup_failed"),
])
def test_sdk_startup_errors_do_not_escape_worker(tmp_path, sdk, code):
    binding = make_binding(tmp_path, sdk=sdk)
    async def case():
        worker = DeepOrcaWorker(binding)
        with pytest.raises(IntegrationError) as caught:
            await worker.start()
        assert str(caught.value) == code
        assert not worker.is_alive()
    asyncio.run(case())


def test_real_spawn_interrupt_cancels_native_task_and_saves(tmp_path):
    binding = make_binding(tmp_path)
    async def case():
        worker = DeepOrcaWorker(binding, interrupt_timeout=2)
        events, settlements = [], []
        try:
            session = await make_session(worker, events, settlements)
            await session.submit_input("input-1", "wait")
            await worker_until(lambda: any(e["ev"] == "message.delta" for e in events))
            await session.interrupt()
            assert settlements == ["cancelled"]
            assert worker.can_accept_turn()
            assert Path(binding["workspace"], "native-saved").read_text() == "cancelled"
        finally:
            await worker.close()
    asyncio.run(case())


@pytest.mark.parametrize("form", ["result", "exception", "contradictory"])
def test_missing_native_context_is_safe_nonfatal_error_not_completion(tmp_path, form):
    failure = ("raise type('ContextUnavailable', (RuntimeError,), "
               "{'code':'native_context_unavailable'})('/private/history SECRET')"
               if form == "exception" else
               "return {'status':'error', 'turn_id':'native-turn', 'code':'native_context_unavailable'}")
    if form == "contradictory":
        failure = failure.replace("'status':'error'", "'status':'completed'")
    sdk = SDK_FIXTURE.replace("self.task = asyncio.current_task()",
        "self.task = asyncio.current_task()\n        if text == 'missing-context':\n            " + failure)
    binding = make_binding(tmp_path, sdk=sdk)
    async def case():
        worker = DeepOrcaWorker(binding)
        events, settlements = [], []
        try:
            session = await make_session(worker, events, settlements)
            assert (await session.submit_input("missing", "missing-context"))["status"] == "delivered"
            await worker_until(lambda: bool(settlements))
            assert settlements == ["error"]
            errors = [e for e in events if e["ev"] == "error"]
            assert len(errors) == 1
            assert errors[0]["code"] == "native_context_unavailable"
            assert "Start a new conversation" in errors[0]["message"]
            ends = [e for e in events if e["ev"] == "turn.end"]
            assert len(ends) == 1 and ends[0]["status"] == "error"
            assert ends[0]["code"] == "native_context_unavailable"
            assert not any(e["ev"] == "message.delta" for e in events)
            assert "SECRET" not in json.dumps(events) and "/private" not in json.dumps(events)
            assert not Path(binding["workspace"], "native-saved").exists()
            assert worker.is_alive() and worker.can_accept_turn()
            # A different native session on the same process still works.
            other = await make_session(worker, events, settlements)
            other.session_id = "fresh-chat"
            assert (await other.submit_input("fresh", "hello"))["status"] == "delivered"
            await worker_until(lambda: len(settlements) == 2)
            assert settlements == ["error", "completed"]
        finally:
            await worker.close()
    asyncio.run(case())


def test_real_spawn_save_failure_after_done_is_uncertain(tmp_path):
    binding = make_binding(tmp_path)
    async def case():
        worker = DeepOrcaWorker(binding)
        events, settlements = [], []
        try:
            session = await make_session(worker, events, settlements)
            await session.submit_input("input-1", "save-error")
            await worker_until(lambda: bool(settlements))
            assert settlements == ["uncertain"]
            assert "SECRET" not in json.dumps(events)
            assert sum(e["ev"] == "turn.end" for e in events) == 1
            assert not worker.is_alive()
        finally:
            await worker.close()
    asyncio.run(case())


def test_real_spawn_approval_fails_even_if_sdk_swallows_sink_exception(tmp_path):
    binding = make_binding(tmp_path)
    async def case():
        worker = DeepOrcaWorker(binding)
        events, settlements = [], []
        try:
            session = await make_session(worker, events, settlements)
            await session.submit_input("input-1", "approval")
            await worker_until(lambda: bool(settlements))
            assert settlements == ["error"]
            assert not worker.is_alive()
            assert all("approval" not in e["ev"] for e in events)
        finally:
            await worker.close()
    asyncio.run(case())


def test_real_spawn_timeout_terminates_owned_process_only(tmp_path):
    sdk = SDK_FIXTURE.replace("self.task.cancel()", "pass  # deliberately uncooperative SDK")
    binding = make_binding(tmp_path, sdk=sdk)
    other_dir = tmp_path / "other-agent"
    other_dir.mkdir()
    other_binding = make_binding(other_dir)
    async def case():
        worker = DeepOrcaWorker(binding, interrupt_timeout=0.05)
        other = DeepOrcaWorker(other_binding)
        events, settlements = [], []
        try:
            await other.start()
            session = await make_session(worker, events, settlements)
            await session.submit_input("input-1", "wait")
            await worker_until(lambda: any(e["ev"] == "message.delta" for e in events))
            await session.interrupt()
            assert settlements == ["uncertain"]
            assert events[-1]["interrupted"]
            assert not worker.is_alive()
            assert other.is_alive() and other.can_accept_turn()
            assert not Path(binding["workspace"], "native-saved").exists()
        finally:
            await worker.close()
            await other.close()
    asyncio.run(case())


def test_real_spawn_startup_timeout_is_bounded_and_cleaned_up(tmp_path):
    sdk = SDK_FIXTURE.replace("async def start(self):", "async def start(self):\n        await asyncio.Event().wait()")
    binding = make_binding(tmp_path, sdk=sdk)
    async def case():
        worker = DeepOrcaWorker(binding, startup_timeout=0.5)
        with pytest.raises(IntegrationError, match="startup_failed"):
            await asyncio.wait_for(worker.start(), 3)
        assert not worker.is_alive()
        assert not worker._transport.is_alive()
        await worker.close()
    asyncio.run(case())


def test_real_spawn_sequential_turns_share_worker_but_use_native_sessions(tmp_path):
    binding = make_binding(tmp_path)
    async def case():
        worker = DeepOrcaWorker(binding)
        first_events, second_events, settlements = [], [], []
        try:
            first = await make_session(worker, first_events, settlements)
            async def output(text):
                second_events.append(json.loads(text))
            async def exited(code):
                pass
            async def settled(message_id, status):
                settlements.append(status)
            second = DeepOrcaSession(worker, "chat-2", output, exited, on_turn_settled=settled)
            await second.start()
            for index in range(4):
                session = first if index % 2 == 0 else second
                assert (await session.submit_input(f"input-{index}", "hi"))["status"] == "delivered"
                await worker_until(lambda: worker.can_accept_turn())
            assert settlements == ["completed"] * 4
            assert [e["text"] for e in first_events if e["ev"] == "message.delta"] == ["hello chat-1"] * 2
            assert [e["text"] for e in second_events if e["ev"] == "message.delta"] == ["hello chat-2"] * 2
        finally:
            await worker.close()
    asyncio.run(case())


# ---------------------------------------------------------------------------
# Session durability and event handling
# ---------------------------------------------------------------------------

"""Hermetic facade/event/admission tests: no SDK installation or model calls."""
import asyncio
import json

import pytest

from connector.integrations.deeporca.events import (
    IntegrationError, MAX_NATIVE_BYTES, prepare_native_event,
    translate_deeporca_event, validate_input,
)
from connector.integrations.deeporca.session import DeepOrcaSession
from connector.integrations.deeporca.worker import DeepOrcaWorker


class FakeTransport:
    def __init__(self, binding, *, provision_only=False):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.alive = True
        self.closed_force = None
        self.active_token = None
        self.on_send = None

    async def send(self, frame):
        self.sent.append(frame)
        if frame["op"] == "init":
            self.put({"kind": "ready", "info": {"configured": True}})
        if frame["op"] == "run":
            self.active_token = frame["token"]
        if self.on_send:
            await self.on_send(frame)

    def put(self, frame):
        self.incoming.put_nowait(dict(frame, v=1))

    def event(self, kind, sequence, **fields):
        self.put({"kind": "event", "token": self.active_token,
                  "event": dict(type=kind, turn_id="turn-1", sequence=sequence, **fields)})

    def result(self, status="completed"):
        self.put({"kind": "result", "token": self.active_token,
                  "result": {"status": status, "turn_id": "turn-1"}})

    async def receive(self):
        value = await self.incoming.get()
        if isinstance(value, BaseException):
            raise value
        return value

    def is_alive(self):
        return self.alive

    async def close(self, force=False):
        self.alive = False
        self.closed_force = force


def binding(tmp_path):
    return {"agent_id": "a1", "profile_name": "profile", "home": str(tmp_path / "home"),
            "workspace": str(tmp_path)}


async def harness(tmp_path, *, timeout=0.1, output=None):
    transports = []
    def factory(*args, **kwargs):
        transport = FakeTransport(*args, **kwargs)
        transports.append(transport)
        return transport
    worker = DeepOrcaWorker(binding(tmp_path), transport_factory=factory, interrupt_timeout=timeout)
    events, exits, settlements = [], [], []
    async def emit(text):
        event = json.loads(text)
        if output:
            await output(event)
        events.append(event)
    async def exited(code):
        exits.append(code)
    async def settled(message_id, status):
        assert events[-1]["ev"] == "turn.end"
        settlements.append((message_id, status))
    session = DeepOrcaSession(worker, "session-1", emit, exited, on_turn_settled=settled)
    await session.start()
    return worker, session, transports[0], events, exits, settlements


async def wait_until(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


def native(kind, sequence=1, **fields):
    return dict(type=kind, turn_id="turn-1", sequence=sequence, **fields)


def test_incremental_text_and_native_envelope():
    first = translate_deeporca_event(native("text", content="Hel"))[0]
    second = translate_deeporca_event(native("text", sequence=2, content="lo"))[0]
    assert first["ev"] == "message.delta"
    assert first["text"] + second["text"] == "Hello"
    assert first["native"]["schema"] == "deeporca.turn.v1"
    assert first["native"]["sequence"] == 1
    assert first["event_id"] != second["event_id"]
    assert translate_deeporca_event(native("done")) == []
    assert translate_deeporca_event(native("thinking", content="hmm"))[0]["ev"] == "thinking.delta"


def test_tool_pairing_uses_ids_and_preserves_parent_and_refusal():
    a = translate_deeporca_event(native("tool_call", tool_call_id="a", name="shell",
                                       arguments={"cmd": "first"}, parent_id="parent"))[0]
    b = translate_deeporca_event(native("tool_call", 2, tool_call_id="b", name="shell"))[0]
    result = translate_deeporca_event(native("tool_result", 3, tool_call_id="a", name="shell",
                                            blocked=True, content="Review unavailable"))[0]
    assert a["tool_id"] == result["tool_id"] != b["tool_id"]
    assert a["parent_id"] == "parent"
    assert a["input"] == {"cmd": "first"}
    assert result["is_error"] and result["blocked"]
    with pytest.raises(IntegrationError, match="missing_tool_id"):
        translate_deeporca_event(native("tool_call", name="shell"))


@pytest.mark.parametrize("kind", ["approval_request", "permission.ask", "approval", "control_request"])
def test_approval_is_a_contract_failure(kind):
    with pytest.raises(IntegrationError, match="approval_contract_violation"):
        translate_deeporca_event(native(kind))


def test_large_tool_output_is_explicit_preview_without_mutating_source():
    event = native("tool_result", tool_call_id="tool-a", content="\U0001f40b" * MAX_NATIVE_BYTES)
    prepared = prepare_native_event(event)
    assert prepared["truncated"] is True
    assert len(prepared["content"]) < len(event["content"])
    assert prepared["original_bytes"] > MAX_NATIVE_BYTES
    assert translate_deeporca_event(prepared)[0]["truncated"]
    with pytest.raises(IntegrationError, match="output_limit"):
        prepare_native_event(native("text", content="x" * (MAX_NATIVE_BYTES + 1)))


@pytest.mark.parametrize("text,options", [
    ("", None), ("  ", None), (None, None), ("hello", []),
    ("hello", {"attachments": []}), ("hello", {"permissions": "full"}),
    ("hello", {"source_path": "C:/evil"}), ("hello", {"model": "bad\nmodel"}),
    ("hello", {"model": []}), ("x" * 100000, None), ("\ud800", None),
], ids=["empty", "blank", "null", "array-options", "attachments", "permissions",
        "source-path", "model-newline", "model-list", "oversized", "surrogate"])
def test_input_allowlist(text, options):
    with pytest.raises(IntegrationError):
        validate_input(text, options)


def test_model_validation():
    assert validate_input("hello", {"model": "provider/model-v1:latest"}) == {
        "model": "provider/model-v1:latest"}
    assert validate_input("hello") == {}


def test_cross_session_reservation_is_immediate_and_admission_precedes_dispatch(tmp_path):
    async def case():
        worker, first, transport, events, _, settlements = await harness(tmp_path)
        other_events = []
        async def other_output(text):
            other_events.append(json.loads(text))
        async def no_exit(code):
            pass
        second = DeepOrcaSession(worker, "session-2", other_output, no_exit)
        await second.start()
        def admit():
            assert not worker.can_accept_turn()
            assert not any(frame["op"] == "run" for frame in transport.sent)
            assert [e["ev"] for e in events] == ["session.config"]
        ack = await first.submit_input("message-1", "Hello", admit=admit)
        assert ack["status"] == "delivered"
        assert await second.submit_input("message-2", "other") == {
            "status": "rejected", "reason": "runtime_busy"}
        await wait_until(lambda: transport.active_token is not None)
        transport.event("text", 1, content="hello")
        transport.event("done", 2)
        await wait_until(lambda: any(e["ev"] == "message.delta" for e in events))
        await asyncio.sleep(0)
        assert not settlements
        assert not any(e["ev"] == "turn.end" for e in events)
        transport.result()
        await wait_until(lambda: worker.can_accept_turn())
        assert settlements == [("message-1", "completed")]
        assert [e["ev"] for e in events].count("turn.end") == 1
        assert [e["ev"] for e in events].count("turn.start") == 1
        assert other_events == [events[0]]
        await worker.close()
    asyncio.run(case())


def test_failed_admit_releases_slot_without_echo_or_ipc(tmp_path):
    async def case():
        worker, session, transport, events, _, _ = await harness(tmp_path)
        def bad_admit():
            raise OSError("disk full")
        with pytest.raises(OSError):
            await session.submit_input("id", "hi", admit=bad_admit)
        assert worker.can_accept_turn()
        assert len(events) == 1
        assert not any(frame["op"] == "run" for frame in transport.sent)
        await worker.close()
    asyncio.run(case())


def test_cancellation_targets_runtime_and_waits_for_saved_result(tmp_path):
    async def case():
        worker, session, transport, events, _, settlements = await harness(tmp_path)
        await session.submit_input("id", "hi")
        await wait_until(lambda: transport.active_token is not None)
        async def on_send(frame):
            if frame["op"] == "interrupt":
                await asyncio.sleep(0.01)
                transport.result("cancelled")
        transport.on_send = on_send
        await session.interrupt()
        assert settlements == [("id", "cancelled")]
        assert worker.can_accept_turn()
        assert events[-1]["status"] == "cancelled"
        assert not events[-1]["uncertain"]
        await worker.close()
    asyncio.run(case())


def test_interrupt_before_dispatch_does_not_run_a_turn(tmp_path):
    async def case():
        worker, session, transport, _, _, settlements = await harness(tmp_path)
        await session.submit_input("id", "hi")
        await session.interrupt()
        assert not any(frame["op"] == "run" for frame in transport.sent)
        assert settlements == [("id", "cancelled")]
        await worker.close()
    asyncio.run(case())


def test_uncooperative_interrupt_retires_epoch_and_drops_late_output(tmp_path):
    async def case():
        worker, session, transport, events, _, settlements = await harness(tmp_path, timeout=0.02)
        await session.submit_input("id", "hi")
        await wait_until(lambda: transport.active_token is not None)
        await session.interrupt()
        assert not worker.is_alive()
        assert transport.closed_force is True
        assert settlements == [("id", "uncertain")]
        count = len(events)
        transport.event("text", 1, content="late")
        transport.result()
        await asyncio.sleep(0.02)
        assert len(events) == count
        assert events[-1]["uncertain"]
        assert events[-1]["status"] != "completed"
        assert (await session.submit_input("id-2", "again"))["status"] == "rejected"
    asyncio.run(case())


def test_facade_close_does_not_kill_turn_or_other_viewers(tmp_path):
    async def case():
        worker, first, transport, events, exits, settlements = await harness(tmp_path)
        async def ignore(*args):
            pass
        second = DeepOrcaSession(worker, "session-2", ignore, ignore)
        await second.start()
        await first.submit_input("id", "hi")
        await wait_until(lambda: transport.active_token is not None)
        await second.interrupt()
        assert not any(frame["op"] == "interrupt" for frame in transport.sent)
        await first.close()
        assert worker.is_alive() and second.is_alive() and not first.is_alive()
        transport.result()
        await wait_until(lambda: bool(settlements))
        assert settlements == [("id", "completed")]
        assert exits == [0]
        assert second.can_accept_turn()
        await second.close()
        assert worker.is_alive()
        await worker.close()
    asyncio.run(case())


def test_unexpected_approval_kills_worker_without_approval_traffic(tmp_path):
    async def case():
        worker, session, transport, events, _, settlements = await harness(tmp_path)
        await session.submit_input("id", "hi")
        await wait_until(lambda: transport.active_token is not None)
        transport.event("approval_request", 1, request_id="secret")
        await wait_until(lambda: bool(settlements))
        assert settlements == [("id", "error")]
        assert not worker.is_alive()
        assert not any("approval" in event["ev"] or "permission" in event["ev"] for event in events)
        assert events[-2]["code"] == "approval_contract_violation"
        assert {f["op"] for f in transport.sent} == {"init", "run"}
    asyncio.run(case())


def test_worker_crash_never_claims_completion(tmp_path):
    async def case():
        worker, session, transport, events, _, settlements = await harness(tmp_path)
        await session.submit_input("id", "hi")
        await wait_until(lambda: transport.active_token is not None)
        transport.event("done", 1)
        transport.incoming.put_nowait(EOFError())
        await wait_until(lambda: bool(settlements))
        assert settlements == [("id", "uncertain")]
        assert events[-1]["status"] == "uncertain"
        assert not worker.can_accept_turn()
    asyncio.run(case())


def test_settlement_waits_for_durable_output_and_failure_does_not_mark_settled(tmp_path):
    async def case():
        gate = asyncio.Event()
        final_started = asyncio.Event()
        async def output(event):
            if event["ev"] == "turn.end":
                final_started.set()
                await gate.wait()
                raise OSError("full spool")
        worker, session, transport, events, _, settlements = await harness(tmp_path, output=output)
        await session.submit_input("id", "hi")
        await wait_until(lambda: transport.active_token is not None)
        transport.result()
        await asyncio.wait_for(final_started.wait(), 1)
        assert not settlements
        assert not worker.can_accept_turn()
        gate.set()
        await wait_until(lambda: not worker.is_alive())
        assert not settlements
        assert not any(e["ev"] == "turn.end" for e in events)
    asyncio.run(case())


def test_wrong_token_is_ignored_and_bad_sequence_fails_closed(tmp_path):
    async def case():
        worker, session, transport, events, _, settlements = await harness(tmp_path)
        await session.submit_input("id", "hi")
        await wait_until(lambda: transport.active_token is not None)
        transport.put({"kind": "event", "token": "old-epoch", "event": native("text", content="late")})
        transport.event("text", 2, content="real")
        transport.event("text", 1, content="reordered")
        await wait_until(lambda: bool(settlements))
        assert [e["text"] for e in events if e["ev"] == "message.delta"] == ["real"]
        assert settlements == [("id", "uncertain")]
    asyncio.run(case())


def test_opaque_provider_ids_are_not_treated_as_paths():
    projected = translate_deeporca_event(native("tool_call", tool_call_id="provider/A+b==", name="shell"))[0]
    assert projected["tool_id"] == "provider/A+b=="


def test_json_escape_expansion_rejected_before_admission():
    with pytest.raises(IntegrationError, match="input_too_large"):
        validate_input("a" + "\x01" * 20000)


def test_async_admission_callback_is_rejected_without_dispatch(tmp_path):
    async def case():
        worker, session, transport, events, _, _ = await harness(tmp_path)
        async def incorrect_admission():
            raise AssertionError("must not run")
        with pytest.raises(TypeError, match="synchronous"):
            await session.submit_input("id", "hi", admit=incorrect_admission)
        assert worker.can_accept_turn()
        assert len(events) == 1
        assert not any(f["op"] == "run" for f in transport.sent)
        await worker.close()
    asyncio.run(case())


def test_concurrent_interrupts_only_cancel_native_task_once(tmp_path):
    async def case():
        worker, session, transport, _, _, settlements = await harness(tmp_path)
        await session.submit_input("id", "hi")
        await wait_until(lambda: transport.active_token is not None)
        async def on_send(frame):
            if frame["op"] == "interrupt":
                await asyncio.sleep(0.01)
                transport.result("cancelled")
        transport.on_send = on_send
        await asyncio.gather(session.interrupt(), session.interrupt())
        assert settlements == [("id", "cancelled")]
        assert sum(f["op"] == "interrupt" for f in transport.sent) == 1
        await worker.close()
    asyncio.run(case())


def test_output_limit_fails_closed_without_forwarding_oversized_frame(tmp_path):
    async def case():
        worker, session, transport, events, _, settlements = await harness(tmp_path)
        await session.submit_input("id", "hi")
        await wait_until(lambda: transport.active_token is not None)
        transport.event("text", 1, content="x" * (MAX_NATIVE_BYTES + 1))
        await wait_until(lambda: bool(settlements))
        assert not any(e["ev"] == "message.delta" for e in events)
        assert events[-2]["code"] == "output_limit"
        assert settlements == [("id", "uncertain")]
        assert not worker.is_alive()
    asyncio.run(case())


def test_settlement_callback_can_retire_worker_without_self_deadlock(tmp_path):
    async def case():
        worker, session, transport, events, _, _ = await harness(tmp_path)
        settled = []
        async def on_settled(message_id, status):
            await worker.close()
            settled.append(status)
        session.on_turn_settled = on_settled
        await session.submit_input("id", "hi")
        await wait_until(lambda: transport.active_token is not None)
        transport.result()
        await wait_until(lambda: bool(settled))
        assert settled == ["completed"]
        assert not worker.is_alive()
    asyncio.run(case())


# ---------------------------------------------------------------------------
# Supervisor lifecycle and control routing
# ---------------------------------------------------------------------------

"""Offline end-to-end: Supervisor -> real spawned library worker -> durable spool.

Only the SDK module is a temporary fake. No server, model or private profile is
used. This exercises contracts which unit mocks of the facade cannot cover.
"""
import asyncio
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from connector.integrations.deeporca.store import DeepOrcaStore
from connector.local_store import LocalProjectStore
from connector.runtime_probe import ProbeResult, probe_family
from connector.supervisor import SessionSupervisor


SDK = '''
import asyncio
from pathlib import Path
EMBEDDED_API_VERSION = 1
def ensure_profile(name, *, home=None, template_dir=None):
    p = Path(home) / 'agents' / name
    p.mkdir(parents=True, exist_ok=True)
    (p / '.state.json').write_text('{}')
    return {'profile':name, 'created':True, 'configured':True}
class EmbeddedRuntime:
    def __init__(self, profile_name, workspace, *, home=None):
        self.workspace = Path(workspace)
        self.task = None
    async def start(self): pass
    async def run_turn(self, session_id, text, *, message_id, model=None, on_event):
        self.task = asyncio.current_task()
        turn = 'turn-' + message_id
        counter = self.workspace / 'execution-count'
        counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else '1')
        await on_event({'type':'text','content':'Hello 世界','turn_id':turn,'sequence':1})
        try:
            if text == 'wait': await asyncio.Event().wait()
            await on_event({'type':'done','turn_id':turn,'sequence':2})
            (self.workspace / ('native-' + session_id)).write_text('completed')
            return {'status':'completed','turn_id':turn}
        except asyncio.CancelledError:
            (self.workspace / ('native-' + session_id)).write_text('cancelled')
            return {'status':'cancelled','turn_id':turn}
    async def interrupt(self):
        if self.task is not None and not self.task.done(): self.task.cancel()
    async def close(self): await self.interrupt()
'''


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    source = tmp_path / "sdk"
    package = source / "deeporca"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "embedded.py").write_text(SDK, encoding="utf-8")
    monkeypatch.setenv("AGENTBRIDGE_DEEPORCA_SOURCE", str(source))
    monkeypatch.delenv("AGENTBRIDGE_DEEPORCA_TEMPLATE_DIR", raising=False)
    workspace = tmp_path / "project"
    workspace.mkdir()
    local = LocalProjectStore(tmp_path / "local.db")
    project = local.add(str(workspace), "Project")
    aid = str(uuid4())
    agent = {"id": aid, "runtime": "deeporca", "local_project_id": project.id,
             "runtime_config": {}, "enabled": True}
    store = DeepOrcaStore(tmp_path / "bindings.db")
    sup = SessionSupervisor({aid: agent}, local_store=local, deeporca_store=store)
    sup.set_enrollment("http://localhost:8077", "test-machine")
    capability = probe_family("deeporca", runner=lambda *_: ProbeResult(
        0, '{"installed":true,"api":1}'))
    monkeypatch.setattr("connector.supervisor.probe_family", lambda *_, **__: capability)
    yield sup, aid, workspace, store, agent
    if not sup._stopped:
        # Every test awaits aclose, including on failure, before store teardown.
        sup.shutdown()
    local.close()


async def supervisor_until(predicate):
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(.01)
    raise AssertionError("timed out waiting for runtime state")


def events(sup):
    output = [f for f in sup.pending if f.get("type") == "output"]
    cursors = {}
    for frame in output:
        assert frame["kind"] == "event" and frame["data"].endswith("\n")
        key = frame["session_id"], frame["pty_instance_id"]
        assert frame["seq"] > cursors.get(key, 0)
        cursors[key] = frame["seq"]
    return [json.loads(f["data"]) for f in output]


def input_frame(aid, sid, text="hello", input_id=None):
    return {"type": "input", "agent_id": aid, "session_id": sid,
            "client_input_id": input_id or str(uuid4()), "data": text}


@pytest.mark.asyncio
async def test_real_worker_delivery_jsonl_and_duplicate_receipts(runtime):
    sup, aid, workspace, store, _ = runtime
    sid = str(uuid4())
    try:
        await sup.open_pty(aid, sid, surface="structured")
        assert (aid, sid) in sup.ptys, list(sup.pending)
        assert store.public_status(aid)["runtime_status"]["state"] == "ready"
        frame = input_frame(aid, sid)
        await sup.handle_control(frame)
        await supervisor_until(lambda: store.input_receipt(aid, sid, frame["client_input_id"]).get("result") == "completed")
        records = events(sup)
        assert [e["ev"] for e in records] == ["session.config", "user.echo", "turn.start", "message.delta", "turn.end"]
        assert records[3]["text"] == "Hello 世界"
        before = len(records)
        await sup.handle_control(frame)
        await asyncio.sleep(.05)
        assert len(events(sup)) == before
        assert (workspace / "execution-count").read_text() == "1"
        assert any(f.get("type") == "input_ack" and f.get("duplicate") for f in sup.pending)
    finally:
        await sup.aclose()


@pytest.mark.asyncio
async def test_worker_shared_busy_stop_and_closed_session_settlement(runtime):
    sup, aid, workspace, store, _ = runtime
    one, two = str(uuid4()), str(uuid4())
    try:
        await sup.open_pty(aid, one, surface="structured")
        await sup.open_pty(aid, two, surface="structured")
        assert sup.ptys[aid, one].worker is sup.ptys[aid, two].worker
        frame = input_frame(aid, one, "wait")
        await sup.handle_control(frame)
        await supervisor_until(lambda: any(e["ev"] == "message.delta" for e in events(sup)))
        rejected = input_frame(aid, two)
        await sup.handle_control(rejected)
        assert store.input_receipt(aid, two, rejected["client_input_id"]) is None
        assert any(f.get("type") == "input_ack" and f.get("reason") == "runtime_busy" for f in sup.pending)
        # Stop in a different conversation must not cancel the owner's turn.
        await sup.handle_control({"type": "interrupt", "agent_id": aid, "session_id": two})
        assert store.input_receipt(aid, one, frame["client_input_id"])["state"] == "running"
        await sup.handle_control({"type": "close", "agent_id": aid, "session_id": one})
        assert store.input_receipt(aid, one, frame["client_input_id"])["result"] == "cancelled"
        assert sup.ptys[aid, two].is_alive()
        await sup.handle_control(rejected)
        await supervisor_until(lambda: store.input_receipt(aid, two, rejected["client_input_id"]).get("result") == "completed")
        assert (workspace / "execution-count").read_text() == "2"
    finally:
        await sup.aclose()


@pytest.mark.asyncio
async def test_uncertain_admission_refused_on_reconnect_without_resubmit(runtime):
    sup, aid, workspace, store, _ = runtime
    sid, uid = str(uuid4()), str(uuid4())
    try:
        sup._library_store()  # initial recovery precedes newly admitted turns
        store.admit_input(aid, sid, uid, "old input")
        store.recover_interrupted()
        await sup.open_pty(aid, sid, surface="structured")
        assert any(e.get("code") == "execution_uncertain" for e in events(sup))
        await sup.handle_control(input_frame(aid, sid, input_id=uid))
        assert any(f.get("reason") == "execution_uncertain" for f in sup.pending)
        assert not (workspace / "execution-count").exists()
    finally:
        await sup.aclose()


@pytest.mark.asyncio
async def test_retiring_agent_preserves_profile_and_rejects_new_turns(runtime):
    sup, aid, workspace, store, _ = runtime
    sid = str(uuid4())
    try:
        await sup.open_pty(aid, sid, surface="structured")
        binding = store.get_binding(aid)
        marker = Path(binding["home"]) / "agents" / binding["profile_name"] / ".state.json"
        assert marker.is_file()
        await sup.handle_control({"type": "agents", "agents": []})
        assert aid not in sup._deeporca_workers
        assert store.get_binding(aid)["retired"] == 1
        assert marker.is_file()
    finally:
        await sup.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", ["frames", "bytes"])
async def test_backlog_rejects_without_receipt_and_same_id_retries_after_ack(runtime, tmp_path, bound):
    from connector.spool import DiskSpool
    sup, aid, workspace, store, _ = runtime
    sup._spool = DiskSpool(str(tmp_path / "pressure.sqlite3"))
    sup._deeporca_backlog_frames = 5
    sup._deeporca_backlog_bytes = 6000
    sid = str(uuid4())
    try:
        await sup.open_pty(aid, sid, surface="structured")
        template = {"type": "output", "agent_id": aid, "session_id": sid,
                    "pty_instance_id": sup.pty_instances[(aid, sid)], "data": ""}
        if bound == "frames":
            for _ in range(4):
                sup.emit(template)
        else:
            used = sup._spool.pending_usage()[1]
            overhead = len(json.dumps({**template, "seq": 2}, sort_keys=True,
                                      separators=(",", ":")).encode("utf-8"))
            sup.emit({**template, "data": "x" * (6000 - used - overhead)})
        frame = input_frame(aid, sid)
        await sup.handle_control(frame)
        ack = [f for f in sup.pending if f.get("type") == "input_ack"][-1]
        assert ack["status"] == "rejected" and ack["reason"] == "output_unavailable"
        assert store.input_receipt(aid, sid, frame["client_input_id"]) is None
        assert not (workspace / "execution-count").exists()
        assert sup.ptys[aid, sid].worker.can_accept_turn()
        for record in sup._spool.records():
            assert sup._spool.ack(record.session_id, record.pty_instance_id, record.seq)
        assert sup._spool.pending_usage() == (0, 0)
        await sup.handle_control(frame)
        await supervisor_until(lambda: store.input_receipt(aid, sid, frame["client_input_id"]).get("result") == "completed")
        assert (workspace / "execution-count").read_text() == "1"
    finally:
        await sup.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", ["frames", "bytes"])
async def test_pressure_during_native_turn_is_bounded_and_uncertain(runtime, tmp_path, monkeypatch, bound):
    from connector.spool import DiskSpool
    from connector.integrations.deeporca.supervisor import DEEPORCA_EMERGENCY_BYTES, DEEPORCA_EMERGENCY_FRAMES
    sup, aid, workspace, store, _ = runtime
    sup._spool = DiskSpool(str(tmp_path / "turn-pressure.sqlite3"))
    if bound == "frames":
        sup._deeporca_backlog_frames = 3  # config, echo, start; native delta fails
    else:
        emit = sup._library_emit_output
        def tighten_byte_limit(frame, *, emergency=False):
            emit(frame, emergency=emergency)
            if json.loads(frame["data"]).get("ev") == "turn.start":
                sup._deeporca_backlog_bytes = sup._spool.pending_usage()[1] + 1
        monkeypatch.setattr(sup, "_library_emit_output", tighten_byte_limit)
    sid = str(uuid4())
    try:
        await sup.open_pty(aid, sid, surface="structured")
        worker = sup.ptys[aid, sid].worker
        frame = input_frame(aid, sid, "wait")
        await sup.handle_control(frame)
        await supervisor_until(lambda: worker._active is None)
        assert (workspace / "execution-count").read_text() == "1"
        assert not worker.is_alive()
        assert store.input_receipt(aid, sid, frame["client_input_id"])["result"] == "uncertain"
        records = sup._spool.records()
        assert [r.seq for r in records] == list(range(1, len(records) + 1))
        assert len(records) <= sup._deeporca_backlog_frames + DEEPORCA_EMERGENCY_FRAMES
        assert sum(r.payload_bytes for r in records) <= sup._deeporca_backlog_bytes + DEEPORCA_EMERGENCY_BYTES
        assert sup._spool.pending_usage() == (len(records), sum(r.payload_bytes for r in records))
        result = events(sup)
        assert [e["ev"] for e in result][-2:] == ["error", "turn.end"]
        assert result[-2]["code"] == "output_unavailable"
        assert result[-1]["status"] == "uncertain"
        assert len([e for e in result if e["ev"] == "turn.end"]) == 1
    finally:
        await sup.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["usage", "admit"])
async def test_predispatch_storage_error_releases_reservation_and_sanitizes(runtime, monkeypatch, failure):
    sup, aid, workspace, store, _ = runtime
    sid = str(uuid4())
    try:
        await sup.open_pty(aid, sid, surface="structured")
        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("disk full C:/secret/token.sqlite errno=28")
        target, name = ((sup._spool, "pending_usage") if failure == "usage" else
                        (store, "admit_input"))
        frame = input_frame(aid, sid)
        with monkeypatch.context() as patch:
            patch.setattr(target, name, fail)
            await sup.handle_control(frame)
        ack = [f for f in sup.pending if f.get("type") == "input_ack"][-1]
        assert ack["status"] == "rejected"
        assert ack["reason"] == "output_unavailable"
        assert "secret" not in json.dumps(ack) and "errno" not in json.dumps(ack)
        assert sup.ptys[aid, sid].worker.can_accept_turn()
        assert not (workspace / "execution-count").exists()
        assert store.input_receipt(aid, sid, frame["client_input_id"]) is None
        await sup.handle_control(frame)
        await supervisor_until(lambda: store.input_receipt(aid, sid, frame["client_input_id"]).get("result") == "completed")
    finally:
        await sup.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
async def test_terminal_enqueue_failure_never_records_completed_receipt(runtime, monkeypatch, persistent):
    sup, aid, workspace, store, _ = runtime
    sid = str(uuid4())
    try:
        await sup.open_pty(aid, sid, surface="structured")
        worker = sup.ptys[aid, sid].worker
        enqueue = sup._spool.enqueue_output
        failed = False
        def fail_terminal(frame):
            nonlocal failed
            event = json.loads(frame["data"])
            if (event.get("ev") == "turn.end" and event.get("status") == "completed") or (failed and persistent):
                failed = True
                raise sqlite3.OperationalError("disk full C:/private/state.sqlite errno=28")
            return enqueue(frame)
        monkeypatch.setattr(sup._spool, "enqueue_output", fail_terminal)
        frame = input_frame(aid, sid)
        await sup.handle_control(frame)
        await supervisor_until(lambda: worker._active is None)
        assert failed and (workspace / "execution-count").read_text() == "1"
        assert not worker.is_alive()
        assert store.input_receipt(aid, sid, frame["client_input_id"])["result"] == "uncertain"
        result = events(sup)
        assert not any(e.get("status") == "completed" for e in result)
        assert "private" not in json.dumps(result) and "errno" not in json.dumps(result)
        if not persistent:
            assert result[-1]["status"] == "uncertain"
    finally:
        await sup.aclose()


def test_utf8_bound_emergency_reserve_and_unchanged_cli_policy():
    from connector.integrations.deeporca.events import IntegrationError
    from connector.integrations.deeporca.supervisor import DEEPORCA_EMERGENCY_FRAMES
    sup = SessionSupervisor({})
    sup._deeporca_backlog_bytes = 1000
    sup._deeporca_backlog_frames = 1
    try:
        frame = {"type": "output", "session_id": "s", "pty_instance_id": "p", "data": "\U0001f30a" * 300}
        with pytest.raises(IntegrationError, match="output_unavailable"):
            sup._library_emit_output(frame)
        assert sup._spool.pending_usage() == (0, 0)
        frame["data"] = "small"
        sup._library_emit_output(frame)
        for _ in range(DEEPORCA_EMERGENCY_FRAMES):
            sup._library_emit_output(frame, emergency=True)
        with pytest.raises(IntegrationError, match="output_unavailable"):
            sup._library_emit_output(frame, emergency=True)
        assert sup._spool.pending_usage()[0] == 1 + DEEPORCA_EMERGENCY_FRAMES
        # The ordinary CLI enqueue path retains its existing unlimited policy.
        for _ in range(20):
            sup.emit(frame)
        assert sup._spool.pending_usage()[0] == 21 + DEEPORCA_EMERGENCY_FRAMES
    finally:
        sup.shutdown()


@pytest.mark.parametrize("disk", [False, True])
def test_spool_usage_tracks_enqueue_ack_fence_and_disk_reopen(tmp_path, disk):
    from connector.spool import DiskSpool, InMemorySpool
    path = str(tmp_path / "usage.sqlite3")
    spool = DiskSpool(path) if disk else InMemorySpool()
    def check():
        status = spool.status()
        assert spool.pending_usage() == (status["pending_frames"], status["pending_bytes"])
    try:
        check()
        for sid in ("a", "b", "a"):
            spool.enqueue_output({"type": "output", "session_id": sid,
                                  "pty_instance_id": "p", "data": "\U0001f30a\u6d77"})
            check()
        assert not spool.ack("a", "p", 2)
        check()
        assert spool.ack("a", "p", 1)
        check()
        if disk:
            spool.close()
            spool = DiskSpool(path)
            check()
        assert spool.fence("a", "p") == 1
        check()
        assert spool.fence("unknown", "p") == 0
        check()
        assert spool.ack("b", "p", 1)
        assert spool.pending_usage() == (0, 0)
    finally:
        spool.close()


def test_disk_enqueue_rollback_preserves_usage_and_sequence(tmp_path):
    from connector.spool import DiskSpool
    spool = DiskSpool(str(tmp_path / "rollback.sqlite3"))
    frame = {"type": "output", "session_id": "s", "pty_instance_id": "p", "data": "hello"}
    try:
        spool._conn.execute("CREATE TRIGGER reject_output BEFORE INSERT ON outbox "
                            "BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with pytest.raises(sqlite3.IntegrityError):
            spool.enqueue_output(frame)
        assert spool.pending_usage() == (0, 0)
        spool._conn.execute("DROP TRIGGER reject_output")
        assert spool.enqueue_output(frame) == 1
        record = spool.records()[0]
        assert spool.pending_usage() == (1, record.payload_bytes)
    finally:
        spool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_url,new_devbox", [
    ("https://new.invalid", "old-devbox"),
    ("https://old.invalid", "new-devbox"),
])
async def test_restart_rejects_shared_native_spool_before_sender(tmp_path, monkeypatch, new_url, new_devbox):
    from connector.client import Connector, PROTOCOL_VERSION
    from connector.integrations.deeporca.store import BindingError, enrollment_namespace
    from connector.spool import DiskSpool

    local = LocalProjectStore(tmp_path / "local.db")
    path = str(tmp_path / "shared-spool.db")
    old = SessionSupervisor(local_store=local, spool=DiskSpool(path))
    old.set_enrollment("https://old.invalid", "old-devbox")
    ledger = old._library_store()
    ledger.admit_input("a", "s", "i", "private request")
    old._library_emit_output({"type": "output", "session_id": "s", "pty_instance_id": "p",
                              "kind": "event", "data": "private native output"})
    pending = old._spool.pending_records()
    owner = list(ledger._conn.execute("SELECT * FROM spool_owners"))[0]["namespace"]
    await old.aclose()

    # No remote calls: fake only /me, and fail if sender/WS gets started.
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {"protocol_version": PROTOCOL_VERSION, "devbox_id": new_devbox,
                    "name": "New", "agents": []}
    class HTTP:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *args, **kwargs): return Response()
    monkeypatch.setattr("connector.client.httpx.AsyncClient", HTTP)
    def forbidden(*args, **kwargs):
        pytest.fail("new enrollment must not start sender or WebSocket")
    monkeypatch.setattr("connector.client.websockets.connect", forbidden)
    fresh = Connector(new_url, "rotated-token", spool=DiskSpool(path), local_store=local)
    monkeypatch.setattr(fresh.supervisor, "drain_to", forbidden)
    try:
        with pytest.raises(BindingError, match="^enrollment_identity_conflict$"):
            await fresh.run()
        assert fresh.supervisor._spool.pending_records() == pending
        assert fresh.supervisor._enrollment_namespace == "local"
        assert fresh.supervisor._deeporca_store is None
        check = DeepOrcaStore(tmp_path / "deeporca.sqlite3", namespace=owner)
        try:
            assert check.input_receipt("a", "s", "i")["state"] == "running"
            assert list(check._conn.execute("SELECT namespace FROM spool_owners"))[0][0] == owner
        finally:
            check.close()
        # Original enrollment/token rotation remains recoverable; recovery was
        # not run under the attempted new namespace. ACK, not send, permits rebind.
        fresh.supervisor.set_enrollment("https://OLD.invalid:443/", "old-devbox")
        assert owner == enrollment_namespace("https://old.invalid", "old-devbox")
        assert fresh.supervisor._library_store().input_receipt("a", "s", "i")["result"] == "uncertain"
        assert fresh.supervisor._spool.ack("s", "p", 1)
        fresh.supervisor.set_enrollment(new_url, new_devbox)
        assert fresh.supervisor._library_store().input_receipt("a", "s", "i") is None
    finally:
        await fresh.supervisor.aclose()
        local.close()


@pytest.mark.asyncio
async def test_native_controls_and_active_reconciliation_prevent_in_process_rebind(tmp_path):
    from connector.integrations.deeporca.store import BindingError
    store = DeepOrcaStore(tmp_path / "deeporca.sqlite3")
    sup = SessionSupervisor(deeporca_store=store)
    try:
        sup.set_enrollment("http://old.invalid", "devbox")
        sup._library_store()  # First native work pins, even before a binding exists.
        sup.emit({"type": "agent.runtime_status", "agent_id": "old-private-agent"})
        with pytest.raises(BindingError, match="^enrollment_identity_conflict$"):
            sup.set_enrollment("http://new.invalid", "devbox")
        assert sup.pending[0]["agent_id"] == "old-private-agent"
        control_id = sup._controls[0][0]
        sup._inflight_ids[control_id] = 0  # Old transport sent this control.
        await sup.handle_control({"type": "ipc_delivery_ack", "delivery_id": control_id})
        lock = asyncio.Lock()
        sup._deeporca_locks["a"] = lock
        await lock.acquire()
        with pytest.raises(BindingError, match="^enrollment_identity_conflict$"):
            sup.set_enrollment("http://new.invalid", "devbox")
        lock.release()
        sup.set_enrollment("http://new.invalid", "devbox")
        assert sup.pending == []
    finally:
        await sup.aclose()


@pytest.mark.asyncio
async def test_cli_only_enrollment_keeps_generic_pending_without_creating_ledger(tmp_path):
    from connector.spool import DiskSpool
    local = LocalProjectStore(tmp_path / "local.db")
    sup = SessionSupervisor(local_store=local, spool=DiskSpool(str(tmp_path / "spool.db")))
    try:
        sup.set_enrollment("http://old.invalid", "one")
        sup.emit({"type": "output", "session_id": "s", "pty_instance_id": "p", "data": "CLI"})
        before = sup.pending
        sup.set_enrollment("http://new.invalid", "two")
        assert sup.pending == before
        assert sup._deeporca_store is None
        assert not (tmp_path / "deeporca.sqlite3").exists()
    finally:
        await sup.aclose()
        local.close()


@pytest.mark.asyncio
async def test_first_native_reconciliation_pins_before_status_or_worker(tmp_path, monkeypatch):
    local = LocalProjectStore(tmp_path / "local.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = local.add(str(workspace), "Project")
    agent = {"id": "a", "runtime": "deeporca", "local_project_id": project.id,
             "runtime_config": {}}
    sup = SessionSupervisor({"a": agent}, local_store=local)
    sup.set_enrollment("http://old.invalid", "devbox")
    original_emit = sup.emit
    checked = []
    def emit(frame):
        assert sup._deeporca_store._conn.execute("SELECT namespace FROM spool_owners").fetchone()[0] == sup._enrollment_namespace
        checked.append(frame)
        original_emit(frame)
    def worker(binding):
        assert checked  # provisioning status is already protected by the pin
        raise ValueError("offline")
    monkeypatch.setattr(sup, "emit", emit)
    monkeypatch.setattr(sup, "_new_library_worker", worker)
    try:
        with pytest.raises(ValueError, match="startup_failed"):
            await sup._ensure_library_worker("a")
        assert checked
    finally:
        await sup.aclose()
        local.close()


# ---------------------------------------------------------------------------
# Output coalescing and cancellation
# ---------------------------------------------------------------------------

"""Bounded native delta batching; no provider/network or real-time sleeps."""
import asyncio

import pytest

from connector.integrations.deeporca import worker as worker_module
from connector.integrations.deeporca.events import IntegrationError, json_bytes
from connector.integrations.deeporca.worker import _NativeEventBatcher, _COALESCE_BYTES, _COALESCE_DELAY


def delta(sequence, content="x", kind="text", **identity):
    return dict(type=kind, turn_id="turn", sequence=sequence, content=content, **identity)


class Clock:
    def __init__(self):
        self.windows = asyncio.Queue()

    async def sleep(self, delay):
        gate = asyncio.get_running_loop().create_future()
        self.windows.put_nowait((delay, gate))
        await gate

    async def window(self):
        delay, gate = await asyncio.wait_for(self.windows.get(), 1)
        assert delay == _COALESCE_DELAY
        return gate


class Sink:
    def __init__(self):
        self.events = []
        self.received = asyncio.Queue()

    async def __call__(self, event):
        self.events.append(event)
        self.received.put_nowait(event)


def test_latency_deadline_is_not_reset_by_more_deltas():
    async def case():
        sink, clock = Sink(), Clock()
        batch = _NativeEventBatcher(sink, sleep=clock.sleep)
        await batch.add(delta(1, "one"))
        gate = await clock.window()
        await batch.add(delta(2, "two"))
        await batch.add(delta(3, "three"))
        assert sink.events == [] and clock.windows.empty()
        gate.set_result(None)
        event = await asyncio.wait_for(sink.received.get(), 1)
        assert event == delta(3, "onetwothree")
        await batch.close()
        assert batch._timer.done()
    asyncio.run(case())


@pytest.mark.parametrize("boundary", ["tool_call", "tool_result", "error", "status", "done"])
def test_boundaries_flush_first_and_are_never_coalesced(boundary):
    async def case():
        sink, clock = Sink(), Clock()
        batch = _NativeEventBatcher(sink, sleep=clock.sleep)
        events = [delta(1, "a"), delta(2, "b"),
                  delta(3, "c", boundary, tool_call_id="call/+=1"),
                  delta(4, "d", boundary, tool_call_id="call/+=1"), delta(5, "e")]
        for event in events:
            await batch.add(event)
        assert sink.events == [delta(2, "ab"), events[2], events[3]]
        await batch.close()
        assert sink.events == [delta(2, "ab"), *events[2:]]
        assert [e["sequence"] for e in sink.events] == [2, 3, 4, 5]
    asyncio.run(case())


def test_identity_type_and_extension_fields_are_boundaries():
    async def case():
        sink, clock = Sink(), Clock()
        batch = _NativeEventBatcher(sink, sleep=clock.sleep)
        events = [delta(1, message_id="a"), delta(2, message_id="b"),
                  delta(3, message_id="b", parent_id="p"),
                  delta(4, kind="thinking", message_id="b", parent_id="p"),
                  delta(5, kind="thinking.delta", message_id="b", parent_id="p"),
                  delta(6, status="extension"), delta(7, status="extension"),
                  dict(delta(8), turn_id="other")]
        for event in events:
            await batch.add(event)
        await batch.close()
        assert sink.events == events
    asyncio.run(case())


@pytest.mark.parametrize("kind,field", [("text", "content"), ("thinking", "content"),
                                        ("text.delta", "text"), ("thinking.delta", "text")])
def test_unicode_serialized_budget_and_original_turn_accounting(kind, field):
    async def case():
        sink, clock = Sink(), Clock()
        batch = _NativeEventBatcher(sink, sleep=clock.sleep)
        # UTF-8 multibyte and JSON escapes must both count, without splitting.
        content = "\U0001f40b\u6d77\n\"" * 650
        originals = []
        for seq in range(1, 8):
            event = delta(seq, content, kind)
            if field == "text":
                event["text"] = event.pop("content")
            originals.append(event)
            await batch.add(event)
        await batch.close()
        assert "".join(e[field] for e in sink.events) == content * 7
        assert 1 < len(sink.events) < 7
        assert all(len(json_bytes(e)) <= _COALESCE_BYTES for e in sink.events)
        assert batch._bytes == sum(len(json_bytes(e)) for e in originals)
        assert batch._bytes > sum(len(json_bytes(e)) for e in sink.events)
    asyncio.run(case())


def test_large_native_event_passes_through_without_duplicate_identity():
    async def case():
        sink = Sink()
        batch = _NativeEventBatcher(sink)
        event = delta(1, "x" * (_COALESCE_BYTES + 1))
        await batch.add(event)
        assert sink.events == [event]
        await batch.close()
    asyncio.run(case())


@pytest.mark.parametrize("bad,code", [(delta(2, 42), "invalid_protocol"),
    (delta(1), "invalid_protocol"), (delta(2, kind="approval_request"), "approval_contract_violation"),
    (delta(2, kind="permission.request"), "approval_contract_violation"),
    (delta(2, "\ud800"), "invalid_protocol")])
def test_malformed_or_approval_cannot_be_hidden_and_valid_prefix_flushes(bad, code):
    async def case():
        sink = Sink()
        batch = _NativeEventBatcher(sink)
        await batch.add(delta(1, "prefix"))
        with pytest.raises(IntegrationError, match=code):
            await batch.add(bad)
        assert sink.events == [delta(1, "prefix")]
        assert batch.failed.done()
        with pytest.raises(IntegrationError, match=code):
            await batch.close()
        assert batch._timer.done()
    asyncio.run(case())


def test_total_budget_cannot_be_evaded_by_batching(monkeypatch):
    monkeypatch.setattr(worker_module, "MAX_TURN_BYTES", len(json_bytes(delta(1))) * 2)
    async def case():
        sink = Sink()
        batch = _NativeEventBatcher(sink)
        await batch.add(delta(1))
        await batch.add(delta(2))
        with pytest.raises(IntegrationError, match="output_limit"):
            await batch.add(delta(3))
        assert sink.events == [delta(2, "xx")]
        with pytest.raises(IntegrationError, match="output_limit"):
            await batch.close()
    asyncio.run(case())


@pytest.mark.parametrize("where", ["timer", "boundary", "close"])
def test_send_failure_is_sticky_supervised_and_never_retried(where):
    async def case():
        calls, clock = [], Clock()
        error = OSError("broken pipe")
        async def fail(event):
            calls.append(event)
            raise error
        batch = _NativeEventBatcher(fail, sleep=clock.sleep)
        await batch.add(delta(1))
        if where == "timer":
            gate = await clock.window()
            gate.set_result(None)
            assert await asyncio.wait_for(asyncio.shield(batch.failed), 1) is error
        elif where == "boundary":
            with pytest.raises(OSError):
                await batch.add(delta(2, kind="done"))
        with pytest.raises(OSError):
            await batch.close()
        assert calls == [delta(1)]
        assert batch._timer.done()
        with pytest.raises(OSError):
            await batch.add(delta(3))
    asyncio.run(case())


def test_cancel_final_flush_and_close_join_timer_without_late_output():
    async def case():
        sink, clock = Sink(), Clock()
        batch = _NativeEventBatcher(sink, sleep=clock.sleep)
        entered = asyncio.Event()
        async def owner():
            try:
                await batch.add(delta(1, "tail"))
                entered.set()
                await asyncio.Event().wait()
            finally:
                await batch.close()
        task = asyncio.create_task(owner())
        await entered.wait()
        gate = await clock.window()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sink.events == [delta(1, "tail")]
        assert gate.cancelled() and batch._timer.done()
        await batch.close()
        with pytest.raises(IntegrationError, match="worker_closed"):
            await batch.add(delta(2))
        await asyncio.sleep(0)
        assert len(sink.events) == 1
    asyncio.run(case())


def test_close_waits_for_inflight_timer_send_before_returning():
    async def case():
        clock = Clock()
        entered, release = asyncio.Event(), asyncio.Event()
        emitted = []
        async def emit(event):
            entered.set()
            await release.wait()
            emitted.append(event)
        batch = _NativeEventBatcher(emit, sleep=clock.sleep)
        await batch.add(delta(1))
        (await clock.window()).set_result(None)
        await entered.wait()
        closer = asyncio.create_task(batch.close())
        await asyncio.sleep(0)
        assert not closer.done()
        release.set()
        await closer
        assert emitted == [delta(1)] and batch._timer.done()
    asyncio.run(case())


@pytest.mark.parametrize("where", ["lock", "send"])
def test_cancelled_sdk_callback_is_supervised_including_lock_wait(where):
    async def case():
        clock = Clock()
        entered, release = asyncio.Event(), asyncio.Event()
        emitted = []

        async def emit(event):
            entered.set()
            await release.wait()
            emitted.append(event)

        batch = _NativeEventBatcher(emit, sleep=clock.sleep)
        if where == "lock":
            await batch.add(delta(1))
            (await clock.window()).set_result(None)
            await asyncio.wait_for(entered.wait(), 1)
            callback = asyncio.create_task(batch.add(delta(2)))
            await asyncio.sleep(0)  # Callback is waiting for the timer's lock.
        else:
            callback = asyncio.create_task(batch.add(delta(1, kind="status")))
            await asyncio.wait_for(entered.wait(), 1)
        assert batch._lock.locked() and not callback.done()
        callback.cancel()
        with pytest.raises(asyncio.CancelledError):
            await callback
        # An SDK can swallow that cancellation; the owner must still fail.
        assert batch.failed.done()
        assert batch.failed.result().code == "output_unavailable"
        release.set()
        with pytest.raises(IntegrationError, match="output_unavailable"):
            await asyncio.wait_for(batch.close(), 1)
        assert emitted == ([delta(1)] if where == "lock" else [])
        assert batch._timer.done()
    asyncio.run(case())


def test_timer_callback_cancellation_is_supervised_not_false_completion():
    async def case():
        clock = Clock()
        async def emit(event):
            raise asyncio.CancelledError()
        batch = _NativeEventBatcher(emit, sleep=clock.sleep)
        await batch.add(delta(1))
        (await clock.window()).set_result(None)
        error = await asyncio.wait_for(asyncio.shield(batch.failed), 1)
        assert error.code == "output_unavailable"
        with pytest.raises(IntegrationError, match="output_unavailable"):
            await batch.close()
        assert batch._timer.done()
    asyncio.run(case())


@pytest.mark.parametrize("finish,status", [("return {'status':'completed', 'turn_id':'native-turn'}", "completed"),
    ("raise asyncio.CancelledError()", "uncertain"),
    ("raise RuntimeError('save failure')", "uncertain")])
def test_real_spawn_burst_final_flush_precedes_result_or_fault(tmp_path, finish, status):
    start = SDK_FIXTURE.index("        await on_event(")
    end = SDK_FIXTURE.index("    async def interrupt", start)
    sdk = SDK_FIXTURE[:start] + """        for i in range(1, 21):
            await on_event({'type':'thinking', 'turn_id':'native-turn', 'sequence':i,
                            'message_id':message_id, 'content':'\u6d77'})
        """ + finish + "\n" + SDK_FIXTURE[end:]
    binding = make_binding(tmp_path, sdk=sdk)
    async def case():
        worker = worker_module.DeepOrcaWorker(binding)
        events, settlements = [], []
        try:
            session = await make_session(worker, events, settlements)
            await session.submit_input("input-1", "hello")
            await worker_until(lambda: bool(settlements))
            assert settlements == [status]
            deltas = [e for e in events if e["ev"] == "thinking.delta"]
            assert len(deltas) == 1
            assert deltas[0]["text"] == "\u6d77" * 20
            assert deltas[0]["turn_seq"] == 20
            assert events.index(deltas[0]) < next(i for i, e in enumerate(events) if e["ev"] == "turn.end")
        finally:
            await worker.close()
    asyncio.run(case())


def test_real_spawn_durable_output_failure_is_uncertain(tmp_path):
    binding = make_binding(tmp_path)
    async def case():
        worker = worker_module.DeepOrcaWorker(binding)
        await worker.start()
        settled = []
        reservation = worker.reserve("chat-1", "message-1")
        async def begin():
            pass
        async def fail(event):
            raise OSError("durable callback unavailable")
        async def done(status, turn_id, code):
            settled.append((status, code))
        try:
            worker.dispatch(reservation, "hello", None, on_begin=begin,
                            on_event=fail, on_settled=done)
            await asyncio.wait_for(reservation.done.wait(), 5)
            assert settled == [("uncertain", "worker_lost")]
            assert not worker.is_alive()
        finally:
            await worker.close()
    asyncio.run(case())


# ---------------------------------------------------------------------------
# Native lifecycle merge compatibility
# ---------------------------------------------------------------------------

"""Offline integration of SDK ownership with native CLI lifecycle generations."""
import asyncio
from uuid import uuid4

import pytest

from connector import runtimes
from connector.integrations.deeporca.store import BindingError
from connector.supervisor import SessionSupervisor


def frames(sup, kind):
    return [frame for frame in sup.pending if frame.get("type") == kind]


def test_library_capabilities_preserve_renderer_and_generic_lifecycle():
    features = runtimes.get("deeporca").capabilities(installed=True)["features"]
    assert features["backend"] == "python-library"
    assert features["renderer"] == "deeporca-chat-v1"
    assert features["interactive_approval"] is False
    assert features["session_lifecycle"] == 1
    assert runtimes.get("deeporca").context_control is None


@pytest.mark.asyncio
async def test_library_tokens_fence_controls_without_cli_writer_leases(
        runtime, tmp_path, monkeypatch):
    sup, aid, workspace, store, _ = runtime
    sid, launch = str(uuid4()), "sdk-launch-1"
    sup._native_lock_root = str(tmp_path / "native-writers")

    def no_cli_lease(*args, **kwargs):
        raise AssertionError("SDK ownership must not acquire a generic CLI lease")

    monkeypatch.setattr("connector.supervisor.NativeWriterLease", no_cli_lease)
    try:
        await sup.open_pty(aid, sid, surface="structured", launch_id=launch)
        assert frames(sup, "ready")[-1]["launch_id"] == launch
        assert sup.sessions_frame()["sessions"][0]["launch_id"] == launch
        assert not (tmp_path / "native-writers").exists()

        stale = {**input_frame(aid, sid), "launch_id": "old-launch"}
        await sup.handle_control(stale)
        assert store.input_receipt(aid, sid, stale["client_input_id"]) is None
        turn = {**input_frame(aid, sid, "wait"), "launch_id": launch}
        await sup.handle_control(turn)
        assert frames(sup, "input_ack")[-1]["launch_id"] == launch
        assert frames(sup, "input_ack")[-1]["status"] == "delivered"
        await sup.handle_control(turn)
        assert frames(sup, "input_ack")[-1]["launch_id"] == launch
        assert frames(sup, "input_ack")[-1]["duplicate"] is True
        await sup.handle_control({**input_frame(aid, sid), "data": None,
                                  "launch_id": launch})
        assert frames(sup, "input_ack")[-1]["launch_id"] == launch
        assert frames(sup, "input_ack")[-1]["status"] == "rejected"
        await supervisor_until(lambda: any(e["ev"] == "message.delta" for e in events(sup)))
        for kind in ("interrupt", "close", "terminate"):
            await sup.handle_control({"type": kind, "agent_id": aid,
                                      "session_id": sid, "launch_id": "old-launch"})
        assert sup.ptys[aid, sid].is_alive()
        assert store.input_receipt(aid, sid, turn["client_input_id"])["state"] == "running"
        await sup.handle_control({"type": "interrupt", "agent_id": aid,
                                  "session_id": sid, "launch_id": launch})
        await supervisor_until(lambda: store.input_receipt(
            aid, sid, turn["client_input_id"]).get("result") == "cancelled")

        # An attach may refresh the launch token without replacing SDK ownership.
        session = sup.ptys[aid, sid]
        await sup.open_pty(aid, sid, surface="structured", launch_id="sdk-launch-2")
        assert sup.ptys[aid, sid] is session
        await sup.handle_control({"type": "close", "agent_id": aid,
                                  "session_id": sid, "launch_id": "sdk-launch-2"})
        assert frames(sup, "exit")[-1]["launch_id"] == "sdk-launch-2"
        assert (aid, sid) not in sup.pty_launch_ids
        assert (workspace / "execution-count").read_text() == "1"
    finally:
        await sup.aclose()


@pytest.mark.asyncio
async def test_library_admission_exception_keeps_originating_generation(runtime, monkeypatch):
    sup, aid, _, _, _ = runtime
    sid, launch = str(uuid4()), "ack-generation"

    async def fail(frame, session):
        # Simulate a generation change while admission was awaited. Never stamp
        # the reply using the replacement generation's mutable map entry.
        sup.pty_launch_ids[aid, sid] = "replacement"
        raise RuntimeError("private admission failure")

    monkeypatch.setattr(sup, "_library_handle_input", fail)
    try:
        await sup.open_pty(aid, sid, surface="structured", launch_id=launch)
        await sup.handle_control({**input_frame(aid, sid), "launch_id": launch})
        ack = frames(sup, "input_ack")[-1]
        assert ack["status"] == "rejected"
        assert ack["reason"] == "output_unavailable"
        assert ack["launch_id"] == launch
    finally:
        await sup.aclose()


@pytest.mark.asyncio
async def test_library_binding_error_keeps_launch_id(runtime, monkeypatch):
    sup, aid, _, _, _ = runtime

    async def fail(*args):
        raise BindingError("configuration_required")

    monkeypatch.setattr(sup, "_make_library_session", fail)
    try:
        await sup.open_pty(aid, str(uuid4()), surface="structured", launch_id="failed")
        error = frames(sup, "runtime.unavailable")[-1]
        assert error["code"] == "configuration_required"
        assert error["launch_id"] == "failed"
        assert not sup.ptys
    finally:
        await sup.aclose()


@pytest.mark.asyncio
async def test_library_does_not_pretend_to_support_cli_native_resume(runtime):
    sup, aid, _, _, _ = runtime
    try:
        await sup.open_pty(aid, str(uuid4()), surface="structured",
                           launch_id="resume", resume=True)
        assert not sup.ptys
        assert frames(sup, "runtime.unavailable")[-1]["code"] == "context.not_found"
        assert frames(sup, "runtime.unavailable")[-1]["launch_id"] == "resume"
        assert not sup._deeporca_workers
    finally:
        await sup.aclose()


@pytest.mark.asyncio
async def test_inventory_removal_keeps_native_cli_history(runtime):
    sup, aid, workspace, _, _ = runtime
    sid = str(uuid4())
    marker = sup.local_store.establish_native_context(
        aid, sid, "claude-structured", str(workspace))
    try:
        await sup.handle_control({"type": "upsert_agents", "agents": []})
        assert sup.local_store.native_context(aid, sid) == marker
    finally:
        await sup.aclose()


@pytest.mark.asyncio
async def test_aclose_drains_cli_reapers_before_runtime_storage_close(tmp_path, monkeypatch):
    sup = SessionSupervisor(native_lock_root=str(tmp_path / "native-writers"))
    order = []

    class Session:
        def kill(self):
            order.append("kill")

        async def wait_closed(self):
            await asyncio.sleep(0)
            order.append("reaped")

    async def settle_library():
        order.append("library-settled")

    monkeypatch.setattr(sup, "_close_runtime_sessions", settle_library)
    monkeypatch.setattr(sup, "_close_runtime_storage", lambda: order.append("storage-closed"))
    sup.ptys["agent", "session"] = Session()
    await sup.aclose()
    assert order == ["library-settled", "kill", "reaped", "storage-closed"]
    assert not sup._close_tasks
