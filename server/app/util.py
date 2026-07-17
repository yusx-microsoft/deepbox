"""Small helpers: ids, tokens, passwords."""
from __future__ import annotations

import hashlib
import secrets
import uuid


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
    salt = secrets.token_hex(8)
    h = hashlib.sha256((salt + pw).encode()).hexdigest()
    return f"{salt}${h}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
    except ValueError:
        return False
    return hashlib.sha256((salt + pw).encode()).hexdigest() == h
