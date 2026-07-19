"""Structured security audit helpers for the DeepBox server.

Security-relevant actions (logins, permission changes, connector
registration, administrative operations, ...) deserve a consistent,
machine-parseable audit trail that is distinct from ordinary application
logging. This module builds on :mod:`server.app.logging` conventions --
one JSON object per line, structured ``extra=`` fields, never raise from
logging -- and adds two audit-specific guarantees:

* **Redaction.** Audit events routinely carry request metadata (headers,
  bodies, query parameters). Those payloads frequently contain secrets.
  :func:`audit_event` walks the supplied metadata recursively and replaces
  the value of any field whose name looks like a secret (``password``,
  ``token``, ``secret``, ``cookie``, ``authorization``, ``api_key``, ...)
  with a fixed placeholder. Redaction is best-effort but defensive: it
  matches on substrings so ``x-api-key`` and ``refreshToken`` are caught.

* **Never throw into request handling.** Emitting an audit event must never
  break the operation being audited. Every public entry point swallows and
  self-logs its own failures, so a malformed payload or a logging backend
  hiccup cannot turn a successful request into a 500.

The canonical event shape is::

    {
      "event": "auth.login",
      "actor": {...},
      "target": {...},
      "outcome": "success" | "failure" | "error" | "denied" | ...,
      "request": {...},   # redacted
      "audit": true,      # marker so audit lines can be filtered/routed
      ... any additional structured fields ...
    }
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

from .logging import log_event

# Dedicated logger name so operators can route/retain audit lines separately
# from the noisier application logger tree.
AUDIT_LOGGER = "deepbox.audit"

_LOGGER = logging.getLogger(AUDIT_LOGGER)

# Placeholder substituted for any redacted value.
REDACTED = "***redacted***"

# Substrings that mark a field name as sensitive. Matching is case-insensitive
# and substring-based so compound names (``x-api-key``, ``refresh_token``,
# ``set-cookie``) are covered without an exhaustive list.
_SECRET_SUBSTRINGS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "cookie",
    "authorization",
    "auth",
    "api_key",
    "apikey",
    "api-key",
    "access_key",
    "private_key",
    "credential",
    "session",
    "otp",
    "passphrase",
)

# Guard against pathological / cyclic structures blowing the stack.
_MAX_DEPTH = 12


def _is_secret_key(key: Any) -> bool:
    """Return ``True`` if a mapping key name looks sensitive."""

    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_SUBSTRINGS)


def redact(value: Any, *, _depth: int = 0, _seen: frozenset[int] = frozenset()) -> Any:
    """Recursively copy ``value`` with secret-looking fields redacted.

    * Mapping values whose key looks sensitive are replaced with
      :data:`REDACTED`.
    * Other mappings, lists, tuples and sets are traversed recursively.
    * Cyclic references and excessive depth are handled gracefully by
      returning a placeholder rather than recursing forever.

    The input is never mutated; a redacted copy is returned.
    """

    if _depth > _MAX_DEPTH:
        return "***max-depth***"

    if isinstance(value, Mapping):
        marker = id(value)
        if marker in _seen:
            return "***cyclic***"
        seen = _seen | {marker}
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _is_secret_key(key):
                result[name] = REDACTED
            else:
                result[name] = redact(item, _depth=_depth + 1, _seen=seen)
        return result

    # Treat strings/bytes as scalars, not iterables of characters.
    if isinstance(value, (str, bytes, bytearray)):
        return value

    if isinstance(value, (list, tuple)):
        return [redact(item, _depth=_depth + 1, _seen=_seen) for item in value]

    if isinstance(value, (set, frozenset)):
        return [redact(item, _depth=_depth + 1, _seen=_seen) for item in value]

    return value


def _clean(value: Any) -> Any | None:
    """Redact a metadata value, returning ``None`` when there is nothing to log.

    ``None`` results are dropped by :func:`server.app.logging.log_event`, which
    keeps empty ``actor``/``target``/``request`` keys out of the record.
    """

    if value is None:
        return None
    redacted = redact(value)
    if isinstance(redacted, (Mapping, list, tuple)) and not redacted:
        return None
    return redacted


def audit_event(
    event: str,
    *,
    actor: Any | None = None,
    target: Any | None = None,
    outcome: str | None = None,
    request: Mapping[str, Any] | None = None,
    logger: logging.Logger | None = None,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Emit a structured, redacted audit event. Never raises.

    Parameters
    ----------
    event:
        Dotted event name, e.g. ``"auth.login"`` or ``"connector.register"``.
    actor:
        Who performed the action (user id, service principal, connector id,
        or a mapping describing them). Redacted before logging.
    target:
        What the action was performed on. Redacted before logging.
    outcome:
        Result of the action, e.g. ``"success"``, ``"failure"``, ``"denied"``.
    request:
        Request metadata (method, path, headers, query, ...). Redacted
        recursively, so ``Authorization`` headers and ``password`` bodies are
        never written to the log.
    logger:
        Optional logger override (defaults to the dedicated audit logger).
    level:
        Log level (defaults to ``INFO``).
    **fields:
        Any additional structured fields; also redacted.
    """

    try:
        target_logger = logger if logger is not None else _LOGGER
        payload: dict[str, Any] = {
            "audit": True,
            "actor": _clean(actor),
            "target": _clean(target),
            "outcome": outcome,
            "request": _clean(request),
        }
        for key, value in fields.items():
            payload[key] = _clean(value)
        log_event(target_logger, event, level=level, **payload)
    except Exception:  # noqa: BLE001 - auditing must never break the caller.
        # Best-effort self-report; guarded so even *this* cannot raise.
        try:
            _LOGGER.exception("audit.emit_failed", extra={"event": event, "audit": True})
        except Exception:  # noqa: BLE001
            pass


def redact_headers(headers: Iterable[tuple[str, Any]] | Mapping[str, Any]) -> dict[str, Any]:
    """Convenience helper: redact an HTTP header collection into a plain dict.

    Accepts either a mapping or an iterable of ``(name, value)`` pairs (as
    exposed by many ASGI/WSGI frameworks). Never raises; returns ``{}`` on
    unexpected input.
    """

    try:
        if isinstance(headers, Mapping):
            items = headers.items()
        else:
            items = headers
        result: dict[str, Any] = {}
        for name, value in items:
            key = str(name)
            result[key] = REDACTED if _is_secret_key(key) else value
        return result
    except Exception:  # noqa: BLE001
        return {}
