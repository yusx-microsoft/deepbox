"""Native-context lifecycle regressions: real file leases, no provider CLI.

Subprocess doubles have independently controlled stream EOF and reaping, so
killing a child is deliberately not evidence that its writer can be replaced.
"""
import asyncio
import json
import os
import threading

import pytest

from connector import agent_session as A
from connector import supervisor as S
from connector.local_store import LocalProjectStore
from connector.native_writer import (
    NativeWriterBusy,
    NativeWriterLease,
    NativeWriterRecoveryRequired,
    status,
)


FAMILY = "claude-code"
RUNTIME = "claude-code-structured"
SID = "6b7ed552-9658-4838-86f4-4b6a4f50f2bc"
TIMEOUT = 3


class _Stream:
    def __init__(self):
        self.lines = asyncio.Queue()
        self.eof = False

    def feed(self, obj):
        self.lines.put_nowait(json.dumps(obj).encode() + b"\n")

    def finish(self):
        if not self.eof:
            self.eof = True
            self.lines.put_nowait(b"")

    async def readline(self):
        if self.eof and self.lines.empty():
            return b""
        return await self.lines.get()


class _Stdin:
    def __init__(self):
        self.buf = b""

    def write(self, data):
        self.buf += data

    async def drain(self):
        pass

    def close(self):
        pass


class _Proc:
    def __init__(self, *, hold_reap=False, wait_error=None):
        self.stdout = _Stream()
        self.stderr = _Stream()
        self.stdin = _Stdin()
        self.returncode = None
        self.exited = asyncio.Event()
        self.killed = asyncio.Event()
        self.wait_started = asyncio.Event()
        self.allow_reap = asyncio.Event()
        if not hold_reap:
            self.allow_reap.set()
        self.wait_error = wait_error
        self.reaped = False

    def finish(self, code=0):
        self.returncode = code
        self.stdout.finish()
        self.stderr.finish()
        self.exited.set()

    def result(self, *, error=False):
        self.stdout.feed({
            "type": "result", "subtype": "error" if error else "success",
            "is_error": error,
        })

    def kill(self):
        self.killed.set()
        self.finish(-9)

    async def wait(self):
        self.wait_started.set()
        await self.exited.wait()
        await self.allow_reap.wait()
        if self.wait_error is not None:
            raise self.wait_error
        self.reaped = True
        return self.returncode


async def _noop(_value):
    pass


async def _until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), TIMEOUT)


def _lease(root):
    return NativeWriterLease(str(root), FAMILY, SID)


def _assert_busy(root):
    lease = _lease(root)
    try:
        with pytest.raises(NativeWriterBusy):
            lease.acquire()
    finally:
        lease.release()


def _assert_available(root):
    with _lease(root):
        assert status(str(root), FAMILY, SID)["state"] == "idle"


def _assert_quarantined(root):
    lease = _lease(root)
    try:
        with pytest.raises(NativeWriterRecoveryRequired):
            lease.acquire()
    finally:
        lease.release()
    assert status(str(root), FAMILY, SID)["state"] == "active"


def _session(root, spawn, events=None, **kwargs):
    events = events if events is not None else []

    async def output(raw):
        # The actual session/supervisor boundary carries JSON strings.
        assert isinstance(raw, str)
        events.append(json.loads(raw))

    kwargs.setdefault("lazy_start", True)
    return A.StructuredAgentSession(
        ["fake-native-agent"], None, output, _noop, spawn=spawn,
        writer_lease_factory=lambda: _lease(root), **kwargs)


async def _turn(session, text="hello"):
    assert session.write_turn(text, {}) is True
    task = session._turn_task
    assert task is not None
    await asyncio.wait_for(asyncio.shield(task), TIMEOUT)


async def _close(sessions, procs=()):
    # Even a failed assertion must release all fake stream/wait gates.
    for proc in procs:
        proc.allow_reap.set()
        proc.kill()
    for session in sessions:
        session.kill()
    for session in sessions:
        await asyncio.wait_for(session.wait_closed(), TIMEOUT)
        if session._reader_tasks:
            await asyncio.wait_for(asyncio.gather(
                *session._reader_tasks, return_exceptions=True), TIMEOUT)


def test_competing_sessions_cannot_spawn_until_kill_and_reap(tmp_path):
    async def run():
        root = tmp_path / "writers"
        first_proc = _Proc(hold_reap=True)
        second_proc = _Proc()
        launches = []
        events = []

        async def spawn_first():
            launches.append("first")
            return first_proc

        async def spawn_second():
            launches.append("second")
            return second_proc

        first = _session(root, spawn_first)
        second = _session(root, spawn_second, events)
        try:
            await first.start()
            await second.start()
            await _turn(first)
            await _turn(second)
            assert launches == ["first"]
            assert any(e.get("code") == "context.in_use" for e in events)
            first.kill()
            closing = asyncio.create_task(first.wait_closed())
            await asyncio.wait_for(first_proc.wait_started.wait(), TIMEOUT)
            assert not first_proc.reaped
            assert not closing.done()
            _assert_busy(root)
            await _turn(second, "still blocked after kill")
            assert launches == ["first"]
            first_proc.allow_reap.set()
            await asyncio.wait_for(closing, TIMEOUT)
            assert first_proc.reaped
            _assert_available(root)
            await _turn(second, "now allowed")
            assert launches == ["first", "second"]
        finally:
            await _close([first, second], [first_proc, second_proc])
        _assert_available(root)

    asyncio.run(run())


def test_per_turn_session_retains_lease_between_children(tmp_path):
    async def run():
        root = tmp_path / "writers"
        procs = []
        prompts = []
        other_launches = []

        async def spawn(prompt):
            prompts.append(prompt)
            proc = _Proc()
            procs.append(proc)
            proc.result()
            proc.finish()
            return proc

        async def other_spawn():
            other_launches.append(True)
            proc = _Proc()
            procs.append(proc)
            return proc

        first = _session(root, spawn, per_turn=True)
        second = _session(root, other_spawn)
        try:
            await first.start()
            await second.start()
            await _turn(first, "one")
            assert procs[0].reaped
            assert first.is_alive()
            assert status(str(root), FAMILY, SID)["state"] == "idle"
            _assert_busy(root)
            await _turn(second)
            assert other_launches == []
            await _turn(first, "two")
            assert prompts == ["one", "two"]
            assert all(proc.reaped for proc in procs)
            _assert_busy(root)
            first.kill()
            await asyncio.wait_for(first.wait_closed(), TIMEOUT)
            await _turn(second)
            assert other_launches == [True]
        finally:
            await _close([first, second], procs)

    asyncio.run(run())


@pytest.mark.parametrize("per_turn", [False, True], ids=["persistent", "per-turn"])
@pytest.mark.parametrize("action", ["kill", "cancel"])
def test_pending_spawn_keeps_lease_until_late_child_is_reaped(
        tmp_path, action, per_turn):
    async def run():
        root = tmp_path / "writers"
        entered = asyncio.Event()
        return_child = asyncio.Event()
        proc = _Proc(hold_reap=True)
        competitor_proc = _Proc()
        competitor_launches = []

        async def spawn(*_args):
            entered.set()
            await return_child.wait()
            return proc

        async def competing_spawn():
            competitor_launches.append(True)
            return competitor_proc

        first = _session(root, spawn, per_turn=per_turn)
        competitor = _session(root, competing_spawn)
        task = None
        try:
            await first.start()
            await competitor.start()
            assert first.write_turn("pending") is True
            task = first._turn_task
            await asyncio.wait_for(entered.wait(), TIMEOUT)
            if action == "kill":
                first.kill()
            else:
                task.cancel()
            # Give the cancellation handler a chance to enter its shielded wait.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done()
            _assert_busy(root)
            await _turn(competitor)
            assert competitor_launches == []
            return_child.set()
            await asyncio.wait_for(proc.killed.wait(), TIMEOUT)
            assert not proc.reaped
            assert not task.done()
            _assert_busy(root)
            await _turn(competitor, "child returned but not reaped")
            assert competitor_launches == []
            proc.allow_reap.set()
            outcome = await asyncio.wait_for(asyncio.gather(
                task, return_exceptions=True), TIMEOUT)
            if action == "cancel":
                assert isinstance(outcome[0], asyncio.CancelledError)
            else:
                assert outcome == [None]
            assert proc.reaped
            assert proc.stdin.buf == b""
            first.kill()
            await asyncio.wait_for(first.wait_closed(), TIMEOUT)
            _assert_available(root)
            await _turn(competitor)
            assert competitor_launches == [True]
        finally:
            return_child.set()
            await _close([first, competitor], [proc, competitor_proc])
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_persistence_callback_failure_fences_session_and_rejects_writes(tmp_path):
    async def run():
        root = tmp_path / "writers"
        proc = _Proc()
        events = []
        persistence_calls = []

        async def spawn():
            return proc

        async def persist():
            _assert_busy(root)
            persistence_calls.append(True)
            raise OSError("simulated local database failure")

        session = _session(root, spawn, events, context_started=persist)
        try:
            await session.start()
            await _turn(session)
            written = proc.stdin.buf
            proc.result()
            await asyncio.wait_for(proc.killed.wait(), TIMEOUT)
            assert not session.is_alive()
            assert session.write_turn("must not write") is False
            assert proc.stdin.buf == written
            assert persistence_calls == [True]
            assert any(e.get("code") == "context_persist_failed" for e in events)
            assert any(e.get("ev") == A.EV_TURN_END and e.get("is_error")
                       and e.get("subtype") == "context_persist_failed"
                       for e in events)
            await asyncio.wait_for(session.wait_closed(), TIMEOUT)
            assert proc.reaped
            _assert_available(root)
        finally:
            await _close([session], [proc])

    asyncio.run(run())


def test_context_preparing_failure_starts_no_child_and_releases_lease(tmp_path):
    async def run():
        root = tmp_path / "writers"
        launches = []
        events = []

        def prepare():
            _assert_busy(root)
            raise ValueError("reservation could not be committed")

        async def spawn():
            launches.append(True)
            raise AssertionError("preparation failure must precede spawn")

        session = _session(root, spawn, events, context_preparing=prepare)
        try:
            await session.start()
            await _turn(session)
            assert launches == []
            assert any("reservation" in e.get("message", "") for e in events)
            _assert_available(root)
        finally:
            await _close([session])

    asyncio.run(run())


@pytest.mark.parametrize("error_type", [OSError, RuntimeError],
                         ids=["definite-no-child", "uncertain-launch"])
def test_spawn_failure_releases_or_quarantines_appropriately(tmp_path, error_type):
    async def run():
        root = tmp_path / "writers"

        async def spawn():
            _assert_busy(root)
            assert status(str(root), FAMILY, SID)["state"] == "active"
            raise error_type("synthetic spawn failure")

        session = _session(root, spawn, lazy_start=False)
        try:
            with pytest.raises(error_type, match="synthetic spawn failure"):
                await session.start()
            assert not session.is_alive()
            if error_type is OSError:
                _assert_available(root)
            else:
                _assert_quarantined(root)
        finally:
            await _close([session])
        if error_type is RuntimeError:
            _assert_quarantined(root)

    asyncio.run(run())


def test_process_wait_failure_quarantines_writer(tmp_path):
    async def run():
        root = tmp_path / "writers"
        proc = _Proc(wait_error=OSError("wait cannot confirm child exit"))

        async def spawn():
            return proc

        session = _session(root, spawn)
        try:
            await session.start()
            await _turn(session)
            session.kill()
            await asyncio.wait_for(session.wait_closed(), TIMEOUT)
            assert not proc.reaped
            _assert_quarantined(root)
        finally:
            await _close([session], [proc])

    asyncio.run(run())


def test_cancelled_spawn_with_returned_child_wait_error_is_not_spawn_failure(tmp_path):
    """An OSError from wait() must not be mistaken for a no-child spawn OSError."""
    async def run():
        root = tmp_path / "writers"
        entered = asyncio.Event()
        return_child = asyncio.Event()
        proc = _Proc(wait_error=OSError("returned child could not be reaped"))

        async def spawn():
            entered.set()
            await return_child.wait()
            return proc

        session = _session(root, spawn)
        task = None
        try:
            await session.start()
            assert session.write_turn("pending") is True
            task = session._turn_task
            await asyncio.wait_for(entered.wait(), TIMEOUT)
            task.cancel()
            await asyncio.sleep(0)
            _assert_busy(root)
            return_child.set()
            await asyncio.wait_for(asyncio.gather(
                task, return_exceptions=True), TIMEOUT)
            assert proc.killed.is_set()
            assert not proc.reaped
            session.kill()
            await asyncio.wait_for(session.wait_closed(), TIMEOUT)
            _assert_quarantined(root)
        finally:
            return_child.set()
            await _close([session], [proc])
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


class _SupervisorHarness:
    """Real supervisors/stores/sessions; only capability probes and CLI are fake."""
    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path / "writers"
        self.db_path = str(tmp_path / "state.db")
        self.stores = []
        self.supervisors = []
        self.sessions = []
        self.procs = []
        self.launches = []
        self.spawn_assertion_errors = []
        self.on_spawn = None
        self.agent = {"id": "a", "runtime": FAMILY}
        monkeypatch.setattr(S, "resolve_cmd", lambda *args, **kw: ["fake-native-agent"])
        monkeypatch.setattr(S, "probe_family", lambda *args, **kw: {})
        monkeypatch.setattr(S, "availability", lambda *args, **kw: (True, None))
        monkeypatch.setattr(A, "_resolve_spawn_argv", lambda argv: list(argv))
        monkeypatch.setattr(A.asyncio, "create_subprocess_exec", self.spawn)
        self.sup = self.new_supervisor()
        self.store = self.stores[0]

    def new_supervisor(self):
        store = LocalProjectStore(self.db_path)
        self.stores.append(store)
        sup = S.SessionSupervisor(
            {"a": dict(self.agent)}, local_store=store,
            native_lock_root=str(self.root))
        self.supervisors.append(sup)
        return sup

    def proc(self):
        proc = _Proc()
        self.procs.append(proc)
        return proc

    async def spawn(self, *argv, **kwargs):
        self.launches.append((list(argv), kwargs))
        try:
            _assert_busy(self.root)
            assert status(str(self.root), FAMILY, SID)["state"] == "active"
            if self.on_spawn is not None:
                return self.on_spawn(list(argv), kwargs)
            return self.proc()
        except AssertionError as exc:
            # The turn worker intentionally catches arbitrary spawn errors. Do
            # not let it turn a failing test invariant into a passing error turn.
            self.spawn_assertion_errors.append(exc)
            raise

    async def open(self, sup=None):
        sup = sup or self.sup
        await sup.open_pty("a", SID, surface="structured")
        session = sup.ptys[("a", SID)]
        assert isinstance(session, A.StructuredAgentSession)
        self.sessions.append(session)
        return session

    async def close(self):
        await _close(self.sessions, self.procs)
        for sup in self.supervisors:
            sup.shutdown()
        for store in self.stores:
            store.close()
        if self.spawn_assertion_errors:
            raise self.spawn_assertion_errors[0]


def _events(sup):
    return [json.loads(frame["data"]) for frame in sup.pending
            if frame.get("type") == "output" and frame.get("kind") == "event"]


def _assert_create(argv):
    assert argv[argv.index("--session-id") + 1] == SID
    assert "--resume" not in argv


def _assert_resume(argv):
    assert argv[argv.index("--resume") + 1] == SID
    assert "--session-id" not in argv


@pytest.mark.parametrize("failure", ["spawn", "failed-turn"])
def test_reservation_precedes_launch_and_failed_attempt_only_resumes(
        tmp_path, monkeypatch, failure):
    async def run():
        # No explicit cwd: store and spawn must use the inherited effective path.
        monkeypatch.chdir(tmp_path)
        h = _SupervisorHarness(tmp_path, monkeypatch)
        expected_cwd = os.path.normcase(os.path.realpath(str(tmp_path)))
        reserve = h.store.reserve_native_context
        reservations = []

        def checked_reserve(*args):
            _assert_busy(h.root)
            reservations.append(args)
            return reserve(*args)

        monkeypatch.setattr(h.store, "reserve_native_context", checked_reserve)

        def first_spawn(argv, kwargs):
            # A separate connection must see a committed reservation BEFORE spawn.
            with LocalProjectStore(h.db_path) as observer:
                marker = observer.native_context("a", SID)
            assert marker is not None and marker.state == "attempted"
            assert marker.runtime_id == RUNTIME
            assert marker.cwd is not None
            assert os.path.normcase(marker.cwd) == expected_cwd
            assert kwargs["cwd"] == expected_cwd
            _assert_create(argv)
            if failure == "spawn":
                raise OSError("definite first launch failure")
            proc = h.proc()
            proc.result(error=True)
            proc.finish(1)
            return proc

        h.on_spawn = first_spawn
        try:
            first = await h.open()
            assert h.store.native_context("a", SID) is None
            assert h.launches == []
            await _turn(first)
            assert len(h.launches) == 1
            assert len(reservations) == 1
            await _close([first], h.procs)
            marker = h.store.native_context("a", SID)
            assert marker.state == "attempted"
            assert marker.established_at == ""
            _assert_available(h.root)
            # A fresh supervisor/DB connection must not rely on in-memory history.
            h.on_spawn = None
            restarted = h.new_supervisor()
            second = await h.open(restarted)
            await _turn(second, "retry")
            assert len(h.launches) == 2
            _assert_resume(h.launches[1][0])
            assert h.launches[1][1]["cwd"] == expected_cwd
        finally:
            await h.close()

    asyncio.run(run())


def test_old_open_snapshot_refreshes_marker_under_acquired_lease(tmp_path, monkeypatch):
    async def run():
        h = _SupervisorHarness(tmp_path, monkeypatch)
        stale_sup = h.new_supervisor()
        stale_store = stale_sup.local_store
        refreshes = []
        try:
            first = await h.open()
            stale = await h.open(stale_sup)
            assert stale_store.native_context("a", SID) is None
            await _turn(first)
            _assert_create(h.launches[0][0])
            h.procs[0].result()
            await _until(lambda: h.store.native_context("a", SID).state == "established")
            first.kill()
            await asyncio.wait_for(first.wait_closed(), TIMEOUT)
            original_read = stale_store.native_context

            def checked_read(*args):
                _assert_busy(h.root)
                refreshes.append(True)
                return original_read(*args)

            monkeypatch.setattr(stale_store, "native_context", checked_read)
            await _turn(stale, "old open must resume")
            assert refreshes
            assert len(h.launches) == 2
            _assert_resume(h.launches[1][0])
        finally:
            await h.close()

    asyncio.run(run())


@pytest.mark.parametrize("state", ["attempted", "established"])
def test_agents_update_omission_preserves_context_markers(tmp_path, monkeypatch, state):
    async def run():
        h = _SupervisorHarness(tmp_path, monkeypatch)
        try:
            first = await h.open()
            await _turn(first)
            h.procs[0].result(error=state == "attempted")
            await _until(lambda: any(e.get("ev") == A.EV_TURN_END
                                     for e in _events(h.sup)))
            before = h.store.native_context("a", SID)
            assert before.state == state
            await h.sup.handle_control({"type": "agents", "agents": []})
            await asyncio.wait_for(first.wait_closed(), TIMEOUT)
            assert h.sup.agents == {}
            assert ("a", SID) not in h.sup.ptys
            assert h.store.native_context("a", SID) == before
            await h.sup.handle_control({"type": "agents", "agents": [h.agent]})
            second = await h.open()
            await _turn(second)
            assert len(h.launches) == 2
            _assert_resume(h.launches[1][0])
        finally:
            await h.close()

    asyncio.run(run())


@pytest.mark.parametrize("mismatch", ["runtime", "cwd"])
def test_binding_mismatch_is_rechecked_under_lease_before_spawn(
        tmp_path, monkeypatch, mismatch):
    async def run():
        h = _SupervisorHarness(tmp_path, monkeypatch)
        original_read = h.store.native_context
        reads = []
        try:
            session = await h.open()
            assert original_read("a", SID) is None
            different_cwd = tmp_path / "other-project"
            different_cwd.mkdir()
            cwd = str(different_cwd) if mismatch == "cwd" else session.cwd
            runtime = "copilot-cli-structured" if mismatch == "runtime" else RUNTIME
            # Simulate another writer committing a binding after the open snapshot.
            with _lease(h.root):
                marker = h.store.reserve_native_context("a", SID, runtime, cwd)

            def checked_read(*args):
                _assert_busy(h.root)
                reads.append(True)
                return original_read(*args)

            monkeypatch.setattr(h.store, "native_context", checked_read)
            await _turn(session)
            assert reads
            assert h.launches == []
            message = "runtime changed" if mismatch == "runtime" else "local project changed"
            assert any(message in e.get("message", "") for e in _events(h.sup))
            assert original_read("a", SID) == marker
            _assert_available(h.root)
        finally:
            await h.close()

    asyncio.run(run())


def test_configuration_change_during_availability_probe_refuses_spawn(
        tmp_path, monkeypatch):
    async def run():
        h = _SupervisorHarness(tmp_path, monkeypatch)
        entered = threading.Event()
        release_probe = threading.Event()

        def probe(*args, **kwargs):
            entered.set()
            if not release_probe.wait(TIMEOUT):
                raise TimeoutError("test did not release availability probe")
            return {}

        monkeypatch.setattr(S, "probe_family", probe)
        opening = asyncio.create_task(h.sup.open_pty("a", SID, surface="structured"))
        try:
            await _until(entered.is_set)
            changed = dict(h.agent, runtime_config={"model": "sonnet"})
            await h.sup.handle_control({"type": "agents", "agents": [changed]})
            release_probe.set()
            await asyncio.wait_for(opening, TIMEOUT)
            assert h.launches == []
            assert h.sup.ptys == {}
            assert h.store.native_context("a", SID) is None
            assert not any(f.get("type") == "ready" for f in h.sup.pending)
            assert any(f.get("code") == "configuration_changed" for f in h.sup.pending)
            _assert_available(h.root)
        finally:
            release_probe.set()
            await asyncio.wait_for(asyncio.gather(
                opening, return_exceptions=True), TIMEOUT)
            # Capture unexpected registrations as well, so a regression cannot leak.
            h.sessions.extend(h.sup.ptys.values())
            await h.close()

    asyncio.run(run())
