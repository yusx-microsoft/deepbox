"""DeepOrca-specific lifecycle extension for the generic session Supervisor."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

from .events import IntegrationError, validate_input
from .store import BindingError, DeepOrcaStore, enrollment_namespace

# Machine-local policy, intentionally not browser/runtime configuration. Shared
# spool usage counts toward the limit, but only library output/admission is gated.
DEEPORCA_BACKLOG_BYTES = 64 * 1024 * 1024
DEEPORCA_BACKLOG_FRAMES = 10_000
DEEPORCA_EMERGENCY_BYTES = 64 * 1024
DEEPORCA_EMERGENCY_FRAMES = 32


class DeepOrcaSupervisorMixin:
    """Narrow lifecycle hooks consumed by Supervisor, not a plugin registry.

    Admission, transport receipts, worker ownership and durable output stay in
    this extension. The platform retains agent/session registration and CLI IO.
    """

    def _init_runtime_extension(self, store=None, worker_factory=None):
        self._deeporca_store = store
        self._deeporca_worker_factory = worker_factory
        self._deeporca_workers = {}
        self._deeporca_tasks = {}
        self._deeporca_locks = {}
        self._enrollment_namespace = "local"
        self._deeporca_recovered = False
        self._deeporca_backlog_bytes = DEEPORCA_BACKLOG_BYTES
        self._deeporca_backlog_frames = DEEPORCA_BACKLOG_FRAMES

    def _library_output_capacity(self, frame=None, *, emergency=False):
        try:
            frames, size = self._spool.pending_usage()
            # Match spool JSON/UTF-8 serialization, including the envelope and
            # a conservative 19-digit SQLite sequence (never allocate a seq on
            # rejection). No backlog scan on the native event hot path.
            extra = (len(json.dumps({**frame, "seq": 2**63 - 1}, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8"))
                     if frame is not None else 1)
            max_frames = self._deeporca_backlog_frames
            max_bytes = self._deeporca_backlog_bytes
            if emergency:
                max_frames += DEEPORCA_EMERGENCY_FRAMES
                max_bytes += DEEPORCA_EMERGENCY_BYTES
            if frames + 1 > max_frames or size + extra > max_bytes:
                raise IntegrationError("output_unavailable")
        except Exception:
            # Storage exceptions may contain local paths, credentials or errno.
            raise IntegrationError("output_unavailable") from None

    def _library_emit_output(self, frame, *, emergency=False):
        try:
            self._library_store()  # Ownership must be durable before first output.
            self._library_output_capacity(frame, emergency=emergency)
            self.emit(frame)
        except Exception:
            raise IntegrationError("output_unavailable") from None

    def set_enrollment(self, server_url: str, devbox_id: str):
        namespace = enrollment_namespace(server_url, devbox_id)
        changing = namespace != self._enrollment_namespace
        if changing and (self._deeporca_workers
                         or any(not task.done() for task in self._deeporca_tasks.values())
                         or any(lock.locked() for lock in self._deeporca_locks.values())):
            raise BindingError("enrollment_identity_conflict")
        # Bootstrap calls this before opening a WebSocket/starting its sender.
        # Load an existing private ledger, but do NOT recover inputs or switch
        # namespaces until its spool ownership has been checked. CLI-only
        # connectors must not create a DeepOrca database merely by enrolling.
        store = self._deeporca_store
        try:
            path = self._library_store_path()
            if store is None and path is not None and path.exists():
                store = DeepOrcaStore(path)
            if store is not None:
                self._guard_library_spool(store, namespace)
        except Exception:
            if store is not None and store is not self._deeporca_store:
                store.close()
            raise BindingError("enrollment_identity_conflict") from None
        self._deeporca_store = store
        self._enrollment_namespace = namespace
        if store is not None:
            store.set_namespace(namespace)
        if changing:
            self._deeporca_recovered = False

    def _library_store_path(self):
        return (Path(self.local_store.path).with_name("deeporca.sqlite3")
                if self.local_store is not None else None)

    def _guard_library_spool(self, store, namespace, *, native_use=False):
        path = getattr(self._spool, "path", None)
        identity = ("disk:" + hashlib.sha256(
            os.path.normcase(str(Path(path).resolve())).encode("utf-8")).hexdigest()
            if path is not None else "memory:" + str(id(self._spool)))
        # Controls are ephemeral but can still disclose the previous native
        # scope when a Supervisor is reused. Require their old-transport drain
        # too; never silently drop them or relabel them for the new enrollment.
        store.guard_spool_enrollment(
            identity, namespace,
            pending=bool(self._controls) or self._spool.pending_usage()[0] > 0,
            native_use=native_use)

    def _library_store(self):
        if self._deeporca_store is None:
            self._deeporca_store = DeepOrcaStore(
                self._library_store_path(), namespace=self._enrollment_namespace)
        self._guard_library_spool(self._deeporca_store, self._enrollment_namespace,
                                  native_use=True)
        if not self._deeporca_recovered:
            self._deeporca_store.recover_interrupted()
            self._deeporca_recovered = True
        return self._deeporca_store

    def _new_library_worker(self, binding):
        factory = self._deeporca_worker_factory
        if factory is None:
            from .worker import DeepOrcaWorker
            factory = DeepOrcaWorker
        return factory(binding)

    @staticmethod
    def _library_error(exc):
        known = {"configuration_required", "profile_unavailable", "profile_conflict",
                 "binding_identity_conflict", "local_project_unavailable",
                 "invalid_local_runtime_configuration", "embedded_api_unavailable",
                 "runtime_unavailable", "worker_lost", "startup_failed",
                 "native_context_unavailable", "enrollment_identity_conflict", "output_unavailable"}
        value = str(exc) if isinstance(exc, BindingError) else getattr(exc, "code", None)
        return value if value in known else "startup_failed"

    def _library_status(self, agent_id, state, code=None):
        store = self._library_store()
        info = self.agents.get(agent_id)
        if not info or self._stopped:
            return
        if store.get_binding(agent_id) is not None:
            store.set_status(agent_id, state, code)
            frame = store.public_status(agent_id)
        else:
            from agentbridge.integrations.deeporca.contract import binding_revision
            frame = {"type": "agent.runtime_status", "agent_id": agent_id,
                     "runtime_status": {"state": state,
                         "revision": binding_revision(agent_id, info.get("local_project_id"),
                                                      info.get("runtime_config")),
                         **({"code": code} if code else {})}}
        if frame:
            self.emit(frame)

    async def _ensure_library_worker(self, agent_id):
        lock = self._deeporca_locks.setdefault(agent_id, asyncio.Lock())
        async with lock:
            info = self.agents.get(agent_id)
            if self._stopped or not info or info.get("runtime") != "deeporca":
                raise BindingError("runtime_unavailable")
            store = self._library_store()
            worker = None
            try:
                store.ensure_binding({**info, "id": agent_id})
                existing = self._deeporca_workers.get(agent_id)
                if existing is not None and existing.is_alive():
                    self._library_status(agent_id, "ready")
                    return existing
                if existing is not None:
                    await existing.close()
                    self._deeporca_workers.pop(agent_id, None)
                self._library_status(agent_id, "provisioning")
                binding = store.worker_binding(agent_id)
                result = await self._new_library_worker(binding).provision()
                if result.get("configured") is not True:
                    raise BindingError("configuration_required")
                if self._stopped or not self._same_library_binding(agent_id, info):
                    raise BindingError("runtime_unavailable")
                worker = self._new_library_worker(binding)
                await worker.start()
                if self._stopped or not self._same_library_binding(agent_id, info):
                    raise BindingError("runtime_unavailable")
                self._deeporca_workers[agent_id] = worker
                self._library_status(agent_id, "ready")
                return worker
            except BaseException as exc:
                if worker is not None:
                    await worker.close()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                code = self._library_error(exc)
                self._library_status(agent_id, "needs_configuration" if code == "configuration_required" else "error", code)
                raise BindingError(code) from None

    def _same_library_binding(self, agent_id, previous):
        current = self.agents.get(agent_id)
        if current is None:
            return False
        return all(current.get(key) == previous.get(key) for key in (
            "runtime", "local_project_id", "cwd", "runtime_config"))

    def _schedule_runtime_reconciliation(self):
        if self._stopped:
            return
        async def reconcile(aid):
            try:
                await self._ensure_library_worker(aid)
            except (BindingError, ValueError):
                pass
            except Exception:
                self._library_status(aid, "error", "startup_failed")
        for aid, info in self.agents.items():
            if info.get("runtime") != "deeporca":
                continue
            task = self._deeporca_tasks.get(aid)
            if task is None or task.done():
                self._deeporca_tasks[aid] = asyncio.create_task(reconcile(aid))

    async def _retire_runtime_agent(self, agent_id):
        task = self._deeporca_tasks.pop(agent_id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        worker = self._deeporca_workers.pop(agent_id, None)
        if worker is not None:
            await worker.close()
        if self._deeporca_store is not None:
            self._deeporca_store.retire(agent_id)

    @staticmethod
    def _is_library_session(session):
        return bool(getattr(session, "_deepbox_library", False))

    async def _handle_runtime_control(self, frame):
        """Return True only for a control owned by this integration."""
        kind = frame.get("type")
        if kind not in ("input", "interrupt", "close", "terminate"):
            return False
        aid, sid = frame.get("agent_id"), frame.get("session_id")
        key = (aid, sid)
        session = self.ptys.get(key)
        if kind == "input" and (self.agents.get(aid, {}).get("runtime") == "deeporca"
                                or self._is_library_session(session)):
            self.emit(await self._library_input(frame, session))
            return True
        if not self._is_library_session(session):
            return False
        if kind == "interrupt":
            await session.interrupt()
            return True
        if kind in ("close", "terminate"):
            # Termination is not viewer detachment. Settle while output
            # epoch/cursor state is valid, before fencing this session.
            await session.interrupt()
            await session.close("session_closed")
            self._invalidate_open(key)
            if self.ptys.get(key) is session:
                self.ptys.pop(key, None)
                self.pty_instances.pop(key, None)
                self.pty_surfaces.pop(key, None)
            return True
        return False

    async def _open_runtime_session(self, adapter, agent_id, session_id, surface,
                                    pty_instance_id, current):
        """Handle library session creation wholly outside the CLI/PTY path."""
        if adapter.id != "deeporca":
            return False
        key = (agent_id, session_id)

        async def on_output(data, *, _library_emergency=False):
            if (self._stopped or agent_id not in self.agents
                    or (self.ptys.get(key) is not session and not current())):
                raise IntegrationError("output_unavailable")
            self._library_emit_output({
                "type": "output", "agent_id": agent_id, "session_id": session_id,
                "pty_instance_id": pty_instance_id, "data": data, "kind": "event",
            }, emergency=_library_emergency)

        async def on_exit(code):
            if self.ptys.get(key) is not session:
                return
            self.ptys.pop(key, None)
            self.emit({"type": "exit", "agent_id": agent_id, "session_id": session_id,
                       "pty_instance_id": pty_instance_id, "code": code})
            self.pty_instances.pop(key, None)
            self.pty_surfaces.pop(key, None)

        try:
            session = await self._make_library_session(agent_id, session_id, on_output, on_exit)
        except BindingError as exc:
            self.emit({"type": "runtime.unavailable", "agent_id": agent_id,
                       "session_id": session_id, "runtime": "deeporca",
                       "surface": surface, "code": self._library_error(exc)})
            return True
        try:
            await session.start()
            if not current():
                return True
            if not session.is_alive():
                raise RuntimeError("Runtime exited during startup")
            self.ptys[key] = session
            self.pty_instances[key] = pty_instance_id
            self.pty_surfaces[key] = surface
            await self._library_recovery_notice(agent_id, session_id, on_output)
        finally:
            if self.ptys.get(key) is not session:
                try:
                    session.kill()
                except Exception:
                    pass
        self.emit({"type": "ready", "agent_id": agent_id, "session_id": session_id,
                   "pty_instance_id": pty_instance_id, "surface": surface,
                   "structured": adapter.structured})
        self.emit({"type": "presence", "agent_id": agent_id, "state": "online"})
        return True

    async def _make_library_session(self, agent_id, session_id, on_output, on_exit):
        from .session import DeepOrcaSession
        worker = await self._ensure_library_worker(agent_id)
        store = self._library_store()
        native_id = store.native_session_id(agent_id, session_id)
        failed_input = None

        async def durable_output(data):
            nonlocal failed_input
            event = json.loads(data)
            input_id = event.get("client_input_id")
            if input_id is not None and input_id == failed_input:
                raise IntegrationError("output_unavailable")
            try:
                await on_output(data)
            except Exception:
                if input_id is not None:
                    failed_input = input_id
                    # Even a native completed result is uncertain if its
                    # terminal record cannot be committed. If the journal also
                    # fails, the running intent recovers as uncertain on boot.
                    try:
                        store.settle_input(agent_id, session_id, input_id, "uncertain")
                    except Exception:
                        pass
                    # A fixed global reserve, not a per-turn exemption. Never
                    # include native payloads/done metadata in emergency output.
                    base = {"client_input_id": input_id,
                            "turn_id": event.get("turn_id") or input_id,
                            "uncertain": True}
                    for notice in (
                        {**base, "ev": "error", "code": "output_unavailable",
                         "message": "DeepOrca output could not be stored safely."},
                        {**base, "ev": "turn.end", "status": "uncertain",
                         "is_error": True, "interrupted": True},
                    ):
                        try:
                            await on_output(json.dumps(notice, separators=(",", ":")) + "\n",
                                            _library_emergency=True)
                        except Exception:
                            break
                raise IntegrationError("output_unavailable") from None

        async def settled(input_id, status):
            store.settle_input(agent_id, session_id, input_id,
                               "uncertain" if input_id == failed_input else status)

        session = DeepOrcaSession(worker, native_id, durable_output, on_exit,
                                  on_turn_settled=settled)
        session._deepbox_library = True
        return session

    async def _library_input(self, frame, session):
        try:
            return await self._library_handle_input(frame, session)
        except Exception:
            # Reads and admission writes must also fail closed, without leaking
            # sqlite/OS diagnostics into a correlated control response.
            return {"type": "input_ack", "agent_id": frame.get("agent_id"),
                    "session_id": frame.get("session_id"),
                    "client_input_id": frame.get("client_input_id"),
                    "status": "rejected", "reason": "output_unavailable"}

    async def _library_handle_input(self, frame, session):
        aid, sid, input_id = frame["agent_id"], frame["session_id"], frame.get("client_input_id")
        ack = {"type": "input_ack", "agent_id": aid, "session_id": sid,
               "client_input_id": input_id, "status": "rejected"}
        if not isinstance(input_id, str) or not input_id or len(input_id) > 200:
            return {**ack, "reason": "invalid_input_id"}
        store = self._library_store()
        receipt = store.input_receipt(aid, sid, input_id)
        if receipt is not None:
            if receipt.get("result") == "uncertain":
                return {**ack, "reason": "execution_uncertain"}
            return {**ack, "status": "delivered", "duplicate": True}
        if session is None or not session.is_alive():
            return {**ack, "reason": "session_not_ready"}
        info = self.agents.get(aid, {})
        options = dict(frame.get("options") or {}) if isinstance(frame.get("options"), (dict, type(None))) else frame["options"]
        if isinstance(options, dict) and "model" not in options:
            model = (info.get("runtime_config") or {}).get("model")
            if model:
                options["model"] = model
        try:
            validate_input(frame.get("data"), options)
        except (ValueError, TypeError):
            return {**ack, "reason": "invalid_options"}

        def admit():
            # Synchronous with the worker reservation; no dispatch or receipt
            # exists when pressure rejects. ACK drain permits same-ID retry.
            self._library_output_capacity()
            try:
                store.admit_input(aid, sid, input_id, frame["data"], options)
            except Exception:
                raise IntegrationError("output_unavailable") from None

        try:
            result = await session.submit_input(input_id, frame["data"], options, admit=admit)
        except Exception as exc:
            # A committed dispatch intent remains uncertain if handoff failed.
            if store.input_receipt(aid, sid, input_id) is not None:
                store.settle_input(aid, sid, input_id, "uncertain")
            return {**ack, "reason": "output_unavailable" if getattr(exc, "code", None) == "output_unavailable"
                    else "input_delivery_failed"}
        return {**ack, **result, "type": "input_ack", "agent_id": aid,
                "session_id": sid, "client_input_id": input_id}

    async def _library_recovery_notice(self, agent_id, session_id, on_output):
        uncertain = self._library_store().session_recovery(agent_id, session_id)
        if uncertain:
            await on_output(json.dumps({
                "ev": "error", "code": "execution_uncertain",
                "message": "A previous turn was interrupted. It was not re-executed; tool side effects may already exist.",
                "input_ids": [row["input_id"] for row in uncertain[:100]],
            }, separators=(",", ":")) + "\n")

    async def _close_runtime_sessions(self):
        tasks = list(self._deeporca_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._deeporca_tasks.clear()
        workers = list(self._deeporca_workers.values())
        self._deeporca_workers.clear()
        await asyncio.gather(*(w.close() for w in workers), return_exceptions=True)
        for session in list(self.ptys.values()):
            if self._is_library_session(session):
                await session.close("supervisor_shutdown")

    def _close_runtime_storage(self):
        if self._deeporca_store is not None:
            self._deeporca_store.close()
            self._deeporca_store = None
