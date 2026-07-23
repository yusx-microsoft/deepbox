"""Connector-local project registry.

Absolute paths are machine-private.  The browser/server only receive the stable
project id and display name returned by :meth:`LocalProjectStore.public_projects`.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

IS_WIN = os.name == "nt"


class LocalStoreBusyError(RuntimeError):
    """Raised when another process holds the short state mutation lock."""


def default_state_root() -> str:
    """Return the per-user deepbox state directory for this platform."""
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "state"
        )
    return os.path.join(base, "deepbox")


def default_state_path() -> str:
    return os.path.join(default_state_root(), "state.db")


def _chmod_best_effort(path: str, mode: int) -> None:
    if IS_WIN:
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


class _MutationLock:
    """Small cross-process lock held only while local project rows mutate."""

    def __init__(self, path: str, timeout: float = 5.0):
        self.path = path
        self.timeout = timeout
        self._fh = None

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout
        fh = open(self.path, "a+b")
        while True:
            try:
                if IS_WIN:
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    fh.close()
                    raise LocalStoreBusyError(
                        f"local project state is busy: {self.path}"
                    )
                time.sleep(0.05)
        _chmod_best_effort(self.path, 0o600)
        self._fh = fh

    def release(self) -> None:
        fh = self._fh
        if fh is None:
            return
        try:
            if IS_WIN:
                import msvcrt

                with contextlib.suppress(OSError):
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                with contextlib.suppress(OSError):
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
            self._fh = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()


@dataclass(frozen=True)
class LocalProject:
    id: str
    name: str
    path: str
    created_at: str
    updated_at: str

    def public_json(self) -> dict:
        """Return display-safe metadata.  Deliberately excludes ``path``."""
        return {"id": self.id, "name": self.name}


class LocalProjectStore:
    """SQLite-backed, per-user mapping from stable project ids to local paths."""

    def __init__(self, path: str | None = None):
        self.path = os.path.abspath(path or default_state_path())
        parent = os.path.dirname(self.path)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        _chmod_best_effort(parent, 0o700)
        self._lock = _MutationLock(self.path + ".lock")
        self._conn = sqlite3.connect(self.path, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS local_project (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.commit()
        _chmod_best_effort(self.path, 0o600)

    @staticmethod
    def _normalized_path(path: str) -> str:
        # A project path is literal user data.  Expand ``~`` for convenience,
        # but never expand environment-variable syntax embedded in a valid
        # directory name.
        raw = os.path.expanduser(str(path).strip())
        if not raw:
            raise ValueError("project path is required")
        return os.path.realpath(os.path.abspath(raw))

    @classmethod
    def _canonical_path(cls, path: str) -> str:
        result = cls._normalized_path(path)
        if not os.path.isdir(result):
            raise ValueError(f"project path is not a directory: {result}")
        return result

    @staticmethod
    def _project_name(path: str, name: str | None) -> str:
        value = (name or Path(path).name or Path(path).anchor or "Project").strip()
        if not value:
            raise ValueError("project name is required")
        if len(value) > 128:
            raise ValueError("project name must be at most 128 characters")
        return value

    @staticmethod
    def _from_row(row: sqlite3.Row | None) -> LocalProject | None:
        if row is None:
            return None
        return LocalProject(
            id=row["id"],
            name=row["name"],
            path=row["path"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def add(self, path: str, name: str | None = None) -> LocalProject:
        canonical = self._canonical_path(path)
        display_name = self._project_name(canonical, name)
        now = str(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM local_project WHERE path=?", (canonical,)
            ).fetchone()
            if row is None:
                project_id = uuid.uuid4().hex
                self._conn.execute(
                    "INSERT INTO local_project(id,name,path,created_at,updated_at) "
                    "VALUES(?,?,?,?,?)",
                    (project_id, display_name, canonical, now, now),
                )
            else:
                project_id = row["id"]
                self._conn.execute(
                    "UPDATE local_project SET name=?,updated_at=? WHERE id=?",
                    (display_name, now, project_id),
                )
            self._conn.commit()
        project = self.get(project_id)
        assert project is not None
        return project

    def remove(self, project_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM local_project WHERE id=?", (project_id,)
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def get(self, project_id: str | None) -> LocalProject | None:
        if not project_id:
            return None
        row = self._conn.execute(
            "SELECT id,name,path,created_at,updated_at FROM local_project WHERE id=?",
            (project_id,),
        ).fetchone()
        return self._from_row(row)

    def get_by_path(self, path: str) -> LocalProject | None:
        canonical = self._normalized_path(path)
        row = self._conn.execute(
            "SELECT id,name,path,created_at,updated_at FROM local_project WHERE path=?",
            (canonical,),
        ).fetchone()
        return self._from_row(row)

    def list_projects(self) -> list[LocalProject]:
        rows = self._conn.execute(
            "SELECT id,name,path,created_at,updated_at FROM local_project "
            "ORDER BY lower(name),id"
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def public_projects(self) -> list[dict]:
        return [project.public_json() for project in self.list_projects()]

    def resolve_agents(self, agents: Iterable[dict]) -> tuple[dict[str, dict], list[dict]]:
        """Resolve project ids locally and migrate legacy server-provided cwd values.

        Returned agent dictionaries may contain a local-only ``cwd`` and
        ``project_error``.  Neither is included in project reports.
        """
        resolved: dict[str, dict] = {}
        migrations: list[dict] = []
        for raw in agents:
            info = dict(raw)
            agent_id = str(info.get("id") or "")
            project_id = info.get("local_project_id")
            if project_id:
                project = self.get(str(project_id))
                if project is None:
                    info["cwd"] = None
                    info["project_error"] = (
                        f"Local project {project_id!s} is not registered on this machine"
                    )
                else:
                    info["cwd"] = project.path
                    info.pop("project_error", None)
            elif info.get("cwd"):
                try:
                    project = self.add(str(info["cwd"]))
                except (OSError, ValueError, sqlite3.Error) as exc:
                    info["project_error"] = f"Legacy project path could not be registered: {exc}"
                else:
                    info["local_project_id"] = project.id
                    info["cwd"] = project.path
                    info.pop("project_error", None)
                    migrations.append(
                        {"agent_id": agent_id, "local_project_id": project.id}
                    )
            if agent_id:
                resolved[agent_id] = info
        return resolved, migrations

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "LocalProjectStore":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()


def open_local_store(path: str | None = None) -> LocalProjectStore:
    return LocalProjectStore(path)
