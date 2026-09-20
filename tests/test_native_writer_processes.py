"""Native-writer regressions using only owned, local Python child processes."""

import asyncio
import json
import sys

import pytest

from connector.agent_session import StructuredAgentSession
from connector.native_writer import NativeWriterBusy, NativeWriterLease


_TIMEOUT = 8.0
_SLEEP_CHILD = "import time; time.sleep(60)"
_FLOOD_CHILD = r'''
import json
import sys
import time

message = {"type": "assistant", "message": {
    "content": [{"type": "text", "text": "stall the output callback"}]}}
print(json.dumps(message), flush=True)
line = (json.dumps({"type": "flood", "padding": "x" * 1024}) + "\n").encode()
# Many short, valid JSON lines: exercise pipe backpressure, not readline limits.
for _ in range(2048):
    sys.stdout.buffer.write(line * 16)
sys.stdout.flush()
time.sleep(60)
'''


def _lease(root):
    return NativeWriterLease(str(root), "python-process-test", "shared-context")


def _assert_busy(root):
    contender = _lease(root)
    try:
        with pytest.raises(NativeWriterBusy):
            contender.acquire()
    finally:
        contender.release()


async def _ignore(_value):
    pass


async def _wait_until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), _TIMEOUT)


def _track_real_launch(monkeypatch, session, return_gate=None):
    """Keep the default launcher; optionally withhold its actual child handle."""
    real_spawn = session._spawn_process
    children = []
    launches = []
    spawned = asyncio.Event()

    async def tracked_spawn(argv, prompt=None):
        launches.append(asyncio.current_task())
        child = await real_spawn(argv, prompt)
        children.append(child)
        spawned.set()
        if return_gate is not None:
            await return_gate.wait()
        return child

    monkeypatch.setattr(session, "_spawn_process", tracked_spawn)
    return children, launches, spawned


async def _cleanup(session, children, launches, parents):
    """Also clean up on regression failures, independently of session shutdown."""
    session.kill()
    try:
        # Gates are opened by the caller's finally block. Never abandon a launch
        # merely because its calling task was cancelled before receiving a PID.
        if launches:
            await asyncio.wait_for(
                asyncio.shield(asyncio.gather(*launches, return_exceptions=True)),
                _TIMEOUT,
            )
    finally:
        for child in children:
            try:
                child.kill()
            except ProcessLookupError:
                pass
            finally:
                # Emergency cleanup only, AFTER the test's shutdown assertions.
                # Closing our own pipe transports also works if the regression
                # leaves a reader paused or still awaiting the output callback.
                child._transport.close()
        if children:
            await asyncio.wait_for(
                asyncio.gather(*(child.wait() for child in children)), _TIMEOUT,
            )
        await asyncio.wait_for(session.wait_closed(), _TIMEOUT)
        if parents:
            await asyncio.wait_for(
                asyncio.gather(*parents, return_exceptions=True), _TIMEOUT,
            )


@pytest.mark.parametrize("per_turn", [False, True], ids=["persistent", "per_turn"])
def test_double_cancel_pending_real_launch_keeps_writer_until_reaped(
    tmp_path, monkeypatch, per_turn,
):
    async def scenario():
        root = tmp_path / "leases"
        return_child = asyncio.Event()
        session = StructuredAgentSession(
            [sys.executable, "-I", "-B", "-u", "-c", _SLEEP_CHILD],
            str(tmp_path), _ignore, _ignore,
            per_turn=per_turn, writer_lease_factory=lambda: _lease(root),
        )
        children, launches, spawned = _track_real_launch(
            monkeypatch, session, return_child,
        )
        parents = []
        try:
            if per_turn:
                await asyncio.wait_for(session.start(), _TIMEOUT)
                assert session.write_turn("local test prompt") is True
                parent = session._turn_task
            else:
                parent = asyncio.create_task(session.start())
            parents.append(parent)
            await asyncio.wait_for(spawned.wait(), _TIMEOUT)
            assert len(children) == 1
            child = children[0]
            assert isinstance(child, asyncio.subprocess.Process)
            assert child.returncode is None
            assert session._proc is None  # The real handle is not published yet.
            _assert_busy(root)

            assert parent.cancel()
            # Let the first cancellation enter launch cleanup before cancelling
            # that same caller again. The child-return gate stays shut throughout.
            await asyncio.sleep(0)
            assert not parent.done()
            _assert_busy(root)
            assert parent.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(parent), _TIMEOUT)
            assert parent.cancelled()
            assert not return_child.is_set()
            assert child.returncode is None
            _assert_busy(root)

            session.kill()
            closing = asyncio.create_task(session.wait_closed())
            parents.append(closing)
            await asyncio.sleep(0)
            assert not closing.done(), "close must retain the pending child launch"
            _assert_busy(root)

            return_child.set()
            await asyncio.wait_for(closing, _TIMEOUT)
            assert child.returncode is not None, "late child must be killed and reaped"
            assert await asyncio.wait_for(child.wait(), _TIMEOUT) == child.returncode
            with _lease(root):
                pass  # Both the OS lock and active journal must be cleared.
        finally:
            return_child.set()
            await _cleanup(session, children, launches, parents)

    asyncio.run(scenario())


@pytest.mark.parametrize("per_turn", [False, True], ids=["persistent", "per_turn"])
def test_kill_reaps_stdout_flood_without_unblocking_output(
    tmp_path, monkeypatch, per_turn,
):
    async def scenario():
        root = tmp_path / "leases"
        output_entered = asyncio.Event()
        release_output = asyncio.Event()
        output_resumed = asyncio.Event()
        output_cancelled = asyncio.Event()

        async def stalled_output(payload):
            if json.loads(payload).get("ev") != "message":
                return
            output_entered.set()
            try:
                await release_output.wait()
                output_resumed.set()
            except asyncio.CancelledError:
                output_cancelled.set()
                raise

        session = StructuredAgentSession(
            [sys.executable, "-I", "-B", "-u", "-c", _FLOOD_CHILD],
            str(tmp_path), stalled_output, _ignore,
            per_turn=per_turn, writer_lease_factory=lambda: _lease(root),
        )
        children, launches, spawned = _track_real_launch(monkeypatch, session)
        parents = []
        try:
            starting = asyncio.create_task(session.start())
            parents.append(starting)
            await asyncio.wait_for(asyncio.shield(starting), _TIMEOUT)
            if per_turn:
                assert session.write_turn("local test prompt") is True
                parents.append(session._turn_task)
            await asyncio.wait_for(spawned.wait(), _TIMEOUT)
            await asyncio.wait_for(output_entered.wait(), _TIMEOUT)
            assert len(children) == 1
            child = children[0]
            stream = child.stdout
            # Observe real asyncio backpressure, rather than guessing a sleep or
            # merely asserting that the child attempted a large write.
            await _wait_until(lambda: stream._paused)
            assert len(stream._buffer) > 2 * stream._limit
            assert child.returncode is None
            assert not release_output.is_set()
            assert not output_resumed.is_set()
            _assert_busy(root)

            session.kill()
            await asyncio.wait_for(session.wait_closed(), _TIMEOUT)
            assert not release_output.is_set(), "shutdown must not need callback progress"
            assert not output_resumed.is_set()
            assert output_cancelled.is_set()
            assert child.returncode is not None, "flooding child must be reaped"
            assert await asyncio.wait_for(child.wait(), _TIMEOUT) == child.returncode
            with _lease(root):
                pass
        finally:
            # This is only a failure-path safety net, never part of the close test.
            release_output.set()
            await _cleanup(session, children, launches, parents)

    asyncio.run(scenario())
