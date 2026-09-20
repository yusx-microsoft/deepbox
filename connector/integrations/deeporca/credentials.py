"""Machine-private sealing key. Only the isolated worker decrypts credentials.

The public-key pin is durable before a key can be advertised. Missing/corrupt
material is never healed by generating a replacement. The state database also
records the public key ID, so losing both adjacent files is not a silent reset.
Cryptography is imported lazily so CLI-only Connectors remain usable.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from .events import IntegrationError

ALGORITHM = "RSA-OAEP-256+A256GCM"
AAD_PREFIX = "agentbridge/deeporca/credential/v1\0"


def key_path(state_path=None) -> Path:
    """Only machine code may select the state path; never runtime_config."""
    if state_path is None:
        from ...local_store import default_state_path
        state_path = default_state_path()
    if str(state_path) == ":memory:":
        raise IntegrationError("credential_unavailable")
    return Path(os.path.abspath(state_path)).with_name("deeporca-credential.pem")


def _check_path(path):
    # Do not resolve() first: that would erase evidence of a symlink/junction.
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("unsafe path")


def _private(path):
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    # Windows chmod does not restrict read access. Protect the DACL, granting
    # access only to the current token user and SYSTEM (not inherited groups).
    import ctypes
    from ctypes import wintypes
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    token = wintypes.HANDLE()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    sid_text = wintypes.LPWSTR()
    descriptor = ctypes.c_void_p()
    try:
        if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise OSError()
        size = wintypes.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        data = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, data, size, ctypes.byref(size)):
            raise OSError()
        sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
        if not adv.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise OSError()
        sddl = f"D:P(A;;FA;;;SY)(A;;FA;;;{sid_text.value})"
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None):
            raise OSError()
        if not adv.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise OSError()
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        if sid_text:
            kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        if token:
            kernel.CloseHandle(token)


def _open(path, flags):
    _check_path(path)
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
    try:
        info = os.fstat(fd)
        current = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError("unsafe file")
        _check_path(path)
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _lock(path):
    lock_path = path.with_suffix(".lock")
    fd = _open(lock_path, os.O_RDWR | os.O_CREAT)
    try:
        _private(lock_path)
        if os.name == "nt":
            import msvcrt
            # locking beyond EOF is supported and does not expose key material.
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read(path, limit):
    with os.fdopen(_open(path, os.O_RDONLY), "rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("oversize file")
    _private(path)
    return data


def _atomic(path, data):
    _check_path(path)
    fd, name = tempfile.mkstemp(prefix=".deeporca-key-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            _private(temp)  # restrict before writing any secret
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _check_path(path)
        os.replace(temp, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def _key_id(key):
    from cryptography.hazmat.primitives import serialization
    der = key.public_key().public_bytes(serialization.Encoding.DER,
                                       serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


def _load(path, *, create=False, expected_key_id=None):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    path = Path(path)
    _check_path(path)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pin = path.with_suffix(".key-id")
    with _lock(path):
        _check_path(pin)
        if not path.exists() and not pin.exists() and create and expected_key_id is None:
            # A crash during first initialization fails closed on the next boot.
            _atomic(pin, b"pending")
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            _atomic(path, key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            _atomic(pin, _key_id(key).encode("ascii"))
        else:
            key = serialization.load_pem_private_key(_read(path, 16 * 1024), password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 2048:
            raise ValueError("wrong key")
        if _read(pin, 64).decode("ascii") != _key_id(key):
            raise ValueError("key pin mismatch")
        if expected_key_id is not None and expected_key_id != _key_id(key):
            raise ValueError("state pin mismatch")
        return key


def public_key_info(state_path=None) -> dict:
    """Provision once, publish only a standards-compatible encrypt-only JWK."""
    try:
        if state_path is None:
            from ...local_store import default_state_path
            state_path = default_state_path()
        path = key_path(state_path)
        state_path = Path(os.path.abspath(state_path))
        _check_path(path)
        _check_path(state_path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Public metadata only. A durable state pin makes accidental loss of
        # both key files fail closed, even before an old sealed envelope arrives.
        conn = sqlite3.connect(state_path, timeout=15)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS deeporca_credential_key "
                         "(id INTEGER PRIMARY KEY CHECK(id=1), key_id TEXT NOT NULL)")
            row = conn.execute("SELECT key_id FROM deeporca_credential_key WHERE id=1").fetchone()
            key = _load(path, create=True, expected_key_id=row[0] if row else None)
            conn.execute("INSERT OR IGNORE INTO deeporca_credential_key VALUES(1,?)", (_key_id(key),))
            if conn.execute("SELECT key_id FROM deeporca_credential_key WHERE id=1").fetchone()[0] != _key_id(key):
                raise ValueError("state pin mismatch")
            conn.commit()
        finally:
            conn.close()
        numbers = key.public_key().public_numbers()
        def integer(value):
            return base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, "big")).decode("ascii").rstrip("=")
        return {"version": 1, "algorithm": ALGORITHM, "key_id": _key_id(key),
                "public_key": {"kty": "RSA", "n": integer(numbers.n), "e": integer(numbers.e),
                               "alg": "RSA-OAEP-256", "ext": True, "key_ops": ["encrypt"]}}
    except Exception:
        raise IntegrationError("credential_unavailable") from None


def decrypt_credential(envelope: dict, base_url: str, *, private_key_path=None) -> str:
    """Worker-only operation: never creates keys, never returns diagnostic text."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = _load(private_key_path or key_path(), create=False)
        key_id = _key_id(key)
        if envelope["mode"] != "sealed" or envelope["key_id"] != key_id:
            raise ValueError("wrong key")
        wrapped = base64.b64decode(envelope["wrapped_key"], validate=True)
        iv = base64.b64decode(envelope["iv"], validate=True)
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        if len(wrapped) != 256 or len(iv) != 12 or not 16 <= len(ciphertext) <= 4112:
            raise ValueError("invalid envelope")
        aes_key = key.decrypt(wrapped, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                                   algorithm=hashes.SHA256(), label=None))
        if len(aes_key) != 32:
            raise ValueError("wrong AES key")
        aad = (AAD_PREFIX + key_id + "\0" + base_url).encode("utf-8")
        value = AESGCM(aes_key).decrypt(iv, ciphertext, aad).decode("utf-8")
        if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("invalid credential")
        return value
    except Exception:
        raise IntegrationError("credential_unavailable") from None


def runtime_configuration(config: dict, *, private_key_path=None):
    """Return plaintext only inside the worker; absence retains local settings."""
    if "llm" not in config and "credential" not in config:
        return None
    result = dict(config.get("llm", {}))
    credential = config.get("credential")
    if credential is not None:
        if credential["mode"] == "none":
            result["api_key"] = ""
        else:
            result["api_key"] = decrypt_credential(credential, result["base_url"],
                                                   private_key_path=private_key_path)
    return result
