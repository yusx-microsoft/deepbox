"""Tests for the structured security audit helper.

These are pure tests: they exercise the redaction logic directly and capture
emitted log records via pytest's ``caplog`` fixture. No server, database or
network is involved.
"""
import logging

import pytest

from server.app.audit import (
    AUDIT_LOGGER,
    REDACTED,
    audit_event,
    redact,
    redact_headers,
)


# --- redaction unit tests --------------------------------------------------

def test_redact_top_level_secret_keys():
    data = {"username": "alice", "password": "hunter2", "api_key": "abc"}
    out = redact(data)
    assert out["username"] == "alice"
    assert out["password"] == REDACTED
    assert out["api_key"] == REDACTED


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "passwd",
        "secret",
        "token",
        "refreshToken",
        "access_token",
        "Cookie",
        "Set-Cookie",
        "Authorization",
        "x-api-key",
        "apikey",
        "private_key",
        "credential",
        "session_id",
        "otp",
        "passphrase",
    ],
)
def test_secret_key_variants_are_redacted(key):
    out = redact({key: "sensitive"})
    assert out[key] == REDACTED


def test_redact_is_recursive():
    data = {
        "level1": {
            "token": "t",
            "level2": {"password": "p", "keep": "ok"},
            "items": [{"secret": "s"}, {"public": "v"}],
        }
    }
    out = redact(data)
    assert out["level1"]["token"] == REDACTED
    assert out["level1"]["level2"]["password"] == REDACTED
    assert out["level1"]["level2"]["keep"] == "ok"
    assert out["level1"]["items"][0]["secret"] == REDACTED
    assert out["level1"]["items"][1]["public"] == "v"


def test_redact_does_not_mutate_input():
    data = {"password": "hunter2", "nested": {"token": "x"}}
    redact(data)
    assert data["password"] == "hunter2"
    assert data["nested"]["token"] == "x"


def test_redact_handles_non_string_keys():
    out = redact({1: "a", ("t",): "b"})
    assert out["1"] == "a"


def test_redact_strings_not_treated_as_iterable():
    assert redact("hello") == "hello"
    assert redact(b"bytes") == b"bytes"


def test_redact_handles_cycles():
    data: dict = {"name": "x"}
    data["self"] = data
    out = redact(data)
    assert out["name"] == "x"
    assert out["self"] == "***cyclic***"


def test_redact_depth_guard():
    node: dict = {}
    cur = node
    for _ in range(30):
        cur["child"] = {}
        cur = cur["child"]
    # Should not raise; deep tail collapses to a placeholder.
    out = redact(node)
    assert out is not None


def test_redact_headers_from_pairs():
    out = redact_headers([("Authorization", "Bearer x"), ("Accept", "json")])
    assert out["Authorization"] == REDACTED
    assert out["Accept"] == "json"


def test_redact_headers_from_mapping():
    out = redact_headers({"cookie": "sid=1", "host": "example"})
    assert out["cookie"] == REDACTED
    assert out["host"] == "example"


def test_redact_headers_bad_input_returns_empty():
    assert redact_headers(123) == {}  # type: ignore[arg-type]


# --- audit_event emission tests --------------------------------------------

def test_audit_event_emits_record(caplog):
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        audit_event(
            "auth.login",
            actor={"user": "alice"},
            target={"resource": "devbox-1"},
            outcome="success",
        )
    records = [r for r in caplog.records if getattr(r, "event", None) == "auth.login"]
    assert len(records) == 1
    rec = records[0]
    assert rec.audit is True
    assert rec.actor == {"user": "alice"}
    assert rec.target == {"resource": "devbox-1"}
    assert rec.outcome == "success"


def test_audit_event_redacts_request_metadata(caplog):
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        audit_event(
            "connector.register",
            actor={"connector_id": "c1", "token": "should-hide"},
            request={
                "method": "POST",
                "path": "/register",
                "headers": {"Authorization": "Bearer xyz", "Accept": "json"},
                "body": {"password": "p", "name": "n"},
            },
            outcome="success",
        )
    rec = next(r for r in caplog.records if getattr(r, "event", None) == "connector.register")
    assert rec.actor["connector_id"] == "c1"
    assert rec.actor["token"] == REDACTED
    assert rec.request["headers"]["Authorization"] == REDACTED
    assert rec.request["headers"]["Accept"] == "json"
    assert rec.request["body"]["password"] == REDACTED
    assert rec.request["body"]["name"] == "n"


def test_audit_event_drops_empty_and_none(caplog):
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        audit_event("plain.event", actor=None, target={}, request=None)
    rec = next(r for r in caplog.records if getattr(r, "event", None) == "plain.event")
    assert not hasattr(rec, "actor")
    assert not hasattr(rec, "target")
    assert not hasattr(rec, "request")
    assert rec.audit is True


def test_audit_event_extra_fields_redacted(caplog):
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        audit_event("x.event", detail={"api_key": "k", "keep": "v"})
    rec = next(r for r in caplog.records if getattr(r, "event", None) == "x.event")
    assert rec.detail["api_key"] == REDACTED
    assert rec.detail["keep"] == "v"


def test_audit_event_respects_level(caplog):
    with caplog.at_level(logging.WARNING, logger=AUDIT_LOGGER):
        audit_event("denied.event", outcome="denied", level=logging.WARNING)
    rec = next(r for r in caplog.records if getattr(r, "event", None) == "denied.event")
    assert rec.levelno == logging.WARNING
    assert rec.outcome == "denied"


def test_audit_event_never_raises(caplog):
    class Boom:
        def __repr__(self):
            raise RuntimeError("no repr")

        def items(self):  # make it look mapping-ish then explode
            raise RuntimeError("no items")

    # Should not raise even with a hostile payload.
    audit_event("bad.event", actor=Boom(), target=Boom(), request={"x": Boom()})


def test_audit_event_uses_custom_logger(caplog):
    logger = logging.getLogger("custom.audit.logger")
    with caplog.at_level(logging.INFO, logger="custom.audit.logger"):
        audit_event("custom.event", logger=logger, outcome="success")
    assert any(
        getattr(r, "event", None) == "custom.event" and r.name == "custom.audit.logger"
        for r in caplog.records
    )


if __name__ == "__main__":
    import pytest as _pytest

    raise SystemExit(_pytest.main([__file__, "-v"]))
