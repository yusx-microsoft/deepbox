"""Offline tests for the Copilot CLI structured adapter (per-turn mode).

No real ``copilot`` process and no token spend: synthetic
``--output-format json`` transcripts are fed through the pure translator and
through a fake subprocess to exercise the per-turn drive mode of
:class:`connector.agent_session.StructuredAgentSession`.
"""
import asyncio
import json

from connector import agent_session as A


def _evs(obj):
    return A.translate_copilot_event(obj)


def test_message_delta_becomes_message_delta():
    out = _evs({"type": "assistant.message_delta",
                "data": {"deltaContent": "Hel"}})
    assert out == [{"ev": A.EV_MESSAGE_DELTA, "text": "Hel"}]


def test_empty_delta_dropped():
    assert _evs({"type": "assistant.message_delta", "data": {}}) == []


def test_assistant_message_final_and_tool_requests():
    out = _evs({"type": "assistant.message", "data": {
        "content": "done",
        "toolRequests": [
            {"id": "t1", "name": "shell", "arguments": {"cmd": "ls"}},
        ],
    }})
    assert out[0] == {"ev": A.EV_MESSAGE, "final": True, "text": "done"}
    assert out[1]["ev"] == A.EV_TOOL_CALL
    assert out[1]["tool"] == "shell"
    assert out[1]["tool_id"] == "t1"
    assert out[1]["input"] == {"cmd": "ls"}


def test_tool_completed_becomes_tool_result():
    out = _evs({"type": "tool.execution_completed", "data": {
        "id": "t1", "isError": False,
        "result": [{"type": "text", "text": "ok"}],
    }})
    assert out == [{"ev": A.EV_TOOL_RESULT, "tool_id": "t1",
                    "is_error": False, "content": "ok"}]


def test_turn_end_and_result():
    assert _evs({"type": "assistant.turn_end", "data": {}})[0]["ev"] \
        == A.EV_TURN_END
    assert _evs({"type": "result", "data": {}})[0]["ev"] == A.EV_TURN_END


def test_session_events_become_status():
    out = _evs({"type": "session.tools_loaded", "data": {"count": 3}})
    assert out == [{"ev": A.EV_STATUS, "subtype": "session.tools_loaded"}]


def test_user_message_and_unknown_dropped():
    assert _evs({"type": "user.message", "data": {"content": "hi"}}) == []
    assert _evs({"type": "mystery"}) == []
    assert _evs({}) == []


def test_registry_maps_copilot_translator():
    assert A.TRANSLATORS["copilot-cli-structured"] is A.translate_copilot_event


# --- Fake-subprocess per-turn integration ---------------------------------

class _FakeStream:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return b""


class _FakeStdin:
    def __init__(self):
        self.buf = b""

    def write(self, b):
        self.buf += b

    async def drain(self):
        pass

    def close(self):
        pass


class _FakeProc:
    def __init__(self, out_lines):
        self.stdout = _FakeStream(out_lines)
        self.stderr = _FakeStream([])
        self.stdin = _FakeStdin()
        self.returncode = None

    async def wait(self):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9


def _turn_transcript(text):
    return [
        json.dumps({"type": "session.status",
                    "data": {"state": "ready"}}).encode() + b"\n",
        json.dumps({"type": "assistant.message_delta",
                    "data": {"deltaContent": text}}).encode() + b"\n",
        json.dumps({"type": "assistant.message",
                    "data": {"content": text}}).encode() + b"\n",
        json.dumps({"type": "result", "data": {}}).encode() + b"\n",
    ]


async def _co_per_turn():
    got = []
    exited = []
    spawned_prompts = []

    async def on_output(s):
        got.append(json.loads(s))

    async def on_exit(code):
        exited.append(code)

    async def fake_spawn(prompt=None):
        spawned_prompts.append(prompt)
        return _FakeProc(_turn_transcript("Hi"))

    sess = A.StructuredAgentSession(
        ["copilot"], None, on_output, on_exit,
        spawn=fake_spawn, translate=A.translate_copilot_event,
        per_turn=True, prompt_argv=["-p"])
    await sess.start()
    # Session is alive with no process yet (per-turn defers spawn).
    assert sess.is_alive() is True
    assert sess._proc is None

    sess.write("hello")
    for _ in range(100):
        if len(got) >= 4:
            break
        await asyncio.sleep(0.01)

    # on_exit must NOT fire for a per-turn process finishing; the session stays.
    assert exited == []
    assert sess.is_alive() is True
    assert spawned_prompts == ["hello"]
    evs = [e["ev"] for e in got]
    assert A.EV_MESSAGE_DELTA in evs
    assert A.EV_TURN_END in evs


def test_per_turn_session_drives_one_process_per_turn_sync():
    asyncio.run(_co_per_turn())


async def _co_per_turn_attachment_is_temporary():
    got = []
    built = []
    spawned = []

    async def on_output(value):
        got.append(json.loads(value))

    async def fake_spawn(prompt=None):
        spawned.append(prompt)
        return _FakeProc(_turn_transcript("attached"))

    def build(options, paths):
        built.append((dict(options), tuple(paths),
                      [__import__("pathlib").Path(path).read_bytes()
                       for path in paths]))
        return ["copilot", "--attachment", *paths]

    sess = A.StructuredAgentSession(
        ["copilot"], None, on_output, lambda code: None,
        spawn=fake_spawn, translate=A.translate_copilot_event,
        per_turn=True, prompt_argv=["-p"], command_builder=build,
        attachment_key="attachments", attachment_mode="flag",
        attachment_max_files=2, attachment_max_bytes=32)
    await sess.start()
    sess.write_turn("inspect", {"reasoning_effort": "high", "attachments": [{
        "name": "../note.txt", "type": "text/plain", "size": 5,
        "data": "aGVsbG8=",
    }]})
    for _ in range(100):
        if built and sess._proc is None and not sess._turn_pending:
            break
        await asyncio.sleep(0.01)

    assert spawned == ["inspect"]
    assert built[0][0]["reasoning_effort"] == "high"
    assert built[0][2] == [b"hello"]
    assert ".." not in __import__("pathlib").Path(built[0][1][0]).name
    assert not __import__("pathlib").Path(built[0][1][0]).exists()
    echo = next(event for event in got if event["ev"] == A.EV_USER_ECHO)
    assert echo["attachments"][0]["name"] == "../note.txt"
    config = next(event for event in got if event["ev"] == A.EV_SESSION_CONFIG)
    assert config["options"] == {"reasoning_effort": "high"}


def test_per_turn_attachment_is_temporary_and_not_in_event_payload():
    asyncio.run(_co_per_turn_attachment_is_temporary())


async def _co_attachment_limit_rejected_before_spawn():
    got = []
    spawned = []

    async def on_output(value):
        got.append(json.loads(value))

    async def fake_spawn(prompt=None):
        spawned.append(prompt)
        return _FakeProc([])

    sess = A.StructuredAgentSession(
        ["copilot"], None, on_output, lambda code: None,
        spawn=fake_spawn, per_turn=True,
        attachment_key="attachments", attachment_mode="flag",
        attachment_max_files=1, attachment_max_bytes=4)
    await sess.start()
    one = {"name": "a.txt", "data": "YQ=="}
    sess.write_turn("inspect", {"attachments": [one, one]})
    for _ in range(100):
        if any(event["ev"] == A.EV_ERROR for event in got):
            break
        await asyncio.sleep(0.01)
    assert spawned == []
    assert next(event for event in got if event["ev"] == A.EV_ERROR)[
        "message"] == "Too many attachments"


def test_attachment_limit_rejected_before_spawn():
    asyncio.run(_co_attachment_limit_rejected_before_spawn())


class _BlockingStream:
    async def readline(self):
        await asyncio.Event().wait()


async def _co_lazy_persistent_controls_are_fixed_by_first_turn():
    got = []
    built = []
    proc = _FakeProc([])
    proc.stdout = _BlockingStream()

    async def on_output(value):
        got.append(json.loads(value))

    async def fake_spawn():
        return proc

    def build(options, paths):
        built.append(dict(options))
        return ["claude"]

    sess = A.StructuredAgentSession(
        ["claude"], None, on_output, lambda code: None,
        spawn=fake_spawn, lazy_start=True, command_builder=build,
        session_option_keys=("model", "reasoning_effort"))
    await sess.start()
    assert sess._proc is None
    sess.write_turn("one", {"model": "sonnet", "reasoning_effort": "high"})
    for _ in range(100):
        if built:
            break
        await asyncio.sleep(0.01)
    sess.write_turn("two", {"model": "opus", "reasoning_effort": "low"})
    for _ in range(100):
        if proc.stdin.buf.count(b"\n") >= 2:
            break
        await asyncio.sleep(0.01)
    assert built == [{"model": "sonnet", "reasoning_effort": "high"}]
    configs = [event["options"] for event in got
               if event["ev"] == A.EV_SESSION_CONFIG]
    assert configs[-1] == {"model": "sonnet", "reasoning_effort": "high"}
    sess.kill()


def test_lazy_persistent_controls_are_fixed_by_first_turn():
    asyncio.run(_co_lazy_persistent_controls_are_fixed_by_first_turn())



class _LateProc(_FakeProc):
    def __init__(self):
        super().__init__([])
        self.killed = False
        self.waited = False

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        self.waited = True
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


async def _co_kill_during_per_turn_spawn_reaps_late_process():
    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()
    proc = _LateProc()

    async def spawn(_prompt=None):
        spawn_started.set()
        await release_spawn.wait()
        return proc

    async def on_output(_data):
        return None

    sess = A.StructuredAgentSession(
        ["copilot", "-p"], None, on_output, lambda _rc: None,
        per_turn=True, spawn=spawn,
    )
    await sess.start()
    sess.write_turn("hello")
    await asyncio.wait_for(spawn_started.wait(), timeout=1)
    sess.kill()
    release_spawn.set()
    for _ in range(100):
        if not sess._turn_pending:
            break
        await asyncio.sleep(0.01)
    assert not sess._turn_pending
    assert proc.killed
    assert proc.waited
    assert sess._proc is None


def test_kill_during_per_turn_spawn_reaps_late_process():
    asyncio.run(_co_kill_during_per_turn_spawn_reaps_late_process())


class _GatedStream:
    def __init__(self, started, release):
        self._started = started
        self._release = release

    async def readline(self):
        self._started.set()
        await self._release.wait()
        return b""


class _FlakyTemp:
    def __init__(self, release):
        self._release = release
        self.calls = 0
        self.early = False
        self.cleaned = False

    def cleanup(self):
        self.calls += 1
        if not self._release.is_set():
            self.early = True
        if self.calls == 1:
            raise PermissionError("simulated transient Windows handle")
        self.cleaned = True


async def _co_attachment_cleanup_waits_for_readers_and_retries():
    stderr_started = asyncio.Event()
    stderr_release = asyncio.Event()
    proc = _FakeProc([])
    proc.stderr = _GatedStream(stderr_started, stderr_release)
    temp = _FlakyTemp(stderr_release)

    async def spawn(_prompt=None):
        return proc

    async def on_output(_data):
        return None

    sess = A.StructuredAgentSession(
        ["copilot", "-p"], None, on_output, lambda _rc: None,
        per_turn=True, spawn=spawn,
        attachment_key="attachments", attachment_mode="flag",
        attachment_max_files=1, attachment_max_bytes=10,
    )
    sess._materialize = lambda _attachments: (("held.txt",), temp)
    await sess.start()
    sess.write_turn("inspect", {"attachments": [
        {"name": "held.txt", "data": "eA==", "size": 1},
    ]})
    await asyncio.wait_for(stderr_started.wait(), timeout=1)
    assert not temp.cleaned
    stderr_release.set()
    for _ in range(100):
        if not sess._turn_pending:
            break
        await asyncio.sleep(0.01)
    assert not sess._turn_pending
    assert not temp.early
    assert temp.calls == 2
    assert temp.cleaned


def test_attachment_cleanup_waits_for_readers_and_retries():
    asyncio.run(_co_attachment_cleanup_waits_for_readers_and_retries())
