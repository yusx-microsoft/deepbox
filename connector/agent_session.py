"""Structured agent session: drive a coding agent in headless/streaming mode.

Where :class:`connector.pty_session.PtySession`投屏一个全屏 TUI 的原始终端字节流
(每次重绘、每个按键回显都被迫走网络往返), a :class:`StructuredAgentSession`
instead runs the agent in its **headless structured** mode and translates its
native protocol into a small, agent-agnostic *canonical event* stream. The
browser then renders a real chat UI (message bubbles, tool cards, a permission
prompt) instead of a terminal, so:

  * 用户"打完整段话再发送"——输入不再逐键往返;
  * agent 的真实反应(文本增量、工具调用、结果)以结构化事件流式到达;
  * 接入新 agent(Copilot CLI、Codex……)只需再写一个 translator。

For Claude Code the headless interface is::

    claude -p --output-format stream-json --input-format stream-json \
           --include-partial-messages --verbose [--permission-mode ...]

which speaks newline-delimited JSON on stdio.

Interface parity with :class:`PtySession`
-----------------------------------------
This class exposes the *exact* surface the supervisor already drives —
``start()``, ``write(str)``, ``resize(cols, rows)``, ``kill()``,
``is_alive()`` and the ``on_output`` / ``on_exit`` async callbacks — so the
supervisor only chooses which class to construct. ``on_output`` still receives
a ``str``; for a structured session that string is one canonical event encoded
as JSON, carried in a frame with ``kind="event"``. The server persists and
fans that frame out through the same durable spool / ACK / replay / fence
pipeline as terminal output (it never branches on ``kind``); the browser
demultiplexes on ``kind``.

Security invariants preserved:
  * argv is built + validated by :mod:`connector.runtimes` (no shell, no
    metacharacters); no secrets are ever placed on argv or emitted.
  * We never log or emit prompt/response *content* here beyond forwarding the
    canonical event to the same trusted output path terminal bytes already use.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
import sys
import tempfile
from typing import Awaitable, Callable

IS_WIN = sys.platform == "win32"

# Canonical event names (agent-agnostic). The browser renders on these.
EV_STATUS = "status"          # session/init/system status
EV_MESSAGE_DELTA = "message.delta"   # assistant text increment
EV_MESSAGE = "message"        # a complete assistant message (fallback / final)
EV_TOOL_CALL = "tool.call"    # agent invoked a tool (name + input)
EV_TOOL_RESULT = "tool.result"  # a tool returned
EV_PERMISSION_ASK = "permission.ask"  # agent needs approval to use a tool
EV_TURN_END = "turn.end"      # one assistant turn finished (usage/cost)
EV_USER_ECHO = "user.echo"    # our own user message, replayed for ack
EV_SESSION_CONFIG = "session.config"  # applied model/reasoning controls
EV_ERROR = "error"


def _event(ev: str, **fields) -> dict:
    """Build one canonical event dict."""
    out = {"ev": ev}
    out.update(fields)
    return out


def translate_claude_event(obj: dict) -> list[dict]:
    """Translate one Claude ``stream-json`` object into canonical events.

    Pure function (no I/O) so it can be unit-tested with synthetic transcripts
    — no real ``claude`` process and no token spend. Returns zero or more
    canonical event dicts; unknown shapes yield ``[]`` (forward-compatible).

    Claude Code ``stream-json`` object shapes handled:
      * ``{"type":"system","subtype":"init", ...}``            -> status(init)
      * ``{"type":"stream_event","event":{...}}``  (partials)  -> message.delta / tool.call
      * ``{"type":"assistant","message":{content:[...]}}``     -> message / tool.call (non-partial)
      * ``{"type":"user","message":{content:[tool_result]}}``  -> tool.result
      * ``{"type":"result","subtype":..., ...}``               -> turn.end
      * ``{"type":"control_request", ... can_use_tool ...}``   -> permission.ask
    """
    t = obj.get("type")

    if t == "system":
        return [_event(EV_STATUS, subtype=obj.get("subtype"),
                       session_id=obj.get("session_id"),
                       model=obj.get("model"))]

    if t == "stream_event":
        # Anthropic Messages API streaming deltas (via --include-partial-messages).
        return _translate_stream_event(obj.get("event") or {})

    if t == "assistant":
        # A complete assistant message (arrives even without partials).
        msg = obj.get("message") or {}
        out: list[dict] = []
        for block in msg.get("content") or []:
            bt = block.get("type")
            if bt == "text" and block.get("text"):
                out.append(_event(EV_MESSAGE, text=block["text"],
                                  final=True))
            elif bt == "tool_use":
                out.append(_event(EV_TOOL_CALL,
                                  tool=block.get("name"),
                                  tool_id=block.get("id"),
                                  input=block.get("input")))
        return out

    if t == "user":
        # Tool results come back wrapped as a user message.
        msg = obj.get("message") or {}
        out = []
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    out.append(_event(EV_TOOL_RESULT,
                                      tool_id=block.get("tool_use_id"),
                                      is_error=bool(block.get("is_error")),
                                      content=_flatten_tool_result(
                                          block.get("content"))))
        return out

    if t == "result":
        return [_event(EV_TURN_END,
                       subtype=obj.get("subtype"),
                       is_error=bool(obj.get("is_error")),
                       cost_usd=obj.get("total_cost_usd"),
                       usage=obj.get("usage"),
                       result=obj.get("result"))]

    if t == "control_request":
        # A tool wants to run and the session isn't in an auto-approve mode.
        req = obj.get("request") or {}
        if req.get("subtype") in ("can_use_tool", "permission"):
            return [_event(EV_PERMISSION_ASK,
                           request_id=obj.get("request_id"),
                           tool=req.get("tool_name") or req.get("tool"),
                           input=req.get("input"))]
        return []

    return []


def _translate_stream_event(event: dict) -> list[dict]:
    et = event.get("type")
    if et == "content_block_delta":
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta" and delta.get("text"):
            return [_event(EV_MESSAGE_DELTA, text=delta["text"])]
        if delta.get("type") == "input_json_delta" and delta.get("partial_json"):
            # Streaming tool-input; browser can ignore until the tool.call lands.
            return []
        return []
    if et == "content_block_start":
        block = event.get("content_block") or {}
        if block.get("type") == "tool_use":
            return [_event(EV_TOOL_CALL, tool=block.get("name"),
                           tool_id=block.get("id"), input=block.get("input"),
                           streaming=True)]
        return []
    if et == "message_stop":
        return [_event(EV_MESSAGE, final=True, text="")]
    return []


def _flatten_tool_result(content) -> str:
    """Reduce a tool_result content payload to a display string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return "".join(parts)
    return str(content)


def translate_copilot_event(obj: dict) -> list[dict]:
    """Translate one GitHub Copilot CLI ``--output-format json`` object.

    Pure function (no I/O), unit-tested with synthetic transcripts. Copilot
    emits newline-delimited JSON objects shaped ``{"type","data","id",...}``.
    Handled types:
      * ``assistant.message_delta`` -> message.delta  (data.deltaContent)
      * ``assistant.message``       -> message (final) + tool.call per toolRequest
      * ``assistant.turn_end`` / ``result`` -> turn.end
      * ``session.*`` (mcp/skills/tools loaded, status) -> status (or dropped)
      * ``user.message``            -> [] (our own echo; UI already showed it)
    Unknown/ephemeral shapes yield ``[]`` (forward-compatible).
    """
    t = obj.get("type")
    data = obj.get("data") or {}

    if t == "assistant.message_delta":
        txt = data.get("deltaContent")
        if txt:
            return [_event(EV_MESSAGE_DELTA, text=txt)]
        return []

    if t == "assistant.message":
        out: list[dict] = []
        # The streamed deltas already carried the text; emit a final marker so
        # the UI closes the bubble, then surface any tool requests.
        out.append(_event(EV_MESSAGE, final=True, text=data.get("content") or ""))
        for tr in data.get("toolRequests") or []:
            out.append(_event(EV_TOOL_CALL,
                              tool=tr.get("name") or tr.get("tool"),
                              tool_id=tr.get("id"),
                              input=tr.get("arguments") if "arguments" in tr
                              else tr.get("input")))
        return out

    if t == "tool.execution_started" or t == "tool.call":
        return [_event(EV_TOOL_CALL, tool=data.get("name"),
                       tool_id=data.get("id") or data.get("toolCallId"),
                       input=data.get("arguments") or data.get("input"))]

    if t in ("tool.execution_completed", "tool.result"):
        return [_event(EV_TOOL_RESULT,
                       tool_id=data.get("id") or data.get("toolCallId"),
                       is_error=bool(data.get("isError") or data.get("error")),
                       content=_flatten_tool_result(
                           data.get("result") if "result" in data
                           else data.get("content")))]

    if t in ("assistant.turn_end", "result"):
        return [_event(EV_TURN_END, subtype=t,
                       is_error=bool((data or {}).get("error")))]

    if t and t.startswith("session."):
        # Startup/system status; keep it lightweight and non-content.
        return [_event(EV_STATUS, subtype=t)]

    return []


def encode_user_message(text: str) -> str:
    """Encode a user turn as one Claude ``stream-json`` stdin line."""
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }) + "\n"


def encode_permission_response(request_id: str, allow: bool) -> str:
    """Encode a control response approving/denying a tool-use request."""
    return json.dumps({
        "type": "control_response",
        "response": {
            "request_id": request_id,
            "subtype": "success" if allow else "error",
            "response": {"behavior": "allow" if allow else "deny"},
        },
    }) + "\n"


# Translator registry: pick by runtime id so adding an agent is one function
# plus one register() — no changes to the session machinery.
TRANSLATORS: dict = {}


def register_translator(runtime_id: str, fn):
    TRANSLATORS[runtime_id] = fn


register_translator("claude-code-structured", translate_claude_event)
register_translator("copilot-cli-structured", translate_copilot_event)


_LOG = logging.getLogger(__name__)
_TEMP_CLEANUP_DELAYS = (0.0, 0.05, 0.2, 0.5)


async def _terminate_process(proc) -> None:
    # Best-effort kill and reap for a process that the session no longer owns.
    try:
        if getattr(proc, "returncode", None) is None:
            proc.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        await proc.wait()
    except Exception:
        pass


async def _cleanup_tempdir(temp) -> bool:
    # Retry transient Windows file-handle failures without faulting the turn task.
    for delay in _TEMP_CLEANUP_DELAYS:
        if delay:
            await asyncio.sleep(delay)
        try:
            temp.cleanup()
            return True
        except OSError:
            continue
    return False


class StructuredAgentSession:
    """Drive one agent through a canonical structured-event stream.

    Persistent agents normally start immediately for backwards compatibility.
    The supervisor opts into ``lazy_start`` so session-scoped controls from the
    first browser turn can be applied before any CLI process is spawned.
    Per-turn agents always spawn once per prompt.
    """

    def __init__(self, cmd: list[str], cwd: str | None,
                 on_output: Callable[[str], Awaitable[None]],
                 on_exit: Callable[[int], Awaitable[None]],
                 cols: int = 120, rows: int = 30,
                 spawn: Callable[..., Awaitable] | None = None,
                 translate=None, per_turn: bool = False,
                 prompt_argv=None, lazy_start: bool = False,
                 command_builder=None, option_sanitizer=None,
                 attachment_key: str | None = None,
                 attachment_mode: str | None = None,
                 attachment_max_files: int = 0,
                 attachment_max_bytes: int = 0,
                 session_option_keys: tuple[str, ...] = ()):
        self.cmd = cmd
        self.cwd = cwd or None
        self.on_output = on_output
        self.on_exit = on_exit
        self.cols = cols
        self.rows = rows
        self._custom_spawn = spawn
        self._translate = translate or translate_claude_event
        self._per_turn = per_turn
        self._prompt_argv = list(prompt_argv or [])
        self._lazy_start = lazy_start
        self._command_builder = command_builder
        self._option_sanitizer = option_sanitizer or (
            lambda value: value if isinstance(value, dict) else {})
        self._attachment_key = attachment_key
        self._attachment_mode = attachment_mode
        self._attachment_max_files = max(0, int(attachment_max_files or 0))
        self._attachment_max_bytes = max(0, int(attachment_max_bytes or 0))
        self._session_option_keys = tuple(session_option_keys)
        self._session_options: dict[str, object] | None = None
        self._turn_end_seen = False
        self._streamed_assistant_text = False
        self._proc = None
        self._alive = False
        self._stderr_tail: list[str] = []
        self._spawn_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._turn_pending = False

    async def _spawn_process(self, argv: list[str], prompt: str | None = None):
        if self._custom_spawn is not None:
            if self._per_turn:
                return await self._custom_spawn(prompt)
            return await self._custom_spawn()
        return await asyncio.create_subprocess_exec(
            *argv, cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    def _command(self, options: dict, paths: tuple[str, ...] = ()) -> list[str]:
        if self._command_builder is None:
            return list(self.cmd)
        return list(self._command_builder(options, paths))

    async def start(self):
        if self._per_turn or self._lazy_start:
            # Logically live while waiting for the first full user turn.
            self._alive = True
            self._proc = None
            await self._emit(_event(EV_STATUS, subtype="ready"))
            return
        self._proc = await self._spawn_process(list(self.cmd))
        self._alive = True
        self._start_readers(self._proc)

    def _start_readers(self, proc):
        asyncio.create_task(self._read_stdout(proc))
        if getattr(proc, "stderr", None) is not None:
            asyncio.create_task(self._read_stderr(proc))

    def _attachment_metadata(self, options: dict) -> list[dict]:
        raw = options.get(self._attachment_key) if self._attachment_key else None
        if not isinstance(raw, list):
            return []
        result = []
        for item in raw[:self._attachment_max_files]:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            size = item.get("size")
            media_type = item.get("type")
            if isinstance(name, str) and name:
                result.append({
                    "name": name[:255],
                    "size": size if isinstance(size, int) and size >= 0 else None,
                    "type": media_type[:100] if isinstance(media_type, str) else "",
                })
        return result

    def _decode_attachments(self, options: dict) -> list[dict]:
        if not self._attachment_key:
            return []
        raw = options.get(self._attachment_key)
        if raw is None:
            return []
        if not isinstance(raw, list) or len(raw) > self._attachment_max_files:
            raise ValueError("Too many attachments")
        decoded = []
        total = 0
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("Invalid attachment")
            name = item.get("name")
            payload = item.get("data")
            if not isinstance(name, str) or not name or len(name) > 255:
                raise ValueError("Invalid attachment name")
            if not isinstance(payload, str):
                raise ValueError(f"Attachment {name} has no data")
            try:
                data = base64.b64decode(payload, validate=True)
            except Exception as exc:
                raise ValueError(f"Attachment {name} is not valid base64") from exc
            total += len(data)
            if total > self._attachment_max_bytes:
                raise ValueError("Attachments exceed this runtime's size limit")
            decoded.append({
                "name": name,
                "type": item.get("type") if isinstance(item.get("type"), str) else "",
                "data": data,
            })
        return decoded

    @staticmethod
    def _safe_filename(index: int, name: str) -> str:
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in base)
        return f"{index:02d}-{safe or 'attachment'}"

    def _materialize(self, attachments: list[dict]):
        temp = tempfile.TemporaryDirectory(prefix="deepbox-attachments-")
        paths = []
        for index, item in enumerate(attachments, 1):
            path = Path(temp.name) / self._safe_filename(index, item["name"])
            path.write_bytes(item["data"])
            paths.append(str(path))
        return tuple(paths), temp

    @staticmethod
    def _embed_text_attachments(prompt: str, attachments: list[dict]) -> str:
        if not attachments:
            return prompt
        blocks = []
        for item in attachments:
            try:
                text = item["data"].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"Attachment {item['name']} must be a UTF-8 text file") from exc
            blocks.append(
                f"<file name={json.dumps(item['name'], ensure_ascii=False)}>\n"
                f"{text}\n</file>")
        return (prompt + "\n\n<deepbox_attachments>\n" +
                "\n".join(blocks) + "\n</deepbox_attachments>")

    async def _run_one_turn(self, prompt: str, options: dict,
                            attachments: list[dict]):
        # Spawn a fresh process for one prompt and clean temporary files.
        temp = None
        proc = None
        stderr_task = None
        paths: tuple[str, ...] = ()
        try:
            if not self._alive:
                return
            if attachments and self._attachment_mode == "flag":
                paths, temp = self._materialize(attachments)
            elif attachments:
                prompt = self._embed_text_attachments(prompt, attachments)
            argv = self._command(options, paths) + self._prompt_argv + [prompt]
            async with self._spawn_lock:
                if not self._alive:
                    return
                proc = await self._spawn_process(argv, prompt)
                # kill() can run while process creation is awaiting. Never publish a
                # late process into session state: terminate and reap it here.
                if not self._alive:
                    await _terminate_process(proc)
                    return
                self._proc = proc
                if getattr(proc, "stderr", None) is not None:
                    stderr_task = asyncio.create_task(self._read_stderr(proc))
                try:
                    await self._drain_turn(proc)
                finally:
                    # If output handling failed, stop the child before waiting for
                    # stderr; otherwise a full pipe can keep cleanup and exit stuck.
                    if getattr(proc, "returncode", None) is None:
                        await _terminate_process(proc)
                    if stderr_task is not None:
                        await asyncio.gather(stderr_task, return_exceptions=True)
        finally:
            if self._proc is proc:
                self._proc = None
            if temp is not None and not await _cleanup_tempdir(temp):
                _LOG.warning("temporary attachment cleanup failed after retries")


    async def _drain_turn(self, proc):
        stream = proc.stdout
        while True:
            try:
                line = await stream.readline()
            except (asyncio.LimitOverrunError, ValueError):
                continue
            except Exception:
                break
            if not line:
                break
            await self._handle_line(line)
        code = 0
        try:
            code = await proc.wait()
        except Exception:
            pass
        # Some CLIs communicate completion only by exiting. Do not add a second
        # turn boundary when their structured transcript already supplied one.
        if not self._turn_end_seen:
            self._turn_end_seen = True
            await self._emit(_event(
                EV_TURN_END, subtype="process_exit", is_error=bool(code),
                result=None))

    async def _read_stdout(self, proc):
        stream = proc.stdout
        while self._alive and self._proc is proc:
            try:
                line = await stream.readline()
            except (asyncio.LimitOverrunError, ValueError):
                continue
            except Exception:
                break
            if not line:
                break
            await self._handle_line(line)
        if self._proc is not proc:
            return
        self._alive = False
        code = 0
        try:
            code = await proc.wait()
        except Exception:
            pass
        await self.on_exit(int(code or 0))

    async def _read_stderr(self, proc):
        stream = proc.stderr
        while True:
            try:
                line = await stream.readline()
            except Exception:
                break
            if not line:
                break
            try:
                self._stderr_tail.append(line.decode(errors="replace"))
                del self._stderr_tail[:-20]
            except Exception:
                pass

    async def _handle_line(self, raw: bytes):
        text = raw.decode(errors="replace").strip()
        if not text:
            return
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            # Never relay arbitrary native output; it can contain provider
            # internals or secrets and is not part of the display-safe protocol.
            await self._emit(_event(EV_ERROR, message="Agent emitted invalid JSON"))
            return

        native_type = obj.get("type")
        stream_type = ((obj.get("event") or {}).get("type")
                       if native_type == "stream_event" else None)
        if stream_type == "message_start":
            self._streamed_assistant_text = False

        events = self._translate(obj)
        # Claude emits both partial Messages API events and a full assistant
        # snapshot when --include-partial-messages is enabled. Keep the live
        # deltas and suppress only duplicated text in the later snapshot; tool
        # snapshots remain useful because they contain the completed input.
        if native_type == "assistant" and self._streamed_assistant_text:
            events = [event for event in events
                      if event.get("ev") != EV_MESSAGE]
            self._streamed_assistant_text = False

        for event in events:
            ev = event.get("ev")
            if (native_type == "stream_event" and ev == EV_MESSAGE_DELTA
                    and event.get("text")):
                self._streamed_assistant_text = True
            if ev == EV_TURN_END:
                if self._turn_end_seen:
                    continue
                self._turn_end_seen = True
            await self._emit(event)

    async def _emit(self, ev: dict):
        await self.on_output(json.dumps(ev))

    async def _dispatch_turn(self, data: str, raw_options: object):
        self._turn_end_seen = False
        options = self._option_sanitizer(raw_options)
        if not self._per_turn and self._session_option_keys:
            if self._session_options is None:
                self._session_options = {
                    key: options.get(key) for key in self._session_option_keys}
            else:
                for key, value in self._session_options.items():
                    if value is None:
                        options.pop(key, None)
                    else:
                        options[key] = value
        public_options = {
            key: value for key, value in options.items()
            if isinstance(value, (str, int, float, bool))
        }
        await self._emit(_event(EV_SESSION_CONFIG, options=public_options))
        await self._emit(_event(
            EV_USER_ECHO, text=data,
            attachments=self._attachment_metadata(options)))
        try:
            attachments = self._decode_attachments(options)
            if self._per_turn:
                await self._run_one_turn(data, options, attachments)
                return
            async with self._write_lock:
                prompt = self._embed_text_attachments(data, attachments)
                if self._proc is None:
                    self._proc = await self._spawn_process(self._command(options))
                    self._start_readers(self._proc)
                stdin = self._proc.stdin
                if stdin is None:
                    raise ValueError("Agent stdin is unavailable")
                stdin.write(encode_user_message(prompt).encode())
                drain = getattr(stdin, "drain", None)
                if drain is not None:
                    await drain()
        except ValueError as exc:
            await self._emit(_event(EV_ERROR, message=str(exc)))
            await self._emit(_event(EV_TURN_END, subtype="input_error"))

    def is_alive(self) -> bool:
        if not self._alive:
            return False
        if self._per_turn or (self._lazy_start and self._proc is None):
            return True
        if self._proc is None:
            return False
        rc = getattr(self._proc, "returncode", None)
        if rc is not None:
            self._alive = False
            return False
        return True

    def write_turn(self, data: str, options: object = None):
        """Send one complete user turn plus declarative runtime options."""
        if not self.is_alive() or not isinstance(data, str):
            return
        if self._per_turn:
            if self._turn_pending or self._proc is not None:
                return
            self._turn_pending = True

            async def run():
                try:
                    await self._dispatch_turn(data, options)
                finally:
                    self._turn_pending = False

            asyncio.create_task(run())
            return
        asyncio.create_task(self._dispatch_turn(data, options))

    def write(self, data: str):
        # Keep the historical synchronous compatibility surface for direct
        # callers/tests. The supervisor uses write_turn so browser turns still
        # receive canonical user/config events and attachment handling.
        if (self.is_alive() and not self._per_turn and self._proc is not None
                and self._proc.stdin is not None):
            try:
                self._proc.stdin.write(encode_user_message(data).encode())
            except Exception:
                pass
            return
        self.write_turn(data, {})

    def respond_permission(self, request_id: str, allow: bool):
        if not self.is_alive() or self._proc is None:
            return
        stdin = self._proc.stdin
        if stdin is None:
            return
        try:
            stdin.write(encode_permission_response(request_id, allow).encode())
        except Exception:
            pass

    def resize(self, cols: int, rows: int):
        self.cols = cols
        self.rows = rows

    def kill(self):
        self._alive = False
        if self._proc is None:
            return
        try:
            self._proc.kill()
        except Exception:
            pass
