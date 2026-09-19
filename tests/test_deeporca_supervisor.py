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


async def until(predicate):
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
        await until(lambda: store.input_receipt(aid, sid, frame["client_input_id"]).get("result") == "completed")
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
        await until(lambda: any(e["ev"] == "message.delta" for e in events(sup)))
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
        await until(lambda: store.input_receipt(aid, two, rejected["client_input_id"]).get("result") == "completed")
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
        await until(lambda: store.input_receipt(aid, sid, frame["client_input_id"]).get("result") == "completed")
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
        await until(lambda: worker._active is None)
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
        await until(lambda: store.input_receipt(aid, sid, frame["client_input_id"]).get("result") == "completed")
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
        await until(lambda: worker._active is None)
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
