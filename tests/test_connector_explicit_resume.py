"""Explicit resume is a locked, non-creating lifecycle operation (no real CLI)."""
import asyncio
import json
import os

import pytest

from connector import agent_session as A, runtimes
from connector.native_writer import NativeWriterLease
from connector.transport import TransportSession
from test_native_context_lifecycle import (
    FAMILY, RUNTIME, SID, _SupervisorHarness, _assert_available, _assert_busy,
    _assert_resume, _turn, _until,
)


def _control(kind="resume", launch_id="new"):
    return {"type": kind, "agent_id": "a", "session_id": SID,
            "surface": "structured", "launch_id": launch_id}


def _frames(sup, kind):
    return [f for f in sup.pending if f.get("type") == kind]


def _reserve(h, state="attempted", runtime=RUNTIME, cwd=None):
    writer = (h.store.reserve_native_context if state == "attempted"
              else h.store.establish_native_context)
    return writer("a", SID, runtime, cwd or os.getcwd())


async def _close(h):
    for sup in h.supervisors:
        sup.shutdown()
    for sup in h.supervisors:
        await sup.wait_closed()
    await h.close()


@pytest.mark.parametrize("state", ["attempted", "established"])
def test_resume_prepares_under_lock_without_spawn_and_first_input_resumes(
        tmp_path, monkeypatch, state):
    async def run():
        h = _SupervisorHarness(tmp_path, monkeypatch)
        _reserve(h, state)
        read = h.store.native_context
        reads = []

        def locked_read(*args):
            _assert_busy(h.root)
            reads.append(args)
            return read(*args)

        monkeypatch.setattr(h.store, "native_context", locked_read)
        try:
            await h.sup.handle_control(_control(launch_id="opaque/0001"))
            ready = _frames(h.sup, "ready")[-1]
            assert ready["launch_id"] == "opaque/0001"
            assert ready["context_resume"] == "pending"
            assert reads and not h.launches
            assert read("a", SID).state == state
            session = h.sup.ptys[("a", SID)]
            h.sessions.append(session)
            _assert_busy(h.root)
            await h.sup.handle_control({"type": "list_sessions"})
            assert _frames(h.sup, "sessions")[-1]["sessions"][0]["launch_id"] == "opaque/0001"

            def spawn(argv, kwargs):
                _assert_resume(argv)
                assert "--continue" not in argv and "latest" not in argv
                proc = h.proc()
                proc.result()
                proc.finish()
                return proc

            h.on_spawn = spawn
            await _turn(session, "only the real user message")
            assert len(h.launches) == 1
            sent = [json.loads(line) for line in h.procs[0].stdin.buf.splitlines()]
            assert len(sent) == 1
            assert sent[0]["message"]["content"] == [
                {"type": "text", "text": "only the real user message"}]
            assert read("a", SID).state == "established"
        finally:
            await _close(h)
        _assert_available(h.root)
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["absent", "missing-db", "legacy", "unknown", "runtime", "cwd"])
def test_strict_resume_rejects_missing_legacy_or_mismatched_context_without_create(
        tmp_path, monkeypatch, failure):
    async def run():
        h = _SupervisorHarness(tmp_path, monkeypatch)
        expected = "context.not_found"
        if failure == "legacy":
            h.store.establish_native_context("a", SID, RUNTIME, None)
        elif failure == "unknown":
            _reserve(h, runtime="unrecognized-old-cli")
        elif failure == "runtime":
            _reserve(h, runtime="copilot-cli-structured")
            expected = "context.runtime_mismatch"
        elif failure == "cwd":
            _reserve(h, cwd=str(tmp_path))
            expected = "context.cwd_mismatch"
        elif failure == "missing-db":
            h.sup.local_store = None
        before = h.store.native_context("a", SID)
        try:
            await h.sup.handle_control(_control())
            assert not h.sup.ptys and not h.launches
            assert not _frames(h.sup, "ready")
            error = _frames(h.sup, "runtime.unavailable")[-1]
            assert error["code"] == expected
            assert error["launch_id"] == "new"
            assert str(tmp_path) not in error["message"]
            assert h.store.native_context("a", SID) == before
            _assert_available(h.root)
        finally:
            await _close(h)
    asyncio.run(run())


@pytest.mark.parametrize("runtime,surface", [("claude-code", "terminal"), ("codex-structured", "structured")])
def test_resume_requires_native_structured_adapter(tmp_path, monkeypatch, runtime, surface):
    async def run():
        h = _SupervisorHarness(tmp_path, monkeypatch)
        h.sup.agents["a"]["runtime"] = runtime
        try:
            frame = _control()
            frame["surface"] = surface
            await h.sup.handle_control(frame)
            assert not h.sup.ptys and not h.launches
            assert _frames(h.sup, "runtime.unavailable")[-1]["code"] == "context.not_found"
        finally:
            await _close(h)
    asyncio.run(run())


def test_deleted_marker_never_downgrades_resume_to_create(tmp_path, monkeypatch):
    async def run():
        h = _SupervisorHarness(tmp_path, monkeypatch)
        _reserve(h)
        try:
            await h.sup.handle_control(_control())
            session = h.sup.ptys[("a", SID)]
            h.sessions.append(session)
            h.store.forget_native_context("a", SID)
            await _turn(session)
            assert not h.launches
            assert h.store.native_context("a", SID) is None
            _assert_available(h.root)
        finally:
            await _close(h)
    asyncio.run(run())


@pytest.mark.parametrize("marker", [False, True])
def test_same_live_lazy_session_attaches_only_with_marker(tmp_path, monkeypatch, marker):
    async def run():
        h = _SupervisorHarness(tmp_path, monkeypatch)
        try:
            await h.sup.handle_control(_control("open", "old"))
            session = h.sup.ptys[("a", SID)]
            h.sessions.append(session)
            instance = h.sup.pty_instances[("a", SID)]
            if marker:
                _reserve(h)
            await h.sup.handle_control(_control())
            assert h.sup.ptys[("a", SID)] is session
            assert h.sup.pty_instances[("a", SID)] == instance
            assert not h.launches
            if marker:
                assert _frames(h.sup, "ready")[-1]["launch_id"] == "new"
                await h.sup.handle_control(_control("terminate", "old"))
                assert session.is_alive()
                await session.on_exit(0)
                assert _frames(h.sup, "exit")[-1]["launch_id"] == "new"
            else:
                assert _frames(h.sup, "runtime.unavailable")[-1]["code"] == "context.not_found"
                assert len(_frames(h.sup, "ready")) == 1
                assert h.sup.pty_launch_ids[("a", SID)] == "old"
                assert session.require_existing_context
                assert h.store.native_context("a", SID) is None
                _assert_available(h.root)
                await _turn(session)
                assert not h.launches
                assert h.store.native_context("a", SID) is None
        finally:
            await _close(h)
    asyncio.run(run())


def test_resume_busy_is_unavailable_before_ready(tmp_path, monkeypatch):
    async def run():
        h = _SupervisorHarness(tmp_path, monkeypatch)
        _reserve(h)
        try:
            with NativeWriterLease(str(h.root), FAMILY, SID):
                await h.sup.handle_control(_control())
                assert not _frames(h.sup, "ready") and not h.launches
                error = _frames(h.sup, "runtime.unavailable")[-1]
                assert error["code"] == "context.in_use" and error["launch_id"] == "new"
        finally:
            await _close(h)
    asyncio.run(run())


def test_current_configuration_rechecked_under_held_guard(tmp_path, monkeypatch):
    async def run():
        h = _SupervisorHarness(tmp_path, monkeypatch)
        _reserve(h)
        try:
            session = await h.open()
            h.sup.agents["a"] = {**h.sup.agents["a"], "model": "changed"}
            await h.sup.handle_control(_control())
            assert _frames(h.sup, "runtime.unavailable")[-1]["code"] == "configuration_changed"
            assert not h.launches
            _assert_available(h.root)
        finally:
            await _close(h)
    asyncio.run(run())


def test_new_generation_fences_pending_start_and_stale_end(tmp_path, monkeypatch):
    async def run():
        h = _SupervisorHarness(tmp_path, monkeypatch)
        _reserve(h)
        entered, release = asyncio.Event(), asyncio.Event()
        created = []
        start = A.StructuredAgentSession.start

        async def delayed_start(session):
            created.append(session)
            if len(created) == 1:
                entered.set()
                await release.wait()
            await start(session)

        monkeypatch.setattr(A.StructuredAgentSession, "start", delayed_start)
        old = new = None
        try:
            old = asyncio.create_task(h.sup.handle_control(_control("open", "old")))
            await asyncio.wait_for(entered.wait(), 3)
            new = asyncio.create_task(h.sup.handle_control(_control()))
            await asyncio.sleep(0)
            await h.sup.handle_control(_control("terminate", "old"))
            release.set()
            await asyncio.gather(old, new)
            assert [f["launch_id"] for f in _frames(h.sup, "ready")] == ["new"]
            assert not _frames(h.sup, "runtime.unavailable")
            assert created[0]._killed
            assert h.sup.ptys[("a", SID)] is created[1]
            await created[0].on_exit(9)
            assert not _frames(h.sup, "exit")
            await h.sup.handle_control(_control("terminate", "old"))
            assert created[1].is_alive()
            await h.sup.handle_control(_control("terminate", "new"))
            await h.sup.wait_closed()
            assert not h.sup.ptys and created[1]._killed
            _assert_available(h.root)
        finally:
            release.set()
            await asyncio.gather(*(t for t in (old, new) if t), return_exceptions=True)
            await _close(h)
    asyncio.run(run())


def test_transport_preserves_resume_and_launch_ids_in_both_directions():
    class Channel:
        def __init__(self, incoming=()):
            self.sent = []
            self.incoming = iter(incoming)

        async def send(self, frame):
            self.sent.append(frame)

        async def recv(self):
            return next(self.incoming, None)

    class WebSocket:
        def __init__(self, incoming=()):
            self.incoming = incoming
            self.sent = []

        def __aiter__(self):
            async def messages():
                for frame in self.incoming:
                    yield json.dumps(frame)
            return messages()

        async def send(self, frame):
            self.sent.append(json.loads(frame))

    async def run():
        controls = [_control("open", "opaque:01"), _control(), _control("terminate", "old")]
        channel = Channel()
        await TransportSession(channel)._ws_to_channel(WebSocket(controls))
        assert channel.sent == controls
        responses = [{"type": kind, "launch_id": "opaque:01"}
                     for kind in ("ready", "runtime.unavailable", "exit", "sessions")]
        channel = Channel([{"type": "ipc_delivery", "delivery_id": i, "frame": f}
                           for i, f in enumerate(responses)])
        ws = WebSocket()
        await TransportSession(channel)._channel_to_ws(ws)
        assert ws.sent == responses
        assert len(channel.sent) == len(responses)
    asyncio.run(run())


def test_per_turn_resume_rechecks_marker_on_every_spawn(tmp_path, monkeypatch):
    async def run():
        h = _SupervisorHarness(tmp_path, monkeypatch)
        h.sup.agents["a"] = {"id": "a", "runtime": "copilot-cli"}
        _reserve(h, runtime="copilot-cli-structured")
        launches = []

        async def spawn(*argv, **kwargs):
            launches.append(argv)
            assert "--resume" in argv and SID in argv
            assert "--session-id" not in argv
            raise OSError("injected failure before process creation")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        try:
            await h.sup.handle_control(_control())
            session = h.sup.ptys[("a", SID)]
            h.sessions.append(session)
            assert session._per_turn
            await _turn(session)
            assert len(launches) == 1
            assert h.store.native_context("a", SID).state == "attempted"
            h.store.forget_native_context("a", SID)
            await _turn(session)
            assert len(launches) == 1  # No second spawn, and no create fallback.
            assert h.store.native_context("a", SID) is None
            with NativeWriterLease(str(h.root), "copilot-cli", SID):
                pass
        finally:
            await _close(h)
    asyncio.run(run())


def test_capabilities_advertise_generation_echo_and_only_native_explicit_resume():
    for adapter in runtimes.all_adapters():
        features = adapter.capabilities(installed=True, version="test")["features"]
        assert features["session_lifecycle"] == 1
        assert features.get("context", {}).get("explicit_resume", False) == bool(
            adapter.structured and adapter.context_control is not None)


def _effect_control(kind, token, *, missing=False):
    frame = _control(kind, token)
    frame.update(data="a complete turn", client_input_id="128be061-283c-4609-9123-d5e1f882782a",
                 options={}, cols=80, rows=24, request_id="permission-1", allow=True)
    if missing:
        frame.pop("launch_id")
    return frame


def _observe_controls(monkeypatch, session):
    effects = []
    monkeypatch.setattr(session, "write_turn", lambda data, options: (
        effects.append(("input", data, options)) or True))
    monkeypatch.setattr(session, "resize", lambda cols, rows: (
        effects.append(("resize", cols, rows))))
    monkeypatch.setattr(session, "respond_permission", lambda request_id, allow: (
        effects.append(("permission", request_id, allow))))
    return effects


@pytest.mark.parametrize("kind", ["input", "resize", "permission", "close", "terminate"])
@pytest.mark.parametrize("transition", ["reattach", "replace", "legacy-upgrade"])
def test_controls_cannot_cross_resume_generation(tmp_path, monkeypatch, kind, transition):
    async def run():
        h = _SupervisorHarness(tmp_path, monkeypatch)
        try:
            old_token = None if transition == "legacy-upgrade" else "old"
            await h.sup.handle_control(_control("open", old_token))
            old = h.sup.ptys[("a", SID)]
            h.sessions.append(old)
            _reserve(h)
            if transition == "replace":
                await h.sup.handle_control(_control("terminate", "old"))
                await h.sup.wait_closed()
            await h.sup.handle_control(_control())
            session = h.sup.ptys[("a", SID)]
            h.sessions.append(session)
            assert (session is old) == (transition != "replace")
            effects = _observe_controls(monkeypatch, session)
            for token, missing in [("old", False), (None, False), (None, True),
                                   ("legacy", False), ("unrelated", False)]:
                before = list(h.sup.pending)
                await h.sup.handle_control(_effect_control(kind, token, missing=missing))
                assert h.sup.pending == before  # No stale input ACK/receipt either.
                assert h.sup.ptys[("a", SID)] is session
                assert session.is_alive()
                assert effects == []
            await h.sup.handle_control(_effect_control(kind, "new"))
            if kind in ("close", "terminate"):
                assert ("a", SID) not in h.sup.ptys
                assert not session.is_alive()
            else:
                assert len(effects) == 1 and effects[0][0] == kind
                if kind == "input":
                    ack = _frames(h.sup, "input_ack")
                    assert len(ack) == 1 and ack[0]["status"] == "delivered"
            assert h.launches == []
        finally:
            await _close(h)
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["input", "resize", "permission", "close", "terminate"])
@pytest.mark.parametrize("missing", [True, False], ids=["no-field", "null"])
def test_legacy_sessions_keep_tokenless_controls(tmp_path, monkeypatch, kind, missing):
    async def run():
        h = _SupervisorHarness(tmp_path, monkeypatch)
        try:
            opened = _control("open", None)
            if missing:
                opened.pop("launch_id")
            await h.sup.handle_control(opened)
            session = h.sup.ptys[("a", SID)]
            h.sessions.append(session)
            assert h.sup.pty_launch_ids[("a", SID)] is None
            effects = _observe_controls(monkeypatch, session)
            for token in ("new", "old", "legacy"):
                await h.sup.handle_control(_effect_control(kind, token))
                assert effects == [] and session.is_alive()
            # The server's internal legacy sentinel must be mapped to None,
            # not treated as a wildcard by the connector.
            await h.sup.handle_control(_effect_control(kind, None, missing=missing))
            if kind in ("close", "terminate"):
                assert ("a", SID) not in h.sup.ptys
                assert not session.is_alive()
            else:
                assert len(effects) == 1 and effects[0][0] == kind
            assert h.launches == []
        finally:
            await _close(h)
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["open", "resume"])
@pytest.mark.parametrize("missing", [True, False], ids=["no-field", "null"])
def test_tokenless_reconnect_cannot_downgrade_modern_session(tmp_path, monkeypatch, kind, missing):
    async def run():
        h = _SupervisorHarness(tmp_path, monkeypatch)
        try:
            _reserve(h)
            await h.sup.handle_control(_control())
            session = h.sup.ptys[("a", SID)]
            h.sessions.append(session)
            before = list(h.sup.pending)
            frame = _control(kind, None)
            if missing:
                frame.pop("launch_id")
            await h.sup.handle_control(frame)
            assert h.sup.pending == before
            assert h.sup.pty_launch_ids[("a", SID)] == "new"
            assert h.sup.ptys[("a", SID)] is session
            assert session.is_alive()
            assert h.launches == []
        finally:
            await _close(h)
    asyncio.run(run())


def test_pending_resume_accepts_no_controls_for_the_previous_child(tmp_path, monkeypatch):
    async def run():
        h = _SupervisorHarness(tmp_path, monkeypatch)
        pending = lock = None
        try:
            await h.sup.handle_control(_control("open", "old"))
            session = h.sup.ptys[("a", SID)]
            h.sessions.append(session)
            _reserve(h)
            effects = _observe_controls(monkeypatch, session)
            lock = h.sup._open_locks.setdefault(("a", SID), asyncio.Lock())
            await lock.acquire()
            pending = asyncio.create_task(h.sup.handle_control(_control()))
            await _until(lambda: h.sup._open_generations.get(("a", SID)) == ("new", True))
            for kind in ("input", "resize", "permission"):
                for token, missing in [("new", False), ("old", False),
                                       (None, False), (None, True)]:
                    await h.sup.handle_control(_effect_control(kind, token, missing=missing))
            assert effects == [] and not _frames(h.sup, "input_ack")
            for kind in ("open", "resume", "close", "terminate"):
                await asyncio.wait_for(h.sup.handle_control(_control(kind, None)), 3)
            assert h.sup._open_generations[("a", SID)] == ("new", True)
            assert session.is_alive()
            lock.release()
            await pending
            assert h.sup.pty_launch_ids[("a", SID)] == "new"
            assert h.sup.ptys[("a", SID)] is session
            for kind in ("input", "resize", "permission"):
                await h.sup.handle_control(_effect_control(kind, "new"))
            assert [e[0] for e in effects] == ["input", "resize", "permission"]
            assert not h.launches
        finally:
            if lock is not None and lock.locked():
                lock.release()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await _close(h)
    asyncio.run(run())


def test_late_cancelled_spawn_holds_writer_until_reaped_and_cannot_touch_resume(tmp_path, monkeypatch):
    async def run():
        h = _SupervisorHarness(tmp_path, monkeypatch)
        _reserve(h)
        entered, return_child = asyncio.Event(), asyncio.Event()
        proc = h.proc()
        proc.allow_reap.clear()
        spawn = h.spawn

        async def delayed_spawn(*argv, **kwargs):
            entered.set()
            await return_child.wait()
            return await spawn(*argv, **kwargs)

        h.on_spawn = lambda argv, kwargs: proc
        monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
        try:
            await h.sup.handle_control(_control("resume", "old"))
            old = h.sup.ptys[("a", SID)]
            h.sessions.append(old)
            await h.sup.handle_control(_effect_control("input", "old"))
            await asyncio.wait_for(entered.wait(), 3)
            turn = old._turn_task
            await h.sup.handle_control(_control("terminate", "old"))
            turn.cancel()
            await _until(lambda: bool(old._launch_cleanups))
            turn.cancel()  # Repeated cancellation cannot abandon ownership.
            await h.sup.handle_control(_control())
            assert ("a", SID) not in h.sup.ptys
            assert _frames(h.sup, "runtime.unavailable")[-1]["code"] == "context.in_use"
            return_child.set()
            await asyncio.wait_for(proc.wait_started.wait(), 3)
            assert proc.killed and not proc.reaped
            _assert_busy(h.root)
            await h.sup.handle_control(_control())
            assert ("a", SID) not in h.sup.ptys
            proc.allow_reap.set()
            await asyncio.wait_for(h.sup.wait_closed(), 3)
            assert proc.reaped
            _assert_available(h.root)
            await h.sup.handle_control(_control())
            current = h.sup.ptys[("a", SID)]
            h.sessions.append(current)
            assert current is not old
            assert _frames(h.sup, "ready")[-1]["launch_id"] == "new"
            effects = _observe_controls(monkeypatch, current)
            before = list(h.sup.pending)
            await old.on_exit(9)
            for kind in ("input", "permission", "resize", "terminate"):
                await h.sup.handle_control(_effect_control(kind, "old"))
            assert h.sup.pending == before and effects == []
            assert current.is_alive() and len(h.launches) == 1
            _assert_busy(h.root)
        finally:
            return_child.set()
            proc.allow_reap.set()
            await _close(h)
        _assert_available(h.root)
    asyncio.run(run())
