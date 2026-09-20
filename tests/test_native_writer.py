"""Only temporary local files and explicitly owned Python subprocesses are used."""

import errno
import json
import os
from pathlib import Path
import queue
import re
import stat
import subprocess
import sys
import threading
import time
import traceback
from contextlib import contextmanager

import pytest

from connector import native_writer as nw


_REPO = str(Path(__file__).resolve().parents[1])
_FAMILY = "claude"
_SESSION = "a0b1c2d3-e4f5-4678-9abc-def012345678"
_WORKER = r'''
import json
import os
import sys
sys.path.insert(0, sys.argv[1])
from connector.native_writer import NativeWriterLease, NativeWriterError, status

root, family, session, mode = sys.argv[2:6]
lease = NativeWriterLease(root, family, session)
try:
    lease.acquire()
except NativeWriterError as exc:
    print(json.dumps({"code": exc.code, "message": str(exc)}), flush=True)
else:
    try:
        if mode == "active":
            lease.begin_process()
        print(json.dumps({"acquired": True, "owner": lease.owner,
                          "pid": os.getpid()}), flush=True)
        if mode != "probe":
            command = sys.stdin.readline().strip()
            if command == "reap":
                lease.process_reaped()
    finally:
        lease.release()
'''


def _arguments(root, family, session, mode):
    return [sys.executable, "-c", _WORKER, _REPO, str(root), family, session, mode]


def _probe(root, family=_FAMILY, session=_SESSION, *, cwd=None, agent="other"):
    env = dict(os.environ, AGENTBRIDGE_AGENT_ID=agent)
    result = subprocess.run(
        _arguments(root, family, session, "probe"), cwd=cwd, env=env,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    return json.loads(result.stdout)


@contextmanager
def _holder(root, family=_FAMILY, session=_SESSION, *, mode="active", cwd=None):
    process = subprocess.Popen(
        _arguments(root, family, session, mode), cwd=cwd,
        env=dict(os.environ, AGENTBRIDGE_AGENT_ID="original-agent"),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    ready = queue.Queue()
    reader = threading.Thread(target=lambda: ready.put(process.stdout.readline()),
                              daemon=True)
    reader.start()
    try:
        line = ready.get(timeout=15)
        assert line, "lease worker exited without a readiness message"
        packet = json.loads(line)
        assert packet.get("acquired") is True, packet
        assert packet["pid"] != os.getpid()
        yield process, packet
    finally:
        # Never enumerate or kill foreign processes, including purported PIDs
        # from journals. Only this exact Popen child belongs to the test.
        if process.poll() is None:
            process.kill()
        process.wait(timeout=15)
        reader.join(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def _lease(root, family=_FAMILY, session=_SESSION):
    return nw.NativeWriterLease(str(root), family, session)


def _status(root, family=_FAMILY, session=_SESSION):
    return nw.status(str(root), family, session)


def _recover(root, owner, consent=True):
    return nw.recover_native_writer(str(root), _FAMILY, _SESSION, owner, consent)


def _lock_path(root):
    paths = list(root.glob("*.lock"))
    assert len(paths) == 1
    return paths[0]


def _read_body(path):
    # Byte zero may be mandatorily locked by another Windows process.
    with path.open("rb") as stream:
        stream.seek(1)
        return stream.read()


def _orphan(root):
    lease = _lease(root).acquire()
    try:
        owner = lease.begin_process()
    finally:
        lease.release()
    return owner


def test_independent_process_contends_for_live_lock(tmp_path):
    root = tmp_path / "leases"
    with _holder(root) as (process, packet):
        before = time.monotonic()
        result = _probe(root)
        assert time.monotonic() - before < 10  # Nonblocking, not a lock wait/TTL.
        assert result == {"code": "context.in_use",
                          "message": "Native context is already in use."}
        assert _status(root) == {"state": "active", "owner": packet["owner"]}
        assert process.poll() is None
        contender = _lease(root)
        for _ in range(2):  # Rejected instances can be retried.
            with pytest.raises(nw.NativeWriterBusy) as error:
                contender.acquire()
            assert isinstance(error.value, ValueError)
            assert error.value.code == "context.in_use"
            contender.release()


def test_live_idle_lock_is_also_exclusive_and_retry_succeeds(tmp_path):
    root = tmp_path / "leases"
    contender = _lease(root)
    with _holder(root, mode="idle") as (process, _):
        assert _status(root) == {"state": "idle", "owner": None}
        with pytest.raises(nw.NativeWriterBusy):
            contender.acquire()
        assert _probe(root)["code"] == "context.in_use"
        process.stdin.write("release\n")
        process.stdin.flush()
        assert process.wait(timeout=15) == 0
    assert contender.acquire() is contender
    contender.release()


def test_sessions_and_families_are_independent(tmp_path):
    root = tmp_path / "leases"
    with _holder(root):
        assert _probe(root, session="different-session")["acquired"]
        assert _probe(root, family="copilot")["acquired"]
        assert _probe(root)["code"] == "context.in_use"
    assert len(list(root.glob("*.lock"))) == 3


@pytest.mark.parametrize("spelling", [
    _SESSION.upper(), "{" + _SESSION.upper() + "}", _SESSION.replace("-", ""),
    "urn:uuid:" + _SESSION,
])
def test_uuid_spellings_agents_and_cwd_cannot_bypass(tmp_path, spelling):
    root = tmp_path / "leases"
    original, remapped = tmp_path / "original", tmp_path / "remapped"
    original.mkdir()
    remapped.mkdir()
    with _holder(root, cwd=original):
        assert _probe(root, session=spelling, cwd=remapped,
                      agent="remapped-agent")["code"] == "context.in_use"
    assert len(list(root.glob("*.lock"))) == 1


def test_hash_is_opaque_unambiguous_and_not_path_sensitive(tmp_path):
    root = tmp_path / "leases"
    pairs = [("ab", "c"), ("a", "bc"), ("a", "B C"), ("a", "b c"),
             ("private/family", "../../secret-session\\native-output")]
    for family, session in pairs:
        with _lease(root, family, session):
            assert _status(root, family, session)["state"] == "idle"
    files = list(root.iterdir())
    assert len(files) == len(pairs)
    assert all(re.fullmatch(r"[0-9a-f]{64}\.lock", p.name) for p in files)


def test_empty_initial_file_and_idempotent_release_keep_inode(tmp_path):
    root = tmp_path / "leases"
    lease = _lease(root)
    lease.release()
    assert lease.acquire() is lease
    path = _lock_path(root)
    assert path.stat().st_size == 0
    inode = path.stat().st_ino
    assert lease.owner is None
    assert _status(root) == {"state": "idle", "owner": None}
    lease.release()
    lease.release()
    assert path.exists()
    with lease:
        assert path.stat().st_ino == inode
    assert path.exists()
    assert _probe(root)["acquired"]


def test_begin_persists_active_then_reaped_persists_idle(tmp_path, monkeypatch):
    root = tmp_path / "leases"
    with _lease(root) as lease:
        path = _lock_path(root)
        inode = path.stat().st_ino
        synced = []
        real_fsync = os.fsync

        def capture_fsync(fd):
            synced.append(json.loads(_read_body(path)))
            return real_fsync(fd)

        monkeypatch.setattr(nw.os, "fsync", capture_fsync)
        owner = lease.begin_process()
        assert re.fullmatch(r"[0-9a-f]{32}", owner)
        assert lease.owner == owner
        journal = json.loads(_read_body(path))
        assert journal == synced[-1]
        assert journal["state"] == "active"
        assert journal["owner"] == owner
        assert journal["supervisor_pid"] == os.getpid()
        assert abs(journal["started_at"] - time.time()) < 10
        assert path.stat().st_size <= 4097
        assert not os.get_inheritable(lease._fd)
        lease.process_reaped()
        assert lease.owner is None
        assert synced[-1] == {"version": 1, "state": "idle"}
        assert len(synced) == 2
        assert _status(root) == {"state": "idle", "owner": None}
        assert _probe(root)["code"] == "context.in_use"
        monkeypatch.undo()
    assert path.stat().st_ino == inode
    assert _probe(root)["acquired"]


def test_subprocess_normal_reap_allows_next_writer(tmp_path):
    root = tmp_path / "leases"
    with _holder(root) as (process, _):
        path = _lock_path(root)
        inode = path.stat().st_ino
        process.stdin.write("reap\n")
        process.stdin.flush()
        assert process.wait(timeout=15) == 0
    assert _status(root) == {"state": "idle", "owner": None}
    assert _probe(root)["acquired"]
    assert path.exists() and path.stat().st_ino == inode


def test_killed_supervisor_leaves_active_journal_and_no_pid_or_age_takeover(tmp_path):
    root = tmp_path / "leases"
    with _holder(root) as (process, packet):
        path = _lock_path(root)
        before = _read_body(path)
        process.kill()
        process.wait(timeout=15)
    assert _read_body(path) == before
    assert _status(root) == {"state": "active", "owner": packet["owner"]}
    assert _probe(root)["code"] == "context.recovery_required"
    # Ancient timestamps and a dead PID cannot grant permission either.
    journal = json.loads(before)
    journal["started_at"] = 1
    path.write_bytes(b"\0" + json.dumps(journal).encode("utf-8"))
    contender = _lease(root)
    for _ in range(2):
        with pytest.raises(nw.NativeWriterRecoveryRequired) as error:
            contender.acquire()
        assert error.value.code == "context.recovery_required"
    # Success here proves the refused acquire relinquished the OS lock.
    _recover(root, packet["owner"])
    with contender:
        assert contender.owner is None


def test_release_and_context_exit_never_clear_active(tmp_path):
    root = tmp_path / "leases"
    with pytest.raises(RuntimeError, match="caller cancellation"):
        with _lease(root) as lease:
            owner = lease.begin_process()
            before = _read_body(_lock_path(root))
            raise RuntimeError("caller cancellation")
    lease.release()
    assert _read_body(_lock_path(root)) == before
    assert _status(root) == {"state": "active", "owner": owner}
    assert _probe(root)["code"] == "context.recovery_required"


@pytest.mark.parametrize("consent", [False, None, 0, 1, "true", "yes"])
def test_recovery_requires_explicit_true_not_truthiness(tmp_path, consent):
    root = tmp_path / "leases"
    owner = _orphan(root)
    before = _lock_path(root).read_bytes()
    with pytest.raises(nw.NativeWriterRecoveryRequired):
        _recover(root, owner, consent)
    assert _lock_path(root).read_bytes() == before


@pytest.mark.parametrize("expected", ["", "wrong-owner", "f" * 32, None, 123, "秘密"])
def test_recovery_requires_exact_owner(tmp_path, expected):
    root = tmp_path / "leases"
    owner = _orphan(root)
    assert owner != expected
    before = _lock_path(root).read_bytes()
    with pytest.raises(nw.NativeWriterRecoveryRequired):
        _recover(root, expected)
    assert _lock_path(root).read_bytes() == before
    _recover(root, owner)


def test_recovery_never_breaks_live_lock_even_with_right_owner(tmp_path):
    root = tmp_path / "leases"
    with _holder(root) as (process, packet):
        before = _read_body(_lock_path(root))
        with pytest.raises(nw.NativeWriterBusy):
            _recover(root, packet["owner"])
        assert _read_body(_lock_path(root)) == before
        assert _probe(root)["code"] == "context.in_use"
        assert process.poll() is None


def test_recovery_preserves_file_and_old_owner_cannot_reset_new_run(tmp_path):
    root = tmp_path / "leases"
    old_owner = _orphan(root)
    path = _lock_path(root)
    inode = path.stat().st_ino
    _recover(root, old_owner)
    assert _status(root) == {"state": "idle", "owner": None}
    assert path.stat().st_ino == inode
    new_owner = _orphan(root)
    assert new_owner != old_owner
    with pytest.raises(nw.NativeWriterRecoveryRequired):
        _recover(root, old_owner)
    assert _status(root)["owner"] == new_owner
    _recover(root, new_owner)
    with pytest.raises(nw.NativeWriterRecoveryRequired):
        _recover(root, new_owner)  # Idle is not a matching active invocation.
    assert _probe(root)["acquired"]


def test_status_is_read_only_and_missing_recovery_does_not_create(tmp_path):
    root = tmp_path / "not-created"
    assert _status(root) == {"state": "idle", "owner": None}
    assert not root.exists()
    with pytest.raises(nw.NativeWriterRecoveryRequired):
        _recover(root, "1" * 32)
    assert not root.exists()
    owner = _orphan(root)
    path = _lock_path(root)
    before = path.read_bytes()
    for _ in range(2):
        assert _status(root) == {"state": "active", "owner": owner}
    assert path.read_bytes() == before


_VALID_ACTIVE = {"version": 1, "state": "active", "owner": "a" * 32,
                 "supervisor_pid": 123, "started_at": 1.0}
_BAD_BODIES = [
    b"", b" ", b"{", b'{"version":1,"state":"active"', b"\xff", b"null", b"[]",
    b'{"version":1,"state":"idle","state":"idle"}',
    b'{"version":1,"state":"idle"} trailing',
    json.dumps({"version": True, "state": "idle"}).encode(),
    json.dumps({"version": 1.0, "state": "idle"}).encode(),
    json.dumps({"version": 2, "state": "idle"}).encode(),
    json.dumps({"version": 1, "state": "idle", "owner": None}).encode(),
    json.dumps({"version": 1, "state": "other"}).encode(),
    json.dumps({"version": 1, "state": "active"}).encode(),
    json.dumps(dict(_VALID_ACTIVE, owner="wrong")).encode(),
    json.dumps(dict(_VALID_ACTIVE, owner=None)).encode(),
    json.dumps(dict(_VALID_ACTIVE, supervisor_pid=True)).encode(),
    json.dumps(dict(_VALID_ACTIVE, supervisor_pid=0)).encode(),
    json.dumps(dict(_VALID_ACTIVE, started_at=True)).encode(),
    json.dumps(dict(_VALID_ACTIVE, started_at=-1)).encode(),
    json.dumps(dict(_VALID_ACTIVE, started_at=float("nan"))).encode(),
    json.dumps(dict(_VALID_ACTIVE, started_at=float("inf"))).encode(),
    json.dumps(dict(_VALID_ACTIVE, unexpected="private-output")).encode(),
    b'{"version":1,"state":"idle"}' + b" " * 4096,
    b"[" * 1500 + b"]" * 1500,
]


@pytest.mark.parametrize("body", _BAD_BODIES, ids=lambda body: repr(body[:60]))
def test_malformed_truncated_or_oversized_journal_fails_closed(tmp_path, body):
    root = tmp_path / "leases"
    with _lease(root):
        pass
    path = _lock_path(root)
    path.write_bytes(b"\0" + body)
    contender = _lease(root)
    for _ in range(2):
        with pytest.raises(nw.NativeWriterRecoveryRequired):
            contender.acquire()
    with pytest.raises(nw.NativeWriterRecoveryRequired):
        _status(root)
    with pytest.raises(nw.NativeWriterRecoveryRequired):
        _recover(root, "a" * 32)
    assert path.read_bytes() == b"\0" + body
    # Only this test-created, known-no-child corruption is repaired directly,
    # to verify that every rejected operation closed its file and lock.
    path.write_bytes(b'\0{"version":1,"state":"idle"}')
    with contender:
        pass


def test_exact_maximum_journal_is_accepted(tmp_path):
    root = tmp_path / "leases"
    with _lease(root):
        pass
    body = b'{"version":1,"state":"idle"}'
    _lock_path(root).write_bytes(b"\0" + body.ljust(4096, b" "))
    with _lease(root):
        assert _status(root)["state"] == "idle"


def test_spawn_failed_and_multiple_sequential_processes(tmp_path):
    root = tmp_path / "leases"
    with _lease(root) as lease:
        first = lease.begin_process()
        lease.spawn_failed()  # The test never spawned any child.
        assert _status(root)["state"] == "idle"
        second = lease.begin_process()
        assert first != second
        lease.process_reaped()
    assert _probe(root)["acquired"]


def test_illegal_lifecycle_calls_are_rejected_without_resetting(tmp_path):
    lease = _lease(tmp_path / "leases")
    for method in (lease.begin_process, lease.process_reaped, lease.spawn_failed):
        with pytest.raises(nw.NativeWriterError):
            method()
    with lease:
        with pytest.raises(nw.NativeWriterError):
            lease.acquire()
        with pytest.raises(nw.NativeWriterError):
            lease.process_reaped()
        owner = lease.begin_process()
        with pytest.raises(nw.NativeWriterError):
            lease.begin_process()
        assert lease.owner == owner
        lease.process_reaped()


@pytest.mark.parametrize("operation", ["write", "ftruncate", "fsync"])
def test_begin_persistence_failure_forbids_launch_and_poisoned_reuse(
    tmp_path, monkeypatch, operation,
):
    root = tmp_path / "private-root"
    lease = _lease(root).acquire()
    secret = "stdout-secret stderr-secret C:/private/native/path"

    def fail(*args):
        raise OSError(errno.EIO, secret, str(root))

    launched = False
    try:
        with monkeypatch.context() as patch:
            patch.setattr(nw.os, operation, fail)
            with pytest.raises(nw.NativeWriterError) as error:
                lease.begin_process()
                launched = True
            assert not launched
            assert str(error.value) == "Native writer guard is unavailable."
            rendered = "".join(traceback.format_exception(error.value))
            assert secret not in rendered and str(root) not in rendered
        for method in (lease.begin_process, lease.process_reaped, lease.spawn_failed):
            with pytest.raises(nw.NativeWriterError):
                method()
    finally:
        lease.release()
    assert _lock_path(root).exists()


def test_partial_write_failure_leaves_fail_closed_journal(tmp_path, monkeypatch):
    root = tmp_path / "leases"
    lease = _lease(root).acquire()
    real_write = os.write
    calls = 0

    def partial(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, data[:12])
        raise OSError(errno.ENOSPC, "private details")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(nw.os, "write", partial)
            with pytest.raises(nw.NativeWriterError):
                lease.begin_process()
    finally:
        lease.release()
    assert _probe(root)["code"] == "context.recovery_required"


def test_short_writes_are_completed_and_synced(tmp_path, monkeypatch):
    root = tmp_path / "leases"
    real_write = os.write
    with _lease(root) as lease:
        monkeypatch.setattr(nw.os, "write", lambda fd, data: real_write(fd, data[:3]))
        owner = lease.begin_process()
        assert _status(root)["owner"] == owner
        lease.process_reaped()
    assert _status(root)["state"] == "idle"


def test_failed_idle_persistence_does_not_silently_release_active(tmp_path, monkeypatch):
    root = tmp_path / "leases"
    lease = _lease(root).acquire()
    owner = lease.begin_process()

    def fail(*args):
        raise OSError(errno.EIO, "secret")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(nw.os, "write", fail)
            with pytest.raises(nw.NativeWriterError):
                lease.process_reaped()
    finally:
        lease.release()
    assert _status(root) == {"state": "active", "owner": owner}
    assert _probe(root)["code"] == "context.recovery_required"


def test_recovery_persistence_failure_unlocks_and_preserves_active(tmp_path, monkeypatch):
    root = tmp_path / "leases"
    owner = _orphan(root)

    def fail(*args):
        raise OSError(errno.EIO, "secret")

    with monkeypatch.context() as patch:
        patch.setattr(nw.os, "write", fail)
        with pytest.raises(nw.NativeWriterError):
            _recover(root, owner)
    assert _status(root)["owner"] == owner
    _recover(root, owner)


@pytest.mark.parametrize("family,session", [("", _SESSION), (" \t", _SESSION),
                                           (None, _SESSION), (_FAMILY, ""),
                                           (_FAMILY, " \n"), (_FAMILY, None)])
def test_blank_or_nonstring_keys_are_sanitized(tmp_path, family, session):
    with pytest.raises(nw.NativeWriterError):
        _lease(tmp_path, family, session)


def test_os_errors_and_busy_recovery_errors_are_sanitized(tmp_path, monkeypatch, capsys):
    root = tmp_path / "sensitive-path"
    secret = "private stdout and stderr"

    def fail(*args, **kwargs):
        raise PermissionError(errno.EACCES, secret, str(root))

    with monkeypatch.context() as patch:
        patch.setattr(nw.os, "open", fail)
        for action in (lambda: _lease(root).acquire(), lambda: _status(root),
                       lambda: _recover(root, "a" * 32)):
            with pytest.raises(nw.NativeWriterError) as error:
                action()
            assert secret not in str(error.value) and str(root) not in str(error.value)
            assert error.value.__suppress_context__
    assert capsys.readouterr() == ("", "")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions and directory fsync")
def test_posix_private_permissions_and_creation_directory_sync(tmp_path, monkeypatch):
    root = tmp_path / "leases"
    real_fsync = os.fsync
    synced_directories = []

    def fsync(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            synced_directories.append(info.st_ino)
        return real_fsync(fd)

    monkeypatch.setattr(nw.os, "fsync", fsync)
    with _lease(root) as lease:
        path = _lock_path(root)
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert root.stat().st_ino in synced_directories
        assert tmp_path.stat().st_ino in synced_directories
        lease.begin_process()
        lease.process_reaped()
    # Tighten existing files too, rather than relying on the current umask.
    root.chmod(0o755)
    path.chmod(0o644)
    with _lease(root):
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
