"""Offline integration of SDK ownership with native CLI lifecycle generations."""
import asyncio
from uuid import uuid4

import pytest

from connector import runtimes
from connector.integrations.deeporca.store import BindingError
from connector.supervisor import SessionSupervisor
from test_deeporca_supervisor import events, input_frame, runtime, until


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
        await until(lambda: any(e["ev"] == "message.delta" for e in events(sup)))
        for kind in ("interrupt", "close", "terminate"):
            await sup.handle_control({"type": kind, "agent_id": aid,
                                      "session_id": sid, "launch_id": "old-launch"})
        assert sup.ptys[aid, sid].is_alive()
        assert store.input_receipt(aid, sid, turn["client_input_id"])["state"] == "running"
        await sup.handle_control({"type": "interrupt", "agent_id": aid,
                                  "session_id": sid, "launch_id": launch})
        await until(lambda: store.input_receipt(
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
