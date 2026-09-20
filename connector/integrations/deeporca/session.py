"""Session-scoped structured display facade over a shared DeepOrcaWorker.

The Supervisor owns worker retirement. Closing a facade only detaches that
session, and an admitted turn keeps its original durable output callbacks until
settlement. Merely disconnecting a viewer never cancels model/tool execution.
"""
from __future__ import annotations

import asyncio
import inspect

from .events import (IntegrationError, MAX_EVENT_BYTES, json_bytes,
                     translate_deeporca_event, validate_id, validate_input)
from .worker import DeepOrcaWorker

__all__ = ["DeepOrcaSession", "validate_input", "translate_deeporca_event"]


class DeepOrcaSession:
    def __init__(self, worker: DeepOrcaWorker, session_id: str, on_output, on_exit,
                 *, on_turn_settled=None):
        self.worker = worker
        self.session_id = validate_id(session_id, "invalid_session_id")
        self.on_output = on_output
        self.on_exit = on_exit
        self.on_turn_settled = on_turn_settled
        self._started = False
        self._closed = False
        self._exited = False
        self._start_lock = asyncio.Lock()
        self._reservation = None

    async def _emit(self, event):
        data = json_bytes(event)
        if len(data) + 1 > MAX_EVENT_BYTES:
            raise IntegrationError("output_limit")
        # DeepBox records/replays the stream as JSONL, not concatenated JSON.
        await self.on_output(data.decode("utf-8") + "\n")

    async def start(self):
        async with self._start_lock:
            if self._closed:
                raise IntegrationError("session_closed")
            if self._started:
                return
            await self.worker.start()
            await self._emit({"ev": "session.config", "runtime": "deeporca",
                "renderer": "deeporca-chat-v1", "schema_version": 1,
                "controls": {"model": True},
                "capabilities": {"interrupt": True, "interactive_approval": False,
                                 "attachments": False}})
            self._started = True

    def is_alive(self):
        return self._started and not self._closed and self.worker.is_alive()

    def can_accept_turn(self):
        return self.is_alive() and self.worker.can_accept_turn()

    async def submit_input(self, client_input_id, text, options=None, *, admit=None):
        """Return on durable admission, NOT when execution completes.

        ``admit()`` runs synchronously with the Agent slot already reserved and
        before any task is launched, output is emitted, or IPC is dispatched.
        An exception releases the slot and propagates to the durable caller.
        """
        try:
            validate_id(client_input_id)
            normalized = validate_input(text, options)
            if not self.is_alive():
                raise IntegrationError("session_closed" if self._closed else "runtime_unavailable")
            reservation = self.worker.reserve(self.session_id, client_input_id)
        except IntegrationError as exc:
            return {"status": "rejected", "reason": exc.code}

        try:
            if admit is not None:
                result = admit()
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result):
                        result.close()
                    raise TypeError("admit must be synchronous")
        except BaseException:
            self.worker.release(reservation)
            raise

        self._reservation = reservation
        started = False
        done_event = None
        terminal_sent = False

        def local_event(ev, **fields):
            return {"ev": ev, "client_input_id": client_input_id,
                    "event_id": f"{reservation.token}:{ev}", **fields}

        async def begin():
            await self._emit(local_event("user.echo", text=text, message_id=client_input_id))

        async def ensure_turn_start(turn_id):
            nonlocal started
            if not started:
                started = True
                await self._emit(local_event("turn.start", turn_id=turn_id))

        async def native(event):
            nonlocal started, done_event
            if not self.worker.is_current(reservation):
                return
            kind = event["type"]
            if kind in ("done", "turn.end", "turn_end"):
                done_event = event
                # The SDK may emit this BEFORE native history has been saved.
                # It remains metadata only until a settled result arrives.
                return
            events = translate_deeporca_event(event)
            if kind in ("turn.start", "turn_start", "start"):
                if started:
                    return
                started = True
            else:
                await ensure_turn_start(event["turn_id"])
            for projected in events:
                if not self.worker.is_current(reservation):
                    return
                projected["client_input_id"] = client_input_id
                await self._emit(projected)

        async def settled(status, turn_id, code):
            nonlocal terminal_sent
            if terminal_sent:
                return
            turn_id = turn_id or reservation.token
            await ensure_turn_start(turn_id)
            if code:
                await self._emit(local_event("error", turn_id=turn_id, code=code,
                    message=("Native conversation context is unavailable. Start a new conversation; "
                             "if that also fails, restore or recreate the managed profile. "
                             "Display history cannot restore model context."
                             if code == "native_context_unavailable" else
                             "DeepOrca could not safely finish this turn."),
                    uncertain=status == "uncertain"))
            terminal = local_event("turn.end", turn_id=turn_id, status=status,
                is_error=status in ("error", "uncertain"),
                interrupted=status in ("cancelled", "uncertain"),
                uncertain=status == "uncertain",
                native={"runtime": "deeporca", "schema": "deeporca.turn.v1",
                        "type": "settled", "turn_id": turn_id,
                        "sequence": reservation.last_sequence + 1, "status": status})
            if done_event is not None:
                terminal["native"]["done"] = done_event
            if code == "native_context_unavailable":
                terminal["code"] = code
            await self._emit(terminal)
            terminal_sent = True
            # This ordering is a durability requirement, not just UI ordering.
            if self.on_turn_settled is not None:
                await self.on_turn_settled(client_input_id, status)
            if self._closed or not self.worker.is_alive():
                await self._exit(0 if status in ("completed", "cancelled") else 1)

        self.worker.dispatch(reservation, text, normalized.get("model"),
                             on_begin=begin, on_event=native, on_settled=settled)
        return {"status": "delivered", "reason": None}

    async def interrupt(self):
        # Another session's Stop button cannot cancel the Agent's active turn.
        await self.worker.interrupt(self.session_id)

    async def _exit(self, code):
        if not self._exited:
            self._exited = True
            await self.on_exit(code)

    async def close(self, reason="closed"):
        self._closed = True
        reservation = self._reservation
        if reservation is None or reservation.done.is_set():
            await self._exit(0)
        # Otherwise the running turn keeps its durable output/settlement sink.
        # No process kill: the worker may have other live session facades.

    def kill(self):
        """Best-effort compatibility: detach this facade, never the Agent."""
        self._closed = True
        try:
            asyncio.get_running_loop().create_task(self.close())
        except RuntimeError:
            pass

    def resize(self, cols, rows):
        pass
