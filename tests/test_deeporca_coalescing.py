"""Bounded native delta batching; no provider/network or real-time sleeps."""
import asyncio

import pytest

from connector.integrations.deeporca import worker as worker_module
from connector.integrations.deeporca.events import IntegrationError, json_bytes
from connector.integrations.deeporca.worker import _NativeEventBatcher, _COALESCE_BYTES, _COALESCE_DELAY
from test_deeporca_worker import SDK_FIXTURE, make_binding, make_session, until


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
            await until(lambda: bool(settlements))
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
