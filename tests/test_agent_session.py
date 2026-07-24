"""Offline tests for the structured agent session (Cut 10).

No real ``claude`` process and no token spend: we feed synthetic Claude
``stream-json`` transcripts through the pure translator and through a fake
subprocess to exercise :class:`connector.agent_session.StructuredAgentSession`.
"""
import asyncio
import json

import pytest

from connector import agent_session as A


def _evs(obj):
    return A.translate_claude_event(obj)


def test_system_init_becomes_status():
    out = _evs({"type": "system", "subtype": "init",
                "session_id": "s1", "model": "sonnet"})
    assert out == [{"ev": A.EV_STATUS, "subtype": "init",
                    "session_id": "s1", "model": "sonnet"}]


def test_text_delta_becomes_message_delta():
    out = _evs({"type": "stream_event",
                "event": {"type": "content_block_delta",
                          "delta": {"type": "text_delta", "text": "Hel"}}})
    assert out == [{"ev": A.EV_MESSAGE_DELTA, "text": "Hel"}]


def test_assistant_text_and_tool_use():
    out = _evs({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "id": "t1", "name": "Bash",
         "input": {"command": "ls"}},
    ]}})
    assert out[0] == {"ev": A.EV_MESSAGE, "text": "hi", "final": True}
    assert out[1]["ev"] == A.EV_TOOL_CALL
    assert out[1]["tool"] == "Bash"
    assert out[1]["tool_id"] == "t1"
    assert out[1]["input"] == {"command": "ls"}


def test_tool_result_from_user_message():
    out = _evs({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "is_error": False,
         "content": [{"type": "text", "text": "file1\nfile2"}]},
    ]}})
    assert out == [{"ev": A.EV_TOOL_RESULT, "tool_id": "t1",
                    "is_error": False, "content": "file1\nfile2"}]


def test_result_becomes_turn_end():
    out = _evs({"type": "result", "subtype": "success", "is_error": False,
                "total_cost_usd": 0.01, "usage": {"input_tokens": 10},
                "result": "done"})
    assert out[0]["ev"] == A.EV_TURN_END
    assert out[0]["cost_usd"] == 0.01
    assert out[0]["result"] == "done"


def test_permission_ask():
    out = _evs({"type": "control_request", "request_id": "r1",
                "request": {"subtype": "can_use_tool", "tool_name": "Write",
                            "input": {"path": "/x"}}})
    assert out == [{"ev": A.EV_PERMISSION_ASK, "request_id": "r1",
                    "tool": "Write", "input": {"path": "/x"}}]


def test_unknown_shape_is_empty():
    assert _evs({"type": "mystery"}) == []
    assert _evs({}) == []


def test_encode_user_message_roundtrip():
    line = A.encode_user_message("hello world")
    assert line.endswith("\n")
    obj = json.loads(line)
    assert obj["type"] == "user"
    assert obj["message"]["content"][0]["text"] == "hello world"


def test_encode_permission_response():
    allow = json.loads(A.encode_permission_response("r1", True))
    assert allow["response"]["request_id"] == "r1"
    assert allow["response"]["response"]["behavior"] == "allow"
    deny = json.loads(A.encode_permission_response("r1", False))
    assert deny["response"]["response"]["behavior"] == "deny"


# --- Fake-subprocess integration ------------------------------------------

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
        self._killed = False

    async def wait(self):
        self.returncode = 0
        return 0

    def kill(self):
        self._killed = True
        self.returncode = -9


async def _co_session_emits():
    transcript = [
        json.dumps({"type": "system", "subtype": "init",
                    "session_id": "s1"}).encode() + b"\n",
        json.dumps({"type": "stream_event",
                    "event": {"type": "content_block_delta",
                              "delta": {"type": "text_delta",
                                        "text": "Hi"}}}).encode() + b"\n",
        json.dumps({"type": "result", "subtype": "success",
                    "is_error": False}).encode() + b"\n",
    ]
    got = []
    exited = []

    async def on_output(s):
        got.append(json.loads(s))

    async def on_exit(code):
        exited.append(code)

    sess = A.StructuredAgentSession(
        ["claude"], None, on_output, on_exit,
        spawn=lambda: _spawn_fake(transcript))
    await sess.start()
    # Let the reader task drain the transcript.
    for _ in range(50):
        if exited:
            break
        await asyncio.sleep(0.01)
    evs = [e["ev"] for e in got]
    assert A.EV_STATUS in evs
    assert A.EV_MESSAGE_DELTA in evs
    assert A.EV_TURN_END in evs
    assert exited == [0]

def test_session_emits_canonical_events_from_transcript_sync():
    asyncio.run(_co_session_emits())


async def _spawn_fake(lines):
    return _FakeProc(lines)


async def _co_write_encodes():
    sess = A.StructuredAgentSession(
        ["claude"], None, _noop, _noop,
        spawn=lambda: _spawn_fake([]))
    await sess.start()
    # Ensure alive before write (reader hasn't hit EOF wait yet in this tick).
    sess._alive = True
    sess.write("do the thing")
    assert b"do the thing" in sess._proc.stdin.buf

def test_write_encodes_user_turn_sync():
    asyncio.run(_co_write_encodes())


async def _co_respond_perm():
    sess = A.StructuredAgentSession(
        ["claude"], None, _noop, _noop,
        spawn=lambda: _spawn_fake([]))
    await sess.start()
    sess._alive = True
    sess.respond_permission("r1", True)
    assert b"control_response" in sess._proc.stdin.buf
    assert b"allow" in sess._proc.stdin.buf

def test_respond_permission_writes_control_sync():
    asyncio.run(_co_respond_perm())


async def _co_canonical_dedup_and_safe_invalid_output():
    events = []

    async def on_output(raw):
        events.append(json.loads(raw))

    sess = A.StructuredAgentSession(["claude"], None, on_output, _noop)
    transcript = [
        {"type": "stream_event", "event": {
            "type": "message_start", "message": {}}},
        {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "hello"}}},
        {"type": "stream_event", "event": {"type": "message_stop"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hello"}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": "ok"},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": "ok"},
    ]
    for item in transcript:
        await sess._handle_line(json.dumps(item).encode())
    await sess._handle_line(b"secret-looking non-json output")

    assert sum(event.get("ev") == A.EV_TURN_END for event in events) == 1
    assert sum(event.get("text") == "hello" for event in events) == 1
    assert not any("secret-looking" in json.dumps(event) for event in events)
    assert events[-1] == {
        "ev": A.EV_ERROR,
        "message": "Agent emitted invalid JSON",
    }


def test_canonical_events_deduplicate_and_hide_invalid_native_output():
    asyncio.run(_co_canonical_dedup_and_safe_invalid_output())


async def _noop(*a, **k):
    pass

async def _co_live_model_control_precedes_next_prompt():
    events = []

    async def on_output(raw):
        events.append(json.loads(raw))

    def controls(previous, current):
        if previous.get("model") == current.get("model"):
            return []
        return [{"subtype": "set_model", "model": current["model"]}]

    sess = A.StructuredAgentSession(
        ["claude"], None, on_output, _noop,
        option_sanitizer=lambda value: dict(value),
        live_control_builder=controls,
        control_timeout=1.0)
    sess._proc = _FakeProc([])
    sess._alive = True

    await sess._dispatch_turn("first", {"model": "sonnet"})
    sess._proc.stdin.buf = b""

    task = asyncio.create_task(
        sess._dispatch_turn("second", {"model": "opus"}))
    for _ in range(50):
        if b"control_request" in sess._proc.stdin.buf:
            break
        await asyncio.sleep(0.01)

    first_line = sess._proc.stdin.buf.splitlines()[0]
    packet = json.loads(first_line)
    assert packet == {
        "type": "control_request",
        "request_id": "deepbox_1",
        "request": {"subtype": "set_model", "model": "opus"},
    }
    assert b'"second"' not in sess._proc.stdin.buf

    await sess._handle_line(json.dumps({
        "type": "control_response",
        "response": {"subtype": "success", "request_id": "deepbox_1"},
    }).encode())
    await task

    assert b'"second"' in sess._proc.stdin.buf
    assert sess._active_options == {"model": "opus"}
    configs = [event for event in events if event["ev"] == A.EV_SESSION_CONFIG]
    assert configs[-1]["options"] == {"model": "opus"}


def test_live_model_control_precedes_next_prompt_sync():
    asyncio.run(_co_live_model_control_precedes_next_prompt())

