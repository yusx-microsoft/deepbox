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
