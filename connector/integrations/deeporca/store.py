"""Machine-private DeepOrca bindings and durable input admission.

This store contains local paths and user input. It is never an inventory payload.
The output spool and native context are separate durability boundaries; an
in-flight turn recovered after a crash is uncertain and must not be retried.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from agentbridge.product import env


class BindingError(ValueError):
    """Public error text must be a stable, path/credential-free code."""


def enrollment_namespace(server_url: str, devbox_id: str) -> str:
    from ...spool import canonicalize_url
    return hashlib.sha256(
        (canonicalize_url(server_url) + "\0" + devbox_id).encode("utf-8")
    ).hexdigest()[:24]


def _key(value: str) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 200 or any(ord(c) < 32 for c in value):
        raise BindingError("invalid_identity")
    return value


def local_worker_settings() -> dict:
    """Only machine-admin environment values may select source/config paths."""
    result = {}
    for stem, key in (("DEEPORCA_SOURCE", "source_path"),
                      ("DEEPORCA_TEMPLATE_DIR", "template_dir")):
        value = env(stem)
        if value:
            path = Path(value).expanduser().resolve()
            if not path.is_dir():
                raise BindingError("invalid_local_runtime_configuration")
            result[key] = str(path)
    return result


class DeepOrcaStore:
    def __init__(self, path: str | Path | None = None, *, namespace: str = "local",
                 native_root: str | Path | None = None):
        self.namespace = _key(namespace)
        self.path = str(Path(path).resolve()) if path is not None else ":memory:"
        if path is not None:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.native_root = Path(native_root).resolve() if native_root else (
            Path(self.path).parent / "deeporca-native" if path is not None else None)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS bindings (
                namespace TEXT NOT NULL, agent_id TEXT NOT NULL,
                project_id TEXT NOT NULL, revision TEXT NOT NULL,
                profile_name TEXT NOT NULL, home TEXT NOT NULL,
                workspace TEXT NOT NULL, config TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', code TEXT,
                retired INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(namespace, agent_id)
            );
            CREATE TABLE IF NOT EXISTS native_sessions (
                namespace TEXT NOT NULL, agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL, native_session_id TEXT NOT NULL,
                PRIMARY KEY(namespace, agent_id, session_id)
            );
            CREATE TABLE IF NOT EXISTS inputs (
                namespace TEXT NOT NULL, agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL, input_id TEXT NOT NULL,
                text TEXT NOT NULL, options TEXT NOT NULL,
                state TEXT NOT NULL, result TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(namespace, agent_id, session_id, input_id)
            );
            CREATE TABLE IF NOT EXISTS spool_owners (
                spool_identity TEXT PRIMARY KEY, namespace TEXT NOT NULL
            );
        """)
        if path is not None:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    @contextmanager
    def _write(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def close(self):
        self._conn.close()

    def set_namespace(self, namespace: str):
        self.namespace = _key(namespace)

    def guard_spool_enrollment(self, spool_identity: str, namespace: str, *,
                               pending: bool, native_use: bool = False):
        """Pin a shared spool before native work; never relabel pending output.

        This private ledger does not tag or delete generic spool frames. After
        native use, *all* pending frames conservatively prevent a different
        enrollment from using that same spool. Drain/ACK under the old canonical
        Server URL + devbox identity, close workers, then rebind; alternatively
        use a fresh local-state directory AND fresh spool. Token changes do not
        change enrollment. Distinct spools keep independent ownership pins.

        Legacy ledgers have no pin: pending output is safe to adopt only when
        every native record belongs to the requested enrollment. Ambiguous or
        old-scope records fail closed, rather than assigning old frames anew.
        An unused ledger alone does not opt a CLI-only connector into this policy.
        """
        namespace = _key(namespace)
        with self._write() as conn:
            owner = conn.execute(
                "SELECT namespace FROM spool_owners WHERE spool_identity=?",
                (spool_identity,)).fetchone()
            if owner is not None:
                if owner["namespace"] == namespace:
                    return
                if pending:
                    raise BindingError("enrollment_identity_conflict")
            else:
                scopes = {row[0] for row in conn.execute(
                    "SELECT namespace FROM bindings UNION SELECT namespace FROM native_sessions "
                    "UNION SELECT namespace FROM inputs UNION SELECT namespace FROM spool_owners")}
                if pending and scopes - {namespace}:
                    raise BindingError("enrollment_identity_conflict")
                if not native_use and not scopes:
                    return
            conn.execute(
                "INSERT INTO spool_owners(spool_identity,namespace) VALUES(?,?) "
                "ON CONFLICT(spool_identity) DO UPDATE SET namespace=excluded.namespace",
                (spool_identity, namespace))

    def get_binding(self, agent_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM bindings WHERE namespace=? AND agent_id=?",
            (self.namespace, agent_id)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["config"] = json.loads(result["config"])
        return result

    def ensure_binding(self, agent: dict, *, allow_update=True, resolved_profile=None) -> dict:
        from agentbridge.integrations.deeporca.contract import (
            binding_identity_revision, binding_revision, validate_runtime_config)
        aid = _key(agent["id"])
        project = _key(agent.get("local_project_id"))
        config = validate_runtime_config(agent.get("runtime_config", {}))
        workspace = agent.get("cwd")
        if not isinstance(workspace, str) or not Path(workspace).is_dir():
            raise BindingError("local_project_unavailable")
        workspace = str(Path(workspace).resolve())
        revision = binding_revision(aid, project, config)
        identity = binding_identity_revision(aid, project, config)
        existing_profile = config["profile"]["mode"] == "bind"
        if existing_profile:
            from .profiles import selected_profile
            selected = selected_profile(resolved_profile, config["profile"]["profile_ref"])
            if selected is None:
                raise BindingError("existing_profile_unavailable")
            profile, home = selected["profile_name"], selected["home"]
        else:
            if self.native_root is None:
                raise BindingError("native_storage_unavailable")
            # Managed paths use only opaque identities. Browser requests never
            # choose home/name; existing selections come from local discovery.
            ns = hashlib.sha256(self.namespace.encode()).hexdigest()[:24]
            profile = "dbx-" + hashlib.sha256((self.namespace + "\0" + aid).encode()).hexdigest()[:24]
            home = str(self.native_root / ns)
        with self._write() as conn:
            row = conn.execute("SELECT * FROM bindings WHERE namespace=? AND agent_id=?",
                               (self.namespace, aid)).fetchone()
            if row is not None:
                if (row["project_id"] != project or row["workspace"] != workspace
                        or binding_identity_revision(aid, row["project_id"],
                            json.loads(row["config"])) != identity or row["retired"]
                        or (existing_profile and (
                            os.path.normcase(row["home"]) != os.path.normcase(home)
                            or os.path.normcase(row["profile_name"]) != os.path.normcase(profile)))):
                    raise BindingError("binding_identity_conflict")
            if existing_profile:
                # SQLite transaction serializes local reservations. This is not
                # a standalone/native lock; the SDK fences actual acquisition.
                target = os.path.normcase(str(Path(home) / "agents" / profile))
                for other in conn.execute("SELECT home,profile_name FROM bindings "
                                          "WHERE namespace=? AND agent_id<>? AND retired=0",
                                          (self.namespace, aid)):
                    if os.path.normcase(str(Path(other["home"]) / "agents" / other["profile_name"])) == target:
                        raise BindingError("existing_profile_unavailable")
            if row is not None:
                if row["revision"] != revision:
                    if not allow_update or conn.execute("SELECT 1 FROM inputs WHERE namespace=? AND agent_id=? "
                                    "AND state='running' LIMIT 1", (self.namespace, aid)).fetchone():
                        raise BindingError("configuration_busy")
                    conn.execute("UPDATE bindings SET config=?,revision=?,state='pending',code=NULL "
                                 "WHERE namespace=? AND agent_id=?",
                                 (json.dumps(config, sort_keys=True), revision, self.namespace, aid))
            else:
                conn.execute(
                    "INSERT INTO bindings(namespace,agent_id,project_id,revision,profile_name,home,workspace,config) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (self.namespace, aid, project, revision, profile, home, workspace,
                     json.dumps(config, sort_keys=True)))
        return self.get_binding(aid)

    def worker_binding(self, agent_id: str) -> dict:
        binding = self.get_binding(agent_id)
        if binding is None or binding["retired"]:
            raise BindingError("binding_not_found")
        result = {k: binding[k] for k in ("agent_id", "profile_name", "home", "workspace")}
        result["config"] = binding["config"]
        if self.path != ":memory:":
            from .credentials import key_path
            result["credential_key_path"] = str(key_path(self.path))
        result.update(local_worker_settings())
        return result

    def set_status(self, agent_id: str, state: str, code: str | None = None):
        if state not in {"pending", "provisioning", "ready", "needs_configuration", "error"}:
            raise BindingError("invalid_runtime_state")
        if code is not None and (not isinstance(code, str) or not code.replace("_", "").isalnum()
                                 or len(code) > 80):
            raise BindingError("invalid_runtime_error_code")
        with self._write() as conn:
            conn.execute("UPDATE bindings SET state=?,code=? WHERE namespace=? AND agent_id=?",
                         (state, code, self.namespace, agent_id))

    def public_status(self, agent_id: str) -> dict | None:
        row = self.get_binding(agent_id)
        if row is None or row["retired"]:
            return None
        status = {"state": row["state"], "revision": row["revision"]}
        if row["code"]:
            status["code"] = row["code"]
        return {"type": "agent.runtime_status", "agent_id": agent_id, "runtime_status": status}

    def active_agent_ids(self) -> set[str]:
        with self._lock:
            return {row[0] for row in self._conn.execute(
                "SELECT agent_id FROM bindings WHERE namespace=? AND retired=0",
                (self.namespace,))}

    def retire(self, agent_id: str):
        # The native profile and all history remain on disk by design.
        with self._write() as conn:
            conn.execute("UPDATE bindings SET retired=1 WHERE namespace=? AND agent_id=?",
                         (self.namespace, agent_id))

    def native_session_id(self, agent_id: str, session_id: str) -> str:
        _key(agent_id); _key(session_id)
        native_id = uuid5(NAMESPACE_URL, "\0".join((self.namespace, agent_id, session_id))).hex
        with self._write() as conn:
            conn.execute("INSERT OR IGNORE INTO native_sessions VALUES(?,?,?,?)",
                         (self.namespace, agent_id, session_id, native_id))
            row = conn.execute(
                "SELECT native_session_id FROM native_sessions WHERE namespace=? AND agent_id=? AND session_id=?",
                (self.namespace, agent_id, session_id)).fetchone()
        return row[0]

    def input_receipt(self, agent_id: str, session_id: str, input_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT state,result FROM inputs WHERE namespace=? AND agent_id=? AND session_id=? AND input_id=?",
            (self.namespace, agent_id, session_id, input_id)).fetchone()
        return dict(row) if row is not None else None

    def admit_input(self, agent_id: str, session_id: str, input_id: str,
                    text: str, options: dict | None = None):
        for value in (agent_id, session_id, input_id):
            _key(value)
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 64 * 1024:
            raise BindingError("invalid_input")
        with self._write() as conn:
            conn.execute(
                "INSERT INTO inputs(namespace,agent_id,session_id,input_id,text,options,state) "
                "VALUES(?,?,?,?,?,?,'running')",
                (self.namespace, agent_id, session_id, input_id, text,
                 json.dumps(options or {}, sort_keys=True)))
        # Dispatch intent is now durable. A crash from this point is uncertain,
        # even if the worker had not yet read the IPC frame. Never auto-resubmit.

    def settle_input(self, agent_id: str, session_id: str, input_id: str, result: str):
        if result not in {"completed", "cancelled", "uncertain", "error"}:
            result = "error"
        with self._write() as conn:
            conn.execute("UPDATE inputs SET state='settled',result=?,text='',options='{}' "
                         "WHERE namespace=? AND agent_id=? AND session_id=? AND input_id=?",
                         (result, self.namespace, agent_id, session_id, input_id))

    def recover_interrupted(self) -> list[dict]:
        with self._write() as conn:
            rows = conn.execute("SELECT agent_id,session_id,input_id FROM inputs "
                                "WHERE namespace=? AND state='running'", (self.namespace,)).fetchall()
            conn.execute("UPDATE inputs SET state='settled',result='uncertain',text='',options='{}' "
                         "WHERE namespace=? AND state='running'", (self.namespace,))
        return [dict(row) for row in rows]

    def session_recovery(self, agent_id: str, session_id: str) -> list[dict]:
        return [dict(row) for row in self._conn.execute(
            "SELECT input_id,result FROM inputs WHERE namespace=? AND agent_id=? AND session_id=? "
            "AND result='uncertain'", (self.namespace, agent_id, session_id)).fetchall()]
