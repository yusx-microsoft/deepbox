"""Structured agent session: drive a coding agent in headless/streaming mode.

Unlike :class:`connector.pty_session.PtySession`, which relays the raw byte
stream of a full-screen TUI, :class:`StructuredAgentSession` runs an agent in
headless mode and translates its native protocol into a small, agent-agnostic
event stream. The browser renders messages, tool cards, and permission prompts
instead of terminal redraws, so:

  * users send complete messages instead of round-tripping every keystroke;
  * text increments, tool calls, and results arrive as structured events; and
  * a new agent runtime needs only an adapter that translates its protocol.

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
from collections import deque
import json
import logging
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Awaitable, Callable

IS_WIN = sys.platform == "win32"
MAX_QUEUED_TURNS = 16

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
_SPAWN_RETRY_DELAYS = (0.1, 0.35)


def _resolve_spawn_argv(argv: list[str]) -> list[str]:
    """Resolve argv[0] so Windows does not race PATH/app aliases."""
    if not argv or not IS_WIN:
        return list(argv)
    executable = shutil.which(argv[0])
    if not executable:
        return list(argv)
    return [executable, *argv[1:]]


def _is_windows_access_denied(exc: BaseException) -> bool:
    return (
        IS_WIN
        and isinstance(exc, PermissionError)
        and (getattr(exc, "winerror", None) == 5 or exc.errno == 13)
    )


def _process_start_error(exc: OSError) -> str:
    if _is_windows_access_denied(exc):
        return (
            "Windows denied access while starting the agent CLI after retries. "
            "Check the CLI executable permission, then retry the turn."
        )
    return "The agent CLI could not be started. Check the local CLI installation and retry."


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
                 session_option_keys: tuple[str, ...] = (),
                 live_control_builder=None,
                 control_timeout: float = 10.0,
                 context_started: Callable[[], Awaitable[None]] | None = None,
                 context_preparing: Callable[[], None] | None = None,
                 writer_lease_factory=None,
                 require_existing_context: bool = False):
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
        self._live_control_builder = live_control_builder
        self._control_timeout = max(0.1, float(control_timeout))
        self._context_started = context_started
        self._context_preparing = context_preparing
        # A resume may tighten an existing lazy session, never loosen it back
        # into a create. The preparing hook enforces this at every spawn.
        self.require_existing_context = require_existing_context
        self._writer_lease_factory = writer_lease_factory
        self._writer_lease = None
        self._writer_proc = None
        self._last_reaped = None
        self._reap_lock = asyncio.Lock()
        self._launch_task = None
        self._launch_cleanups: set[asyncio.Task] = set()
        self._close_task = None
        self._reader_tasks: list[asyncio.Task] = []
        self._exit_reported = False
        self._context_ready_reported = False
        self._active_options: dict[str, object] | None = None
        self._control_counter = 0
        self._pending_controls: dict[str, asyncio.Future] = {}
        self._turn_end_seen = False
        self._streamed_assistant_text = False
        self._proc = None
        self._alive = False
        self._killed = False
        self._stderr_tail: list[str] = []
        self._spawn_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._turn_pending = False
        self._turn_queue: deque[tuple[str, object]] = deque()
        self._turn_task: asyncio.Task | None = None
        self._queue_notice_task: asyncio.Task | None = None

    async def _spawn_process(self, argv: list[str], prompt: str | None = None):
        if self._killed:
            raise RuntimeError("Session is closed")
        if self._custom_spawn is not None:
            if self._per_turn:
                return await self._custom_spawn(prompt)
            return await self._custom_spawn()

        resolved_argv = _resolve_spawn_argv(argv)
        for attempt in range(len(_SPAWN_RETRY_DELAYS) + 1):
            if self._killed:
                raise RuntimeError("Session is closed")
            try:
                return await asyncio.create_subprocess_exec(
                    *resolved_argv, cwd=self.cwd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except PermissionError as exc:
                if (
                    not _is_windows_access_denied(exc)
                    or attempt >= len(_SPAWN_RETRY_DELAYS)
                ):
                    raise
                _LOG.warning(
                    "Windows denied an agent process start; retrying (%d/%d)",
                    attempt + 1,
                    len(_SPAWN_RETRY_DELAYS),
                )
                await asyncio.sleep(_SPAWN_RETRY_DELAYS[attempt])
                resolved_argv = _resolve_spawn_argv(argv)
        raise RuntimeError("unreachable process-spawn retry state")

    def _command(self, options: dict, paths: tuple[str, ...] = ()) -> list[str]:
        if self._command_builder is None:
            return list(self.cmd)
        return list(self._command_builder(options, paths))

    def _release_writer(self):
        lease, self._writer_lease = self._writer_lease, None
        if lease is not None:
            # release never erases an active journal. Unknown child state stays
            # quarantined even if this supervisor goes away.
            lease.release()

    async def _wait_process(self, proc):
        async with self._reap_lock:
            if self._last_reaped is not None and self._last_reaped[0] is proc:
                return self._last_reaped[1]
            code = await self._wait_process_once(proc)
            self._last_reaped = (proc, code)
            return code

    async def _wait_process_once(self, proc):
        code = await proc.wait()
        if self._writer_proc is proc and self._writer_lease is not None:
            try:
                self._writer_lease.process_reaped()
            except Exception:
                self._alive = False
                self._killed = True
                self._turn_queue.clear()
                self._release_writer()
                raise ValueError("Native writer cleanup could not be saved. Local recovery is required.") from None
            self._writer_proc = None
        return code

    async def _stop_process(self, proc):
        try:
            if getattr(proc, "returncode", None) is None:
                proc.kill()
        except (ProcessLookupError, OSError):
            pass
        return await self._wait_process(proc)

    async def _discard_stream(self, stream):
        if stream is None:
            return
        try:
            read = getattr(stream, "read", None)
            while (await read(65536) if read is not None else await stream.readline()):
                pass
        except (OSError, ValueError):
            pass

    async def _stop_unpublished(self, proc):
        # A child can appear after close/cancellation, before readers are started.
        # Drain its pipes concurrently with wait, or a full pipe can block reap.
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        drains = [asyncio.create_task(self._discard_stream(getattr(proc, name, None)))
                  for name in ("stdout", "stderr")]
        try:
            return await self._wait_process(proc)
        finally:
            await asyncio.gather(*drains, return_exceptions=True)

    async def _abort_launch(self, task):
        # This independently retained task, NOT the cancelling caller, owns
        # cleanup. Repeated cancellation cannot drop the eventual child handle.
        try:
            try:
                proc = await task
            except OSError:
                if self._writer_lease is not None:
                    self._writer_lease.process_reaped()  # definite spawn failure
                return
            self._writer_proc = proc
            # A wait OSError is NOT a spawn failure; leave the journal active.
            await self._stop_unpublished(proc)
        finally:
            if self._launch_task is task:
                self._launch_task = None
            self._release_writer()

    def prepare_context(self):
        """Acquire native ownership and validate context without spawning a CLI.

        Retain a successful lease through the lazy wait for input. On failure,
        release only idle ownership: a live/in-flight child still owns its guard.
        This hook is deliberately re-run before every process launch.
        """
        try:
            if self._killed:
                raise RuntimeError("Session is closed")
            if self._writer_lease is None and self._writer_lease_factory is not None:
                lease = self._writer_lease_factory()
                lease.acquire()
                self._writer_lease = lease
            if self._context_preparing is not None:
                self._context_preparing()
        except BaseException:
            if self._writer_proc is None and self._launch_task is None:
                self._release_writer()
            raise

    async def _launch_process(self, options, paths=(), prompt=None):
        try:
            self.prepare_context()
            argv = self._command(options, paths)
            if self._per_turn:
                argv += self._prompt_argv + [prompt]
        except BaseException:
            self._release_writer()
            raise
        if self._writer_lease is not None:
            try:
                self._writer_lease.begin_process()
            except Exception:
                self._release_writer()
                raise ValueError("Could not secure native writer ownership. No process was started.") from None
        task = self._launch_task = asyncio.create_task(self._spawn_process(argv, prompt))
        aborting = False
        try:
            proc = await asyncio.shield(task)
        except asyncio.CancelledError:
            aborting = True
            self._alive = False
            self._killed = True
            self._turn_queue.clear()
            cleanup = asyncio.create_task(self._abort_launch(task))
            self._launch_cleanups.add(cleanup)
            # Keep even a completed cleanup until wait_closed retrieves errors.
            try:
                await asyncio.shield(cleanup)
            except Exception:
                pass  # active journal is retained; cancellation still propagates
            raise
        except OSError:
            if self._writer_lease is not None:
                self._writer_lease.process_reaped()  # no child handle was created
            self._release_writer()
            raise
        except BaseException:
            self._release_writer()  # uncertain launch -> keep journal active
            raise
        finally:
            if not aborting and self._launch_task is task:
                self._launch_task = None
        self._writer_proc = proc
        return proc

    async def start(self):
        if self._killed:
            raise RuntimeError("Session is closed")
        self._alive = True
        if self._per_turn or self._lazy_start:
            # Logically live while waiting for the first full user turn.
            self._proc = None
            await self._emit(_event(EV_STATUS, subtype="ready"))
            return
        try:
            # Persistent runtimes must also go through the command builder so
            # session-continuity flags apply; ``cmd`` is only the base command.
            proc = await self._launch_process({})
        except BaseException:
            self._alive = False
            raise
        if not self._alive:
            await self._stop_unpublished(proc)
            return
        self._proc = proc
        self._start_readers(proc)

    def _start_readers(self, proc):
        self._reader_tasks.append(asyncio.create_task(self._read_stdout(proc)))
        if getattr(proc, "stderr", None) is not None:
            self._reader_tasks.append(asyncio.create_task(self._read_stderr(proc)))

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
        temp = tempfile.TemporaryDirectory(prefix="agentbridge-attachments-")
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
            async with self._spawn_lock:
                if not self._alive:
                    return
                proc = await self._launch_process(options, paths, prompt)
                # kill() can run while process creation is awaiting. Never publish a
                # late process into session state: terminate and reap it here.
                if not self._alive:
                    await self._stop_unpublished(proc)
                    return
                self._proc = proc
                if getattr(proc, "stderr", None) is not None:
                    stderr_task = asyncio.create_task(self._read_stderr(proc))
                try:
                    await self._drain_turn(proc)
                finally:
                    # If output handling failed, stop the child before waiting for
                    # stderr; otherwise a full pipe can keep cleanup and exit stuck.
                    if stderr_task is not None:
                        if self._last_reaped is None or self._last_reaped[0] is not proc:
                            stderr_task.cancel()
                        await asyncio.gather(stderr_task, return_exceptions=True)
                    if self._last_reaped is None or self._last_reaped[0] is not proc:
                        await self._stop_unpublished(proc)
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
        code = await self._wait_process(proc)
        # Some CLIs communicate completion only by exiting. Do not add a second
        # turn boundary when their structured transcript already supplied one.
        if not self._turn_end_seen:
            self._turn_end_seen = True
            await self._emit(_event(
                EV_TURN_END, subtype="process_exit", is_error=bool(code),
                result=None))

    async def _read_stdout(self, proc):
        stream = proc.stdout
        while self._proc is proc:
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
        try:
            code = await self._wait_process(proc)
        finally:
            self._release_writer()
        self._fail_pending_controls("Agent exited before applying runtime control")
        await self._report_exit(code)

    async def _report_exit(self, code):
        if not self._exit_reported:
            self._exit_reported = True
            await self.on_exit(int(code or 0))

    def _fail_pending_controls(self, message: str) -> None:
        pending = list(self._pending_controls.values())
        self._pending_controls.clear()
        for future in pending:
            if not future.done():
                future.set_exception(ValueError(message))

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

        if not isinstance(obj, dict):
            await self._emit(_event(EV_ERROR, message="Agent emitted invalid JSON"))
            return
        native_type = obj.get("type")
        if native_type == "control_response":
            response = obj.get("response")
            if isinstance(response, dict):
                request_id = response.get("request_id")
                future = (self._pending_controls.pop(request_id, None)
                          if isinstance(request_id, str) else None)
                if future is not None and not future.done():
                    future.set_result(response)
            return
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
                # A successful turn confirms establishment, but failed/uncertain
                # launches already have a durable reservation and only resume.
                # Provider IDs are not an equality contract.
                if (self._context_started is not None
                        and not self._context_ready_reported
                        and not event.get("is_error")):
                    try:
                        await self._context_started()
                    except Exception:
                        _LOG.error("Could not record the local agent context marker")
                        await self._emit(_event(
                            EV_ERROR, message="Conversation state could not be saved. "
                            "This session has stopped; fix local storage before resuming.",
                            code="context_persist_failed"))
                        await self._emit(dict(event, is_error=True, subtype="context_persist_failed"))
                        self.kill()
                        return
                    self._context_ready_reported = True
            await self._emit(event)

    async def _emit(self, ev: dict):
        await self.on_output(json.dumps(ev))

    async def _apply_live_controls(self, options: dict[str, object]):
        if self._live_control_builder is None or self._active_options is None:
            return
        requests = self._live_control_builder(self._active_options, options)
        for request in requests:
            if not isinstance(request, dict) or not request.get("subtype"):
                raise ValueError("invalid live control request")
            self._control_counter += 1
            request_id = f"deepbox_{self._control_counter}"
            future = asyncio.get_running_loop().create_future()
            self._pending_controls[request_id] = future
            packet = {
                "type": "control_request",
                "request_id": request_id,
                "request": request,
            }
            try:
                self._proc.stdin.write((json.dumps(packet) + "\n").encode())
                await self._proc.stdin.drain()
                response = await asyncio.wait_for(future, self._control_timeout)
            except asyncio.TimeoutError as exc:
                raise ValueError(
                    f"Agent did not confirm {request['subtype']}") from exc
            finally:
                self._pending_controls.pop(request_id, None)
            if response.get("subtype") != "success":
                detail = response.get("error") or response.get("message") or "rejected"
                raise ValueError(
                    f"Agent rejected {request['subtype']}: {detail}")

    async def _dispatch_turn(self, data: str, raw_options: object):
        if not self._alive:
            return
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
        await self._emit(_event(
            EV_USER_ECHO, text=data,
            attachments=self._attachment_metadata(options)))
        try:
            if not self._alive:
                return
            attachments = self._decode_attachments(options)
            if self._per_turn:
                await self._emit(_event(
                    EV_SESSION_CONFIG, options=public_options))
                await self._run_one_turn(data, options, attachments)
                return
            async with self._write_lock:
                if not self._alive:
                    return
                prompt = self._embed_text_attachments(data, attachments)
                spawned = self._proc is None
                if spawned:
                    proc = await self._launch_process(options)
                    if not self._alive:
                        await self._stop_unpublished(proc)
                        return
                    self._proc = proc
                    self._start_readers(proc)
                stdin = self._proc.stdin
                if stdin is None:
                    raise ValueError("Agent stdin is unavailable")
                if not spawned:
                    await self._apply_live_controls(public_options)
                if not self._alive:
                    return
                self._active_options = dict(public_options)
                await self._emit(_event(
                    EV_SESSION_CONFIG, options=public_options))
                if not self._alive:
                    return
                stdin.write(encode_user_message(prompt).encode())
                drain = getattr(stdin, "drain", None)
                if drain is not None:
                    await drain()
        except ValueError as exc:
            if not self._alive:
                return
            await self._emit(_event(
                EV_ERROR, message=str(exc), code=getattr(exc, "code", "input_error")))
            await self._emit(_event(EV_TURN_END, subtype="input_error", is_error=True))
        except OSError as exc:
            if not self._alive:
                return
            code = getattr(exc, "winerror", None) or exc.errno
            _LOG.warning(
                "Agent process could not start (error code %s)",
                code if code is not None else "unknown",
            )
            await self._emit(_event(
                EV_ERROR, message=_process_start_error(exc),
            ))
            await self._emit(_event(
                EV_TURN_END, subtype="process_error", is_error=True,
            ))

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

    def can_accept_turn(self) -> bool:
        return self.is_alive() and len(self._turn_queue) < MAX_QUEUED_TURNS

    def write_turn(self, data: str, options: object = None):
        """Queue a complete turn, returning False on rejection.

        One bounded FIFO worker serializes inputs/controls. Per-turn processes
        finish before the next queued turn starts. This local queue is not
        durable; explicit close discards queued messages.
        """
        if not self.is_alive() or not isinstance(data, str):
            return False
        if len(self._turn_queue) >= MAX_QUEUED_TURNS:
            if self._queue_notice_task is None or self._queue_notice_task.done():
                self._queue_notice_task = asyncio.create_task(self._queue_full())
            return False
        self._turn_queue.append((data, options))
        if self._turn_task is None or self._turn_task.done():
            self._turn_pending = True
            self._turn_task = asyncio.create_task(self._drain_turns())
        return True

    async def _queue_full(self):
        if self._alive:
            await self._emit(_event(
                EV_ERROR, code="input.queue_full",
                message="Too many queued messages. Wait for pending turns to finish, then resend this message."))

    async def _drain_turns(self):
        try:
            while self._alive and self._turn_queue:
                data, options = self._turn_queue.popleft()
                try:
                    await self._dispatch_turn(data, options)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if self._alive:
                        await self._emit(_event(
                            EV_ERROR, code="runtime.unavailable",
                            message="The runtime could not process this message. Check the local runtime and retry."))
                        await self._emit(_event(
                            EV_TURN_END, subtype="process_error", is_error=True))
        finally:
            self._turn_queue.clear()
            self._turn_pending = False
            self._turn_task = None

    def write(self, data: str):
        return self.write_turn(data, {})

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

    async def _finish_close(self):
        try:
            if self._turn_task is not None:
                await asyncio.gather(self._turn_task, return_exceptions=True)
            if self._launch_cleanups:
                await asyncio.gather(*self._launch_cleanups, return_exceptions=True)
                self._launch_cleanups.clear()
            if self._launch_task is not None:
                results = await asyncio.gather(self._launch_task, return_exceptions=True)
                if results and not isinstance(results[0], BaseException):
                    self._writer_proc = results[0]
                    await self._stop_unpublished(results[0])
            if self._reader_tasks:
                await asyncio.gather(*self._reader_tasks, return_exceptions=True)
            if self._proc is not None:
                if self._last_reaped is None or self._last_reaped[0] is not self._proc:
                    await self._stop_unpublished(self._proc)
                if not self._per_turn and self._last_reaped is not None:
                    await self._report_exit(self._last_reaped[1])
        except Exception:
            _LOG.error("Native writer could not be reaped; local recovery may be required")
        finally:
            self._release_writer()

    async def wait_closed(self):
        """Wait for owned children and writer ownership to finish closing."""
        if self._close_task is not None:
            await asyncio.shield(self._close_task)

    def kill(self):
        self._killed = True
        self._alive = False
        self._turn_queue.clear()
        if self._queue_notice_task is not None:
            self._queue_notice_task.cancel()
        for future in self._pending_controls.values():
            if not future.done():
                future.cancel()
        self._pending_controls.clear()
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
        current = asyncio.current_task()
        for task in self._reader_tasks:
            if task is not current and not task.done():
                task.cancel()
        # A spawned per-turn child may have blocked in an output callback. Its
        # finally path owns drain/reap. Pending launches are left to settle.
        if (self._per_turn and self._proc is not None and self._turn_task is not None
                and self._turn_task is not current and not self._turn_task.done()):
            self._turn_task.cancel()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._finish_close())
