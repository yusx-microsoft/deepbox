"""One spawn-isolated embedded-library worker per locally bound Agent.

Wire traffic uses ONLY bounded JSON in Pipe.send_bytes/recv_bytes. The trusted
multiprocessing bootstrap passes a private pipe handle, never browser objects.
stdout/stderr are not a protocol and are discarded in the child. No DeepOrca
imports or process-global profile changes occur in the Connector process.

Test seam: transport_factory(binding, *, provision_only=False) returns an
object with async send(dict), receive(), close(force=False), and is_alive().
Factories may themselves be async. worker_factory is an alias for this seam.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import importlib
import inspect
import json
import multiprocessing
import os
from pathlib import Path
import re
import sys
import uuid

from .events import (IntegrationError, MAX_TURN_BYTES, json_bytes,
                     prepare_native_event, validate_id, validate_input,
                     validate_native_id)

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 256 * 1024
_COALESCE_BYTES = 16 * 1024
_COALESCE_DELAY = 0.012
INTERRUPT_TIMEOUT = 10.0
STARTUP_TIMEOUT = 30.0
_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}\Z")
_SAFE_CODES = frozenset({"sdk_missing", "sdk_incompatible", "configuration_required",
    "credential_unavailable", "configuration_api_unavailable", "configuration_busy",
    "existing_profile_api_unavailable", "existing_profile_unavailable",
    "profile_unavailable", "startup_failed", "runtime_error", "worker_lost",
    "invalid_protocol", "output_limit", "approval_contract_violation", "missing_tool_id",
    "interrupt_timeout", "output_unavailable", "worker_closed", "native_context_unavailable"})


def _safe_code(value, default="runtime_error"):
    return value if isinstance(value, str) and value in _SAFE_CODES else default


def encode_frame(value: dict) -> bytes:
    if (not isinstance(value, dict) or type(value.get("v")) is not int
            or value["v"] != PROTOCOL_VERSION):
        raise IntegrationError("invalid_protocol")
    data = json_bytes(value)
    if len(data) > MAX_FRAME_BYTES:
        raise IntegrationError("output_limit")
    return data


def decode_frame(data: bytes) -> dict:
    if not isinstance(data, bytes) or len(data) > MAX_FRAME_BYTES:
        raise IntegrationError("output_limit")
    def no_constant(_):
        raise ValueError()
    def unique_pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        value = json.loads(data.decode("utf-8"), parse_constant=no_constant,
                           object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise IntegrationError("invalid_protocol") from None
    if not isinstance(value, dict) or type(value.get("v")) is not int or value["v"] != 1:
        raise IntegrationError("invalid_protocol")
    return value


def _binding(value):
    if not isinstance(value, dict):
        raise IntegrationError("invalid_binding")
    required = {"agent_id", "profile_name", "home", "workspace"}
    if not required <= value.keys() or value.keys() - required - {
            "source_path", "template_dir", "config", "credential_key_path"}:
        raise IntegrationError("invalid_binding")
    result = dict(value)
    validate_id(result["agent_id"], "invalid_binding")
    for key in ("home", "workspace", "source_path", "template_dir", "credential_key_path"):
        path = result.get(key)
        if key in ("source_path", "template_dir", "credential_key_path") and path is None:
            result.pop(key, None)
            continue
        if not isinstance(path, str) or not path or "\x00" in path or not Path(path).is_absolute():
            raise IntegrationError("invalid_binding")
    if "config" in result:
        from agentbridge.integrations.deeporca.contract import validate_runtime_config
        try:
            result["config"] = validate_runtime_config(result["config"])
        except ValueError:
            raise IntegrationError("invalid_binding") from None
    name = result["profile_name"]
    if result.get("config", {}).get("profile", {}).get("mode") == "bind":
        from .profiles import safe_existing_name
        valid_name = safe_existing_name(name)
    else:
        valid_name = isinstance(name, str) and _PROFILE.fullmatch(name) and ".." not in name
    if not valid_name:
        raise IntegrationError("invalid_binding")
    return result


class _NativeEventBatcher:
    """One owned deadline task and one bounded pending native delta per turn.

    The SDK's text/thinking wire events are incremental. Keep the LAST native
    sequence of a batch (gaps are legal), never invent IDs or renumber events.
    Unknown fields/types are boundaries, not candidates for lossy projection.
    ``failed`` is supervised by the turn runner, even if the SDK swallows a
    callback exception or never calls the sink again.
    """
    def __init__(self, emit, *, delay=_COALESCE_DELAY, limit=_COALESCE_BYTES, sleep=None):
        self._emit = emit
        self._delay, self._limit = delay, limit
        self._sleep = sleep or asyncio.sleep
        self._pending = None
        self._key = None
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._closed = False
        self._bytes = 0
        self._last_turn = None
        self._last_sequence = -1
        self.failed = asyncio.get_running_loop().create_future()
        self._timer = asyncio.create_task(self._deadline())

    def _check(self):
        if self.failed.done():
            raise self.failed.result()
        if self._closed:
            raise IntegrationError("worker_closed")

    def _fail(self, exc):
        if not self.failed.done():
            self.failed.set_result(exc)

    @staticmethod
    def _merge_key(event):
        if event["type"] not in ("text", "thinking", "text.delta", "thinking.delta"):
            return None
        field = "content" if "content" in event else "text"
        if not isinstance(event.get(field), str):
            raise IntegrationError("invalid_protocol")
        identities = ("type", "turn_id", "message_id", "parent_id", "parent_tool_id")
        # In particular, never consume flags such as error/approval/status or
        # a second content field while merging ostensibly textual events.
        if event.keys() - {*identities, "sequence", field}:
            return None
        for name in identities[2:]:
            if name in event:
                validate_native_id(event[name])
        return field, tuple((name, event.get(name), name in event) for name in identities)

    async def _flush(self):
        if self._pending is not None:
            event, self._pending = self._pending, None
            self._key = None
            # Remove before awaiting: a failed/uncertain send must not retry.
            await self._emit(event)

    async def add(self, raw):
        try:
            await self._add_ordered(raw)
        except asyncio.CancelledError:
            # Waiting for the timer's send lock is also an undelivered event.
            # The SDK may catch cancellation and return a saved result, so the
            # owner must observe failure independently of that SDK result.
            self._fail(IntegrationError("output_unavailable"))
            raise

    async def _add_ordered(self, raw):
        async with self._lock:
            self._check()
            try:
                event = prepare_native_event(raw)
                if (event["turn_id"] == self._last_turn
                        and event["sequence"] <= self._last_sequence):
                    raise IntegrationError("invalid_protocol")
                self._last_turn, self._last_sequence = event["turn_id"], event["sequence"]
                size = len(json_bytes(event))
                # Account ORIGINAL validated events, not the smaller batches.
                self._bytes += size
                if self._bytes > MAX_TURN_BYTES:
                    raise IntegrationError("output_limit")
                key = self._merge_key(event)
                if key is not None and key == self._key:
                    field = key[0]
                    merged = dict(event, **{field: self._pending[field] + event[field]})
                    if len(json_bytes(merged)) <= self._limit:
                        self._pending = merged
                        return
                await self._flush()
                if key is None or size > self._limit:
                    # Already bounded by prepare_native_event. Do not split a
                    # native event into several copies of its sequence/ID.
                    await self._emit(event)
                else:
                    self._pending, self._key = event, key
                    self._wake.set()
            except Exception as exc:
                try:
                    await self._flush()  # valid prefix BEFORE a fault
                finally:
                    self._fail(exc)
                raise

    async def _deadline(self):
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                # First arrival starts the window; subsequent deltas never
                # postpone it. An intervening boundary may only flush earlier.
                await self._sleep(self._delay)
                async with self._lock:
                    self._check()
                    await self._flush()
        except asyncio.CancelledError:
            if not self._closed:
                self._fail(IntegrationError("output_unavailable"))
            raise
        except Exception as exc:
            self._fail(exc)  # awaited by owner, not a detached task exception

    async def close(self):
        try:
            async with self._lock:
                if not self._closed:
                    self._closed = True
                    if self.failed.done():
                        self._pending = None
                        raise self.failed.result()
                    try:
                        await self._flush()
                    except Exception as exc:
                        self._fail(exc)
                        raise
        finally:
            self._timer.cancel()
            await asyncio.gather(self._timer, return_exceptions=True)


class SpawnTransport:
    def __init__(self, binding, *, provision_only=False):
        self.binding = binding
        self.provision_only = provision_only
        ctx = multiprocessing.get_context("spawn")
        self._conn, child = ctx.Pipe(duplex=True)
        self._process = ctx.Process(target=_worker_main, args=(child,),
                                    name="deeporca-library-worker")
        self._closed = False
        self._send_lock = asyncio.Lock()
        try:
            self._process.start()
        except BaseException:
            self._conn.close()
            raise
        finally:
            child.close()

    async def send(self, frame):
        data = encode_frame(frame)
        async with self._send_lock:
            await asyncio.to_thread(self._conn.send_bytes, data)

    async def receive(self):
        # Short polls avoid leaving an uninterruptible executor thread behind
        # when a coroutine is cancelled, on Windows as well as POSIX.
        while not self._closed:
            if await asyncio.to_thread(self._conn.poll, 0.05):
                return decode_frame(await asyncio.to_thread(self._conn.recv_bytes, MAX_FRAME_BYTES))
            if not self.is_alive():
                # The child can write its final provision reply and exit
                # between poll() and is_alive(). Drain that last frame first.
                if self._conn.poll(0):
                    return decode_frame(await asyncio.to_thread(self._conn.recv_bytes, MAX_FRAME_BYTES))
                raise EOFError()
        raise EOFError()

    def is_alive(self):
        return not self._closed and self._process.is_alive()

    def has_exited(self):
        # Unlike is_alive(), this checks OS termination even after IPC closes.
        try:
            return not self._process.is_alive()
        except ValueError:  # multiprocessing handle already joined and closed
            return True

    async def close(self, force=False):
        if self._closed:
            return
        self._closed = True
        if force and self._process.is_alive():
            self._process.terminate()
        await asyncio.to_thread(self._process.join, 0.3 if force else 1.0)
        if self._process.is_alive():
            self._process.terminate()
            await asyncio.to_thread(self._process.join, 0.5)
        if self._process.is_alive():
            self._process.kill()
            await asyncio.to_thread(self._process.join, 0.5)
        self._conn.close()
        if not self._process.is_alive():
            self._process.close()


@dataclass
class Reservation:
    token: str
    session_id: str
    message_id: str
    epoch: int
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    dispatched: bool = False
    interrupt_requested: bool = False
    forced_status: str | None = None
    turn_id: str | None = None
    last_sequence: int = -1


class DeepOrcaWorker:
    def __init__(self, binding: dict, *, transport_factory=None, worker_factory=None,
                 interrupt_timeout=INTERRUPT_TIMEOUT, startup_timeout=STARTUP_TIMEOUT):
        self.binding = _binding(binding)
        if transport_factory is not None and worker_factory is not None:
            raise TypeError("Choose one transport factory")
        self._factory = transport_factory or worker_factory or SpawnTransport
        self.interrupt_timeout = min(max(float(interrupt_timeout), 0.01), INTERRUPT_TIMEOUT)
        self.startup_timeout = max(float(startup_timeout), 0.01)
        self._transport = None
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._active: Reservation | None = None
        self._epoch = 0
        self._ready = False
        self._retired = False
        self._closing = False
        self._startup_info = None
        self.last_error = None

    async def _connect(self, provision_only=False):
        transport = self._factory(self.binding, provision_only=provision_only)
        if inspect.isawaitable(transport):
            transport = await transport
        self._transport = transport
        await transport.send({"v": 1, "op": "init", "binding": self.binding,
                              "provision_only": provision_only})
        frame = decode_frame(encode_frame(await transport.receive()))
        if frame.get("kind") == "fault":
            raise IntegrationError(_safe_code(frame.get("code"), "startup_failed"))
        if frame.get("kind") != "ready" or not isinstance(frame.get("info"), dict):
            raise IntegrationError("invalid_protocol")
        return frame["info"]

    async def start(self):
        async with self._start_lock:
            if self._ready and self.is_alive():
                return self._startup_info
            if self._ready:
                # A crashed worker cannot be silently resurrected in the same
                # epoch. The Supervisor must explicitly replace it.
                await self._retire("worker_lost")
            if self._retired or self._closing:
                raise IntegrationError(self.last_error or "worker_closed")
            try:
                self._startup_info = await asyncio.wait_for(self._connect(), self.startup_timeout)
                self._ready = True
                return self._startup_info
            except BaseException as exc:
                code = _safe_code(getattr(exc, "code", None), "startup_failed")
                await self._retire(code)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise IntegrationError(code) from None

    async def provision(self):
        """One-shot local profile/config probe. Never constructs a runtime.

        Use a separate Worker instance for this reconciler operation; it is
        closed on return, including all failure paths. No model calls occur.
        """
        async with self._start_lock:
            if self._transport is not None or self._retired:
                raise IntegrationError("worker_closed")
            try:
                return await asyncio.wait_for(self._connect(True), self.startup_timeout)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                raise IntegrationError(_safe_code(getattr(exc, "code", None), "startup_failed")) from None
            finally:
                await self._retire("worker_closed", force=False)

    probe = provision

    def is_alive(self):
        return bool(self._ready and not self._retired and self._transport
                    and self._transport.is_alive())

    def can_accept_turn(self):
        return self.is_alive() and not self._closing and self._active is None

    def reserve(self, session_id, message_id):
        """Synchronous reservation: no await between validation and ownership."""
        validate_id(session_id, "invalid_session_id")
        validate_id(message_id)
        if self._active is not None:
            raise IntegrationError("runtime_busy")
        if not self.can_accept_turn():
            raise IntegrationError("runtime_unavailable")
        self._active = Reservation(uuid.uuid4().hex, session_id, message_id, self._epoch)
        return self._active

    def release(self, reservation):
        if self._active is reservation and reservation.task is None:
            self._active = None
            reservation.done.set()

    def is_current(self, reservation):
        return self._active is reservation and reservation.epoch == self._epoch and not self._retired

    def dispatch(self, reservation, text, model, *, on_begin, on_event, on_settled):
        if not self.is_current(reservation) or reservation.task is not None:
            raise IntegrationError("runtime_unavailable")
        reservation.task = asyncio.create_task(self._drive(
            reservation, text, model, on_begin, on_event, on_settled))

    async def _drive(self, active, text, model, on_begin, on_event, on_settled):
        status, code = "uncertain", None
        try:
            await on_begin()
            if active.interrupt_requested:
                status = "cancelled"  # definitely not dispatched
            else:
                active.dispatched = True
                await self._transport.send({"v": 1, "op": "run", "token": active.token,
                    "session_id": active.session_id, "message_id": active.message_id,
                    "text": text, "model": model})
                output_bytes = 0
                stale_frames = 0
                while self.is_current(active):
                    frame = decode_frame(encode_frame(await self._transport.receive()))
                    if frame.get("kind") == "fault":
                        raise IntegrationError(_safe_code(frame.get("code")))
                    if frame.get("token") != active.token:
                        # Epoch/token fencing: never deliver a previous turn's
                        # delayed output into the currently active session.
                        stale_frames += 1
                        if stale_frames > 128:
                            raise IntegrationError("invalid_protocol")
                        continue
                    if frame.get("kind") == "event":
                        event = prepare_native_event(frame.get("event"))
                        output_bytes += len(json_bytes(event))
                        if output_bytes > MAX_TURN_BYTES:
                            raise IntegrationError("output_limit")
                        if active.turn_id is not None and event["turn_id"] != active.turn_id:
                            raise IntegrationError("invalid_protocol")
                        if event["sequence"] <= active.last_sequence:
                            raise IntegrationError("invalid_protocol")
                        active.turn_id = event["turn_id"]
                        active.last_sequence = event["sequence"]
                        await on_event(event)
                    elif frame.get("kind") == "result":
                        result = frame.get("result")
                        if not isinstance(result, dict) or result.get("status") not in (
                                "completed", "cancelled", "uncertain", "error"):
                            raise IntegrationError("invalid_protocol")
                        turn_id = validate_native_id(result.get("turn_id"))
                        if active.turn_id is not None and active.turn_id != turn_id:
                            raise IntegrationError("invalid_protocol")
                        active.turn_id = turn_id
                        # Context loss is always an ERROR, never a completion,
                        # even if an SDK supplies a contradictory status.
                        status = ("error" if result.get("code") == "native_context_unavailable"
                                  else result["status"])
                        code = _safe_code(result.get("code")) if status == "error" else None
                        if status == "uncertain":
                            code = "runtime_error"
                            await self._retire(code)
                        break
                    else:
                        raise IntegrationError("invalid_protocol")
        except asyncio.CancelledError:
            status, code = active.forced_status or "uncertain", self.last_error or "worker_closed"
        except IntegrationError as exc:
            code = _safe_code(exc.code)
            status = "error" if code == "approval_contract_violation" else "uncertain"
            await self._retire(code)
        except (EOFError, OSError):
            code, status = "worker_lost", "uncertain"
            await self._retire(code)
        except Exception:
            code, status = "output_unavailable", "uncertain"
            await self._retire(code)
        finally:
            status = active.forced_status or status
            try:
                # on_settled must write the terminal event durably BEFORE it
                # updates the parent's execution journal. Slot held throughout.
                await on_settled(status, active.turn_id, code)
            except (Exception, asyncio.CancelledError):
                # No false settled receipt when the durable callback failed.
                await self._retire("output_unavailable")
            finally:
                if self._active is active:
                    self._active = None
                active.done.set()

    async def _retire(self, code, force=True):
        if not self._retired:
            self._epoch += 1
        self._ready = False
        self._retired = True
        self.last_error = _safe_code(code)
        if self._transport is not None:
            await self._transport.close(force=force)

    async def interrupt(self, session_id=None):
        active = self._active
        if active is None or (session_id is not None and active.session_id != session_id):
            return
        send_interrupt = not active.interrupt_requested
        active.interrupt_requested = True
        async def request_and_wait():
            if send_interrupt and active.dispatched and self.is_current(active):
                await self._transport.send({"v": 1, "op": "interrupt", "token": active.token})
            await active.done.wait()
        try:
            await asyncio.wait_for(request_and_wait(), self.interrupt_timeout)
        except (asyncio.TimeoutError, EOFError, OSError):
            active.forced_status = "uncertain"
            self._epoch += 1  # retire output immediately, before any await
            self._ready = False
            self.last_error = "interrupt_timeout"
            if active.task is not None:
                active.task.cancel()
            await self._retire("interrupt_timeout")
            # Do not wait indefinitely for a broken durable-output callback.
            if active.task is not None:
                await asyncio.wait({active.task}, timeout=0.5)

    async def close(self):
        if self._active is not None and self._active.task is asyncio.current_task():
            # on_exit/on_turn_settled may themselves retire the worker. Check
            # before taking the lock: another closer may be awaiting us.
            await self._retire("worker_closed")
            return
        async with self._close_lock:
            if self._retired:
                return
            self._closing = True
            await self.interrupt()
            if self._retired:
                return
            try:
                if self._transport:
                    await asyncio.wait_for(self._transport.send({"v": 1, "op": "close"}), 0.5)
            except (Exception, asyncio.CancelledError):
                pass
            await self._retire("worker_closed", force=False)

    def is_retired(self):
        if self._transport is None:
            return True
        if hasattr(self._transport, "has_exited"):
            return self._transport.has_exited()
        return not self._transport.is_alive()


async def _child_loop(conn):
    send_lock = asyncio.Lock()
    async def send(frame):
        data = encode_frame(dict(frame, v=1))
        async with send_lock:
            await asyncio.to_thread(conn.send_bytes, data)
    async def receive():
        while not await asyncio.to_thread(conn.poll, 0.05):
            pass
        return decode_frame(await asyncio.to_thread(conn.recv_bytes, MAX_FRAME_BYTES))

    runtime = None
    runner = None
    receiver = None
    interrupt_tasks = set()
    try:
        initial = await receive()
        if initial.get("op") != "init":
            raise IntegrationError("invalid_protocol")
        binding = _binding(initial.get("binding"))
        # Explicit, child-local configuration BEFORE importing the SDK.
        os.environ["DEEPORCA_HOME"] = binding["home"]
        os.environ["DEEPORCA_AGENT_NAME"] = binding["profile_name"]
        os.environ["DEEPORCA_WORKSPACE"] = binding["workspace"]
        os.chdir(binding["workspace"])
        sys.path[:] = [p for p in sys.path if p and Path(p).is_absolute()
                       and Path(p) != Path(binding["workspace"])]
        if binding.get("source_path"):
            sys.path.insert(0, binding["source_path"])
        try:
            sdk = importlib.import_module("deeporca.embedded")
        except ImportError:
            raise IntegrationError("sdk_missing") from None
        if type(getattr(sdk, "EMBEDDED_API_VERSION", None)) is not int or sdk.EMBEDDED_API_VERSION != 1:
            raise IntegrationError("sdk_incompatible")
        config = binding.get("config", {})
        is_existing = config.get("profile", {}).get("mode") == "bind"
        has_configuration = "llm" in config or "credential" in config
        if has_configuration and (type(getattr(sdk, "PROFILE_CONFIGURATION_API_VERSION", None)) is not int
                                  or sdk.PROFILE_CONFIGURATION_API_VERSION != 1):
            raise IntegrationError("configuration_api_unavailable")
        if is_existing:
            from .profiles import existing_profile_api
            if not existing_profile_api(sdk):
                raise IntegrationError("existing_profile_api_unavailable")
            # Native owns acquisition and validates busy/invalid local state.
            # Never run ensure_profile or pass configuration for a binding.
            try:
                runtime = sdk.EmbeddedRuntime(binding["profile_name"], binding["workspace"],
                                              home=binding["home"], profile_mode="existing")
                readiness = await runtime.start()
                if isinstance(readiness, dict) and readiness.get("configured") is False:
                    raise IntegrationError("existing_profile_unavailable")
            except Exception:
                raise IntegrationError("existing_profile_unavailable") from None
            info = {"profile": binding["profile_name"], "created": False, "configured": True,
                    "embedded_api_version": 1, "status": "ready"}
        else:
            # Neither the Connector parent nor the server see the plaintext.
            from .credentials import runtime_configuration
            configuration = runtime_configuration(config,
                private_key_path=binding.get("credential_key_path"))
            profile = sdk.ensure_profile(binding["profile_name"], home=binding["home"],
                                         template_dir=binding.get("template_dir"))
            if not isinstance(profile, dict) or any(type(profile.get(k)) is not bool
                                                   for k in ("created", "configured")):
                raise IntegrationError("invalid_protocol")
            info = {"profile": binding["profile_name"], "created": profile["created"],
                    "configured": profile["configured"], "embedded_api_version": 1,
                    "status": "ready" if profile["configured"] else "configuration_required"}
            if initial.get("provision_only") and not has_configuration:
                await send({"kind": "ready", "info": info})
                return
            if not profile["configured"] and not has_configuration:
                raise IntegrationError("configuration_required")
            runtime = sdk.EmbeddedRuntime(binding["profile_name"], binding["workspace"], home=binding["home"])
            if has_configuration:
                readiness = await runtime.start(configuration=configuration)
                if isinstance(readiness, dict) and readiness.get("configured") is False:
                    raise IntegrationError("configuration_required")
                info.update(configured=True, status="ready")
                configuration = None
            else:
                await runtime.start()
        await send({"kind": "ready", "info": info})
        if initial.get("provision_only"):
            return

        executing = False

        async def run(command):
            nonlocal executing
            token = command["token"]
            turn_id = None
            async def emit(event):
                await send({"kind": "event", "token": token, "event": event})
            batcher = _NativeEventBatcher(emit)
            async def on_event(event):
                nonlocal turn_id
                await batcher.add(event)
                turn_id = event["turn_id"]
            sdk_task = asyncio.create_task(runtime.run_turn(
                command["session_id"], command["text"], message_id=command["message_id"],
                model=command.get("model"), on_event=on_event))
            try:
                await asyncio.wait({sdk_task, batcher.failed}, return_when=asyncio.FIRST_COMPLETED)
                if batcher.failed.done():
                    raise batcher.failed.result()
                result = await sdk_task
                terminal = {"kind": "result", "token": token, "result": result}
            except asyncio.CancelledError:
                # An escaping cancellation is not affirmative proof of a save.
                terminal = {"kind": "result", "token": token,
                            "result": {"status": "uncertain", "turn_id": turn_id or token}}
            except BaseException as exc:
                if getattr(exc, "code", None) == "native_context_unavailable":
                    # SDK v1 may expose this as an exception or additive error
                    # result. Never serialize details or kill other sessions.
                    terminal = {"kind": "result", "token": token,
                                "result": {"status": "error", "turn_id": turn_id or token,
                                           "code": "native_context_unavailable"}}
                else:
                    terminal = {"kind": "fault", "code": _safe_code(getattr(exc, "code", None))}
            try:
                try:
                    # Join the timer and flush before ANY terminal receipt.
                    await batcher.close()
                except Exception as exc:
                    terminal = {"kind": "fault", "code": _safe_code(getattr(exc, "code", None))}
                # Publish failure before awaiting a possibly broken SDK's
                # cancellation; the parent owns the hard process deadline.
                if sdk_task.done():
                    executing = False
                await send(terminal)
            finally:
                if not sdk_task.done():
                    sdk_task.cancel()
                await asyncio.gather(sdk_task, return_exceptions=True)
                executing = False

        active_token = None
        while True:
            receiver = asyncio.create_task(receive())
            if runner is not None:
                await asyncio.wait({receiver, runner}, return_when=asyncio.FIRST_COMPLETED)
                if runner.done():
                    await runner  # propagate pipe/callback failure; never detach
                    runner = None
            command = await receiver
            receiver = None
            op = command.get("op")
            if op == "run":
                if executing:
                    raise IntegrationError("invalid_protocol")
                if runner is not None:
                    await runner  # finish the prior send/cleanup before reuse
                    runner = None
                validate_id(command.get("session_id"), "invalid_protocol")
                validate_id(command.get("message_id"), "invalid_protocol")
                validate_id(command.get("token"), "invalid_protocol")
                validate_input(command.get("text"), {"model": command.get("model")})
                active_token = command["token"]
                executing = True
                runner = asyncio.create_task(run(command))
            elif op == "interrupt":
                if runner is not None and not runner.done() and command.get("token") == active_token:
                    task = asyncio.create_task(runtime.interrupt())
                    interrupt_tasks.add(task)
                    task.add_done_callback(interrupt_tasks.discard)
                    # Retrieve errors: the parent deadline handles failed cleanup.
                    task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
            elif op == "close":
                break
            else:
                raise IntegrationError("invalid_protocol")
    except (EOFError, BrokenPipeError):
        pass
    except BaseException as exc:
        code = _safe_code(getattr(exc, "code", None),
                          "profile_unavailable" if isinstance(exc, SystemExit) else "startup_failed")
        try:
            await send({"kind": "fault", "code": code})
        except (Exception, asyncio.CancelledError):
            pass
    finally:
        if receiver is not None:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
        if runtime is not None:
            try:
                await asyncio.wait_for(runtime.close(), 2.0)
            except BaseException:
                pass
        for task in interrupt_tasks:
            task.cancel()
        if runner is not None and not runner.done():
            runner.cancel()
        await asyncio.gather(*interrupt_tasks, *([runner] if runner is not None else []),
                             return_exceptions=True)


def _worker_main(conn):
    # Library print/log output cannot corrupt control frames or flood the
    # Connector's console. Diagnostic codes travel on the private pipe only.
    try:
        with open(os.devnull, "w", encoding="utf-8") as sink:
            sys.stdout = sys.stderr = sink
            asyncio.run(_child_loop(conn))
    finally:
        conn.close()
