"""Small helpers: ids, tokens, passwords.

Password hashing uses Argon2id (via argon2-cffi). Legacy salted-SHA256
hashes of the form ``sha256$<salt>$<digest>`` -- and the even older bare
``<salt>$<digest>`` form -- are still verifiable so existing credentials keep
working, and callers can transparently upgrade them to Argon2id on the next
successful login.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2 import exceptions as argon2_exceptions
from argon2.low_level import Type

# A single, shared hasher configured for Argon2id. The parameters below are a
# reasonable interactive-login baseline; ``check_needs_rehash`` will flag any
# stored hash that was produced with weaker settings so it can be upgraded.
_PH = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,  # 64 MiB
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

# Prefixes used to recognise the different stored-hash formats.
_ARGON2_PREFIX = "$argon2"
_LEGACY_SHA256_PREFIX = "sha256$"


def new_id() -> str:
    return uuid.uuid4().hex


def new_token() -> tuple[str, str, str]:
    """Return (full_token, sha256_hash_hex, preview)."""
    raw = secrets.token_hex(32)  # 64 hex chars
    full = f"hpc_box_{raw}"
    h = hashlib.sha256(full.encode()).hexdigest()
    preview = f"hpc_box_{raw[:6]}…"
    return full, h, preview


def hash_token(full: str) -> str:
    return hashlib.sha256(full.encode()).hexdigest()


def hash_password(pw: str) -> str:
    """Hash a password with Argon2id.

    API-compatible with the previous implementation: takes the plaintext
    password and returns an opaque string suitable for storage. The returned
    value is a standard PHC-format Argon2id string beginning with ``$argon2``.
    """
    return _PH.hash(pw)


def _is_argon2(stored: str) -> bool:
    return stored.startswith(_ARGON2_PREFIX)


def _legacy_parts(stored: str) -> tuple[str, str] | None:
    """Return (salt, digest) for a recognised legacy SHA256 hash, else None."""
    s = stored
    if s.startswith(_LEGACY_SHA256_PREFIX):
        s = s[len(_LEGACY_SHA256_PREFIX):]
    try:
        salt, digest = s.split("$", 1)
    except ValueError:
        return None
    # A legacy digest is a 64-char lowercase hex sha256; reject anything else so
    # we don't mistake an Argon2/other string for a legacy hash.
    if len(digest) != 64:
        return None
    try:
        int(digest, 16)
    except ValueError:
        return None
    return salt, digest


def _verify_legacy(pw: str, stored: str) -> bool:
    parts = _legacy_parts(stored)
    if parts is None:
        return False
    salt, digest = parts
    calc = hashlib.sha256((salt + pw).encode()).hexdigest()
    # Constant-time comparison of the two digests.
    return hmac.compare_digest(calc, digest)


@dataclass(frozen=True)
class PasswordVerification:
    """Result of :func:`verify_password_ex`.

    Attributes:
        valid: whether the supplied password matched the stored hash.
        needs_upgrade: whether the stored hash should be replaced (either a
            legacy format, or an Argon2 hash produced with outdated
            parameters). Always ``False`` when ``valid`` is ``False``.
        replacement: a fresh Argon2id hash to persist when ``needs_upgrade`` is
            ``True``, otherwise ``None``. Only populated on a successful
            verification.
    """

    valid: bool
    needs_upgrade: bool = False
    replacement: str | None = None

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.valid


def verify_password_ex(pw: str, stored: str) -> PasswordVerification:
    """Pure verification helper.

    Returns a :class:`PasswordVerification` describing whether the password is
    valid and whether the stored hash needs to be upgraded/replaced. This
    function has no side effects: it never mutates any state and leaves it to
    the caller to persist ``replacement`` when appropriate.

    Constant-time behaviour is preserved as far as is practical: Argon2
    verification is inherently constant-time with respect to the digest, and
    legacy verification uses :func:`hmac.compare_digest`.
    """
    if not isinstance(stored, str) or not stored:
        return PasswordVerification(valid=False)

    if _is_argon2(stored):
        try:
            _PH.verify(stored, pw)
        except (
            argon2_exceptions.VerifyMismatchError,
            argon2_exceptions.InvalidHashError,
            argon2_exceptions.VerificationError,
        ):
            return PasswordVerification(valid=False)
        # Valid Argon2 hash: check whether params are out of date.
        try:
            needs = _PH.check_needs_rehash(stored)
        except argon2_exceptions.InvalidHashError:
            needs = True
        if needs:
            return PasswordVerification(
                valid=True, needs_upgrade=True, replacement=hash_password(pw)
            )
        return PasswordVerification(valid=True)

    # Legacy salted-SHA256 formats -> always flag for upgrade to Argon2id.
    if _verify_legacy(pw, stored):
        return PasswordVerification(
            valid=True, needs_upgrade=True, replacement=hash_password(pw)
        )
    return PasswordVerification(valid=False)


def verify_password(pw: str, stored: str) -> bool:
    """Backwards-compatible boolean verify.

    Accepts both Argon2id hashes and the legacy SHA256 formats.
    """
    return verify_password_ex(pw, stored).valid
