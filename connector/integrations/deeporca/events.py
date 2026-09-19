"""Bounded, versioned projections of the embedded SDK's native events.

There is deliberately no approval response protocol. A request for one is a
broken SDK contract, not a UI state. Native ``done`` is NOT a commit marker.
"""
from __future__ import annotations

import json
import re

MAX_TEXT_BYTES = 64 * 1024
MAX_NATIVE_BYTES = 48 * 1024
MAX_EVENT_BYTES = 128 * 1024
MAX_TURN_BYTES = 16 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9_:.\-]{1,128}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:@+\-]{0,255}\Z")


class IntegrationError(ValueError):
    """Only stable diagnostic codes cross the worker boundary."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def validate_id(value, code="invalid_input_id") -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value) or value in (".", ".."):
        raise IntegrationError(code)
    return value


def validate_native_id(value, code="invalid_protocol") -> str:
    # Provider call IDs are opaque, not filenames. In particular '/', '+',
    # and '=' can be part of a perfectly valid provider-issued call ID.
    if (not isinstance(value, str) or not value or len(value) > 256
            or any(ord(char) < 32 for char in value)):
        raise IntegrationError(code)
    return value


def validate_input(text, options=None, *, client_input_id=None) -> dict:
    """Validate the browser allowlist; return a fresh normalized options dict.

    No attachments, permissions, environment, paths, or arbitrary SDK kwargs
    are accepted. This synchronous helper is also suitable for preflight.
    """
    if client_input_id is not None:
        validate_id(client_input_id)
    if not isinstance(text, str) or not text.strip() or "\x00" in text:
        raise IntegrationError("invalid_text")
    try:
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise IntegrationError("input_too_large")
    except UnicodeError:
        raise IntegrationError("invalid_text") from None
    # Escaped control characters can be much larger on the JSON wire than
    # their UTF-8 input. Reject before durable admission, not during the echo.
    if len(json_bytes({"text": text})) > MAX_TEXT_BYTES + 32:
        raise IntegrationError("input_too_large")
    if options is None:
        options = {}
    if not isinstance(options, dict) or set(options) - {"model"}:
        raise IntegrationError("unsupported_options")
    model = options.get("model")
    if model is not None and (not isinstance(model, str) or not _MODEL.fullmatch(model)):
        raise IntegrationError("invalid_model")
    return {"model": model} if model is not None else {}


def json_bytes(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise IntegrationError("invalid_protocol") from None


def _preview(value, budget=8192):
    raw = value if isinstance(value, str) else json_bytes(value).decode("utf-8")
    return raw.encode("utf-8")[:budget].decode("utf-8", errors="ignore")


def prepare_native_event(event: dict) -> dict:
    """Validate identity and bound output before IPC or durable recording.

    Large tool results get an explicitly labelled display preview. Native
    model context remains entirely the SDK's responsibility.
    """
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise IntegrationError("invalid_protocol")
    kind = event["type"]
    lowered = kind.lower()
    if "approval" in lowered or "permission" in lowered or lowered == "control_request":
        raise IntegrationError("approval_contract_violation")
    validate_native_id(event.get("turn_id"))
    seq = event.get("sequence")
    if type(seq) is not int or seq < 0:
        raise IntegrationError("invalid_protocol")
    encoded = json_bytes(event)
    # Make a copy so an SDK reusing mutable event dictionaries cannot change
    # already recorded events, and so test transports obey JSON semantics too.
    if len(encoded) <= MAX_NATIVE_BYTES:
        return json.loads(encoded)
    if kind not in ("tool_result", "tool.result"):
        raise IntegrationError("output_limit")
    keep = ("type", "turn_id", "sequence", "tool_id", "tool_call_id", "call_id",
            "id", "name", "tool", "tool_name", "parent_id", "parent_tool_id",
            "message_id", "is_error", "error", "blocked", "status", "code")
    bounded = {key: event[key] for key in keep if key in event}
    content = event.get("content", event.get("result", event.get("output", "")))
    bounded.update(content=_preview(content), truncated=True, original_bytes=len(encoded))
    if len(json_bytes(bounded)) > MAX_NATIVE_BYTES:
        raise IntegrationError("output_limit")
    return bounded


def translate_deeporca_event(event: dict) -> list[dict]:
    """Project one native event, preserving its typed envelope and call IDs."""
    event = prepare_native_event(event)
    kind = event["type"]
    if kind in ("done", "turn.end", "turn_end"):
        return []
    native = dict(event, runtime="deeporca", schema="deeporca.turn.v1")
    out = {"turn_id": event["turn_id"], "turn_seq": event["sequence"],
           "event_id": f"{event['turn_id']}:{event['sequence']}", "native": native}
    for key in ("message_id", "parent_id", "parent_tool_id"):
        if key in event:
            out[key] = event[key]
    if kind in ("text", "thinking", "text.delta", "thinking.delta"):
        text = event.get("content", event.get("text", ""))
        if not isinstance(text, str):
            raise IntegrationError("invalid_protocol")
        out.update(ev="thinking.delta" if kind.startswith("thinking") else "message.delta",
                   text=text)
    elif kind in ("tool_call", "tool.call", "tool_result", "tool.result"):
        tool_id = next((event[k] for k in ("tool_call_id", "call_id", "tool_id", "id")
                        if event.get(k)), None)
        validate_native_id(tool_id, "missing_tool_id")
        out.update(ev="tool.call" if kind in ("tool_call", "tool.call") else "tool.result",
                   tool_id=tool_id,
                   tool=event.get("name", event.get("tool", event.get("tool_name"))))
        if out["ev"] == "tool.call":
            out["input"] = event.get("arguments", event.get("args", event.get("input", {})))
        else:
            out.update(content=event.get("content", event.get("result", event.get("output", ""))),
                       is_error=bool(event.get("is_error") or event.get("error")
                                     or event.get("blocked") or event.get("status") in ("error", "blocked")))
            for key in ("truncated", "original_bytes", "blocked", "code", "status"):
                if key in event:
                    out[key] = event[key]
            if event.get("blocked"):
                out["status"] = "blocked"
    elif kind in ("turn.start", "turn_start", "start"):
        out["ev"] = "turn.start"
    elif kind == "error":
        out.update(ev="error", code=event.get("code", "runtime_error"),
                   message=event.get("message", event.get("content", "DeepOrca turn failed.")))
    else:
        # Status, usage and future extensions retain their native semantics.
        out["ev"] = "status"
        out["subtype"] = kind
        if isinstance(event.get("content"), str):
            out["note"] = event["content"]
    if len(json_bytes(out)) > MAX_EVENT_BYTES:
        raise IntegrationError("output_limit")
    return [out]
