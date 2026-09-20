"""Local single-writer leases for *cooperating AgentBridge* native CLI sessions.

The caller supplies a stable, per-user lease directory, shared by every agent
and working directory. Only the runtime family and session identity form the
key. This does not coordinate manual CLI invocations or uncooperative writers.

Acquire before launch, then call ``begin_process()`` BEFORE spawning. Call
``process_reaped()`` only after the child is confirmed reaped; ``spawn_failed()``
is for a definite no-child spawn OSError, not cancellation or an uncertain
launch outcome. Release never clears an active journal. A dead supervisor may
have left a live child, so neither elapsed time nor PID checks permit takeover.

Files are never unlinked or replaced: the OS lock and bounded journal share one
inode. Byte zero is reserved for the Windows lock, with JSON after it. Instances
are process-local and must not be shared across threads or forked processes.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
import uuid

__all__ = [
    "NativeWriterLease",
    "NativeWriterError",
    "NativeWriterBusy",
    "NativeWriterRecoveryRequired",
    "status",
    "recover_native_writer",
]

_MAX_JOURNAL = 4096
_OWNER = re.compile(r"[0-9a-f]{32}\Z")
_IDLE = {"version": 1, "state": "idle"}


class NativeWriterError(ValueError):
    """Sanitized guard failure; the caller MUST NOT launch a writer."""

    code = "context.writer_unavailable"
    message = "Native writer guard is unavailable."

    def __init__(self) -> None:
        super().__init__(self.message)


class NativeWriterBusy(NativeWriterError):
    """Another cooperating process currently holds the OS lock."""

    code = "context.in_use"
    message = "Native context is already in use."


class NativeWriterRecoveryRequired(NativeWriterError):
    """An active or invalid journal forbids automatic takeover."""

    code = "context.recovery_required"
    message = "Native context requires explicit local recovery."


def _location(root: str, runtime_family: str, session_id: str) -> tuple[str, str]:
    try:
        if not isinstance(runtime_family, str) or not runtime_family.strip():
            raise NativeWriterError()
        if not isinstance(session_id, str) or not session_id.strip():
            raise NativeWriterError()
        # UUID spelling (including case, braces and hyphens) is not identity.
        try:
            identity = str(uuid.UUID(session_id))
        except ValueError:
            identity = session_id  # Nonblank opaque test/legacy identities.
        key = json.dumps([runtime_family, identity], ensure_ascii=True,
                         separators=(",", ":")).encode("ascii")
        directory = os.fspath(root)
        if not isinstance(directory, str) or not directory.strip():
            raise NativeWriterError()
        directory = os.path.abspath(directory)
        path = os.path.join(directory, hashlib.sha256(key).hexdigest() + ".lock")
        return directory, path
    except (OSError, TypeError, ValueError):
        raise NativeWriterError() from None


def _lock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            # Windows permits a byte-range lock beyond EOF on a new empty file.
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            raise NativeWriterBusy() from None
        raise NativeWriterError() from None


def _close(fd: int, *, locked: bool) -> None:
    try:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass  # Closing also releases the lock.
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _sync_directory(directory: str) -> None:
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _open_locked(directory: str, path: str, *, create: bool = True) -> int:
    fd = None
    locked = False
    try:
        if create:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        if create:
            flags |= os.O_CREAT
        fd = os.open(path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NativeWriterError()
        _lock(fd)
        locked = True
        if os.name != "nt":
            os.chmod(directory, 0o700)
            os.fchmod(fd, 0o600)
            os.fsync(fd)
            # Sync even when another opener won the initial creation race.
            # The parent sync also persists a newly-created lease directory.
            _sync_directory(directory)
            _sync_directory(os.path.dirname(directory))
        result, fd = fd, None
        return result
    except FileNotFoundError:
        if not create:
            raise NativeWriterRecoveryRequired() from None
        raise NativeWriterError() from None
    except (OSError, ValueError) as exc:
        if isinstance(exc, NativeWriterError):
            raise
        raise NativeWriterError() from None
    finally:
        if fd is not None:
            _close(fd, locked=locked)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _read_journal(fd: int) -> dict:
    try:
        os.lseek(fd, 1, os.SEEK_SET)
        raw = os.read(fd, _MAX_JOURNAL + 1)
        if not raw and os.fstat(fd).st_size == 0:
            return dict(_IDLE)
        if not raw or len(raw) > _MAX_JOURNAL:
            raise NativeWriterRecoveryRequired()
        journal = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(journal, dict):
            raise NativeWriterRecoveryRequired()
        if type(journal.get("version")) is not int or journal["version"] != 1:
            raise NativeWriterRecoveryRequired()
        if journal.get("state") == "idle" and journal == _IDLE:
            return journal
        if set(journal) != {
            "version", "state", "owner", "supervisor_pid", "started_at"
        } or journal["state"] != "active":
            raise NativeWriterRecoveryRequired()
        owner, pid, started = (journal["owner"], journal["supervisor_pid"],
                               journal["started_at"])
        if not isinstance(owner, str) or _OWNER.fullmatch(owner) is None:
            raise NativeWriterRecoveryRequired()
        if type(pid) is not int or pid <= 0:
            raise NativeWriterRecoveryRequired()
        if type(started) not in (int, float) or not math.isfinite(started) or started <= 0:
            raise NativeWriterRecoveryRequired()
        return journal
    except OSError:
        raise NativeWriterError() from None
    except (ValueError, OverflowError, RecursionError):
        raise NativeWriterRecoveryRequired() from None


def _write_journal(fd: int, journal: dict) -> None:
    try:
        data = json.dumps(journal, separators=(",", ":"), allow_nan=False).encode("ascii")
        if len(data) > _MAX_JOURNAL:
            raise NativeWriterError()
        os.lseek(fd, 1, os.SEEK_SET)
        remaining = memoryview(data)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise NativeWriterError()
            remaining = remaining[written:]
        os.ftruncate(fd, 1 + len(data))
        os.fsync(fd)
    except (OSError, ValueError, OverflowError):
        raise NativeWriterError() from None


class NativeWriterLease:
    """Exclusive local guard; ``acquire()`` returns this initially idle lease.

    Re-acquire after release or a rejected acquisition is supported. Acquiring
    an already-held instance, overlapping begin calls, or reusing an instance
    after a persistence failure without releasing it is forbidden. A successful
    ``begin_process()`` returns the random owner token stored in the journal.
    """

    def __init__(self, root: str, runtime_family: str, session_id: str):
        self._directory, self._path = _location(root, runtime_family, session_id)
        self._fd: int | None = None
        self._owner: str | None = None
        self._failed = False

    @property
    def owner(self) -> str | None:
        """This instance's active owner token, or None while idle/released."""
        return self._owner

    def acquire(self) -> NativeWriterLease:
        if self._fd is not None:
            raise NativeWriterError()
        fd = _open_locked(self._directory, self._path)
        try:
            if _read_journal(fd)["state"] != "idle":
                raise NativeWriterRecoveryRequired()
        except BaseException:
            _close(fd, locked=True)
            raise
        self._fd = fd
        self._owner = None
        self._failed = False
        return self

    def _held_fd(self) -> int:
        if self._fd is None or self._failed:
            raise NativeWriterError()
        return self._fd

    def begin_process(self) -> str:
        """Durably mark active BEFORE launch; any failure forbids launching."""
        fd = self._held_fd()
        if self._owner is not None:
            raise NativeWriterError()
        self._failed = True
        try:
            owner = secrets.token_hex(16)
            started = time.time()
            if not math.isfinite(started) or started <= 0:
                raise NativeWriterError()
            _write_journal(fd, {
                "version": 1, "state": "active", "owner": owner,
                "supervisor_pid": os.getpid(), "started_at": started,
            })
        except (OSError, ValueError, OverflowError):
            raise NativeWriterError() from None
        self._owner = owner
        self._failed = False
        return owner

    def process_reaped(self) -> None:
        """Persist idle ONLY after the child has definitely been reaped."""
        fd = self._held_fd()
        if self._owner is None:
            raise NativeWriterError()
        self._failed = True
        _write_journal(fd, dict(_IDLE))
        self._owner = None
        self._failed = False

    def spawn_failed(self) -> None:
        """Clear active only for a definite no-child spawn OSError."""
        self.process_reaped()

    def release(self) -> None:
        """Idempotently unlock/close, without changing the on-disk journal."""
        fd, self._fd = self._fd, None
        self._owner = None
        self._failed = False
        if fd is not None:
            _close(fd, locked=True)

    def __enter__(self) -> NativeWriterLease:
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def status(root: str, runtime_family: str, session_id: str) -> dict:
    """Return only ``state`` and ``owner`` for local inspection, without writes.

    This is an advisory journal snapshot, NOT proof a writer stopped or that a
    lock is free. It can be read while a lease is held; a concurrent partial
    journal write may fail closed with RecoveryRequired. An absent file is idle.
    No path, PID, timestamp, runtime/session ID, or CLI output is returned.
    """
    _, path = _location(root, runtime_family, session_id)
    fd = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return {"state": "idle", "owner": None}
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NativeWriterError()
        journal = _read_journal(fd)
        return {"state": journal["state"], "owner": journal.get("owner")}
    except (OSError, ValueError) as exc:
        if isinstance(exc, NativeWriterError):
            raise
        raise NativeWriterError() from None
    finally:
        if fd is not None:
            _close(fd, locked=False)


def recover_native_writer(
    root: str, runtime_family: str, session_id: str,
    expected_owner: str, confirm_writer_stopped: bool,
) -> None:
    """Clear a valid active journal with explicit, exact-owner acknowledgment.

    ``confirm_writer_stopped is True`` is the CALLER'S MANUAL ATTESTATION that
    the writer has stopped, not programmatic proof. The helper never kills a
    process, breaks a live OS lock, or uses PID/age as evidence. Invalid journals
    cannot be recovered by this helper. A stale owner token cannot reset a newer
    invocation. Callers must keep this operation local and explicitly confirmed.
    """
    if (confirm_writer_stopped is not True or not isinstance(expected_owner, str)
            or _OWNER.fullmatch(expected_owner) is None):
        raise NativeWriterRecoveryRequired()
    directory, path = _location(root, runtime_family, session_id)
    fd = _open_locked(directory, path, create=False)
    try:
        journal = _read_journal(fd)
        if (journal["state"] != "active"
                or not secrets.compare_digest(journal["owner"], expected_owner)):
            raise NativeWriterRecoveryRequired()
        _write_journal(fd, dict(_IDLE))
    finally:
        _close(fd, locked=True)
