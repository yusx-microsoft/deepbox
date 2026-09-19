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


async def until(predicate):
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
            await until(lambda: bool(settlements))
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
            await until(lambda: any(e["ev"] == "message.delta" for e in events))
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
            await until(lambda: bool(settlements))
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
            await until(lambda: len(settlements) == 2)
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
            await until(lambda: bool(settlements))
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
            await until(lambda: bool(settlements))
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
            await until(lambda: any(e["ev"] == "message.delta" for e in events))
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
                await until(lambda: worker.can_accept_turn())
            assert settlements == ["completed"] * 4
            assert [e["text"] for e in first_events if e["ev"] == "message.delta"] == ["hello chat-1"] * 2
            assert [e["text"] for e in second_events if e["ev"] == "message.delta"] == ["hello chat-2"] * 2
        finally:
            await worker.close()
    asyncio.run(case())
