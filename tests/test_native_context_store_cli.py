"""Durable reservations and explicit, local-only writer recovery."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from connector import cli
from connector.local_store import LocalProjectStore
from connector.native_writer import NativeWriterLease, NativeWriterRecoveryRequired, status

SID = "23015cb9-e1d0-46d7-b217-fbce867b15aa"


def test_attempt_survives_reopen_and_never_downgrades_established(tmp_path):
    db = tmp_path / "state.db"
    store = LocalProjectStore(str(db))
    attempted = store.reserve_native_context("a", SID, "runtime", str(tmp_path))
    assert attempted.state == "attempted"
    assert attempted.established_at == ""
    store.close()
    store = LocalProjectStore(str(db))
    try:
        assert store.native_context("a", SID).state == "attempted"
        established = store.establish_native_context("a", SID, "runtime", str(tmp_path))
        assert established.state == "established" and established.established_at
        again = store.reserve_native_context("a", SID, "runtime", str(tmp_path))
        assert again.state == "established"
        assert again.established_at == established.established_at
        with pytest.raises(ValueError):
            store.reserve_native_context("a", SID, "other-runtime", str(tmp_path))
        assert store.native_context("a", SID).runtime_id == "runtime"
    finally:
        store.close()


def test_old_schema_migrates_without_reassigning_history(tmp_path):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE native_context (agent_id TEXT, session_id TEXT, "
                     "runtime_id TEXT, cwd TEXT, established_at TEXT NOT NULL, "
                     "updated_at TEXT NOT NULL, PRIMARY KEY(agent_id, session_id))")
        conn.execute("INSERT INTO native_context VALUES(?,?,?,?,?,?)",
                     ("a", SID, "runtime", str(tmp_path), "before", "before"))
    store = LocalProjectStore(str(db))
    try:
        row = store.native_context("a", SID)
        assert row.state == "established"
        assert row.established_at == "before"
        assert row.cwd == str(tmp_path)
        assert store._conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        store.close()


def test_conflicting_connections_cannot_rebind_a_reserved_id(tmp_path):
    db = tmp_path / "state.db"
    LocalProjectStore(str(db)).close()
    barrier = Barrier(2)

    def reserve(index):
        store = LocalProjectStore(str(db))
        barrier.wait(timeout=5)
        try:
            store.reserve_native_context("a", SID, f"runtime-{index}", str(tmp_path))
            return True
        except ValueError:
            return False
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, [0, 1]))
    assert sorted(results) == [False, True]
    store = LocalProjectStore(str(db))
    try:
        winner = results.index(True)
        assert store.native_context("a", SID).runtime_id == f"runtime-{winner}"
    finally:
        store.close()


def test_context_cli_is_local_requires_confirmation_and_exact_owner(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("connector.local_store.default_state_root", lambda: str(tmp_path))
    def no_network(_):
        raise AssertionError("context commands must not run/connect a connector")
    monkeypatch.setattr(cli.asyncio, "run", no_network)
    root = tmp_path / "native-writers"
    guard = NativeWriterLease(str(root), "claude-code", SID)
    guard.acquire()
    guard.begin_process()
    owner = status(str(root), "claude-code", SID)["owner"]
    assert cli.main(["context", "release", "claude-code", SID,
                     "--owner", owner, "--confirm-writer-stopped"]) == 2
    assert "context.in_use" in capsys.readouterr().err
    guard.release()  # simulate lost supervisor; never automatically reclaim
    assert cli.main(["context", "status", "claude-code", SID]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "active"
    assert cli.main(["context", "release", "claude-code", SID, "--owner", owner]) == 2
    capsys.readouterr()
    with pytest.raises(NativeWriterRecoveryRequired):
        NativeWriterLease(str(root), "claude-code", SID).acquire()
    assert cli.main(["context", "release", "claude-code", SID,
                     "--owner", "wrong-owner", "--confirm-writer-stopped"]) == 2
    capsys.readouterr()
    assert cli.main(["context", "release", "claude-code", SID,
                     "--owner", owner, "--confirm-writer-stopped"]) == 0
    assert json.loads(capsys.readouterr().out) == {"released": True}
    assert status(str(root), "claude-code", SID)["state"] == "idle"
    assert cli.main(["context", "status", "unknown-runtime", SID]) == 2
    assert "does not support" in capsys.readouterr().err
