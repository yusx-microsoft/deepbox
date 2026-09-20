"""Browser profile configuration is declarative and contains no raw secrets."""
import base64
from copy import deepcopy

import pytest

from agentbridge.integrations.deeporca.contract import (
    MAX_CONTEXT_WINDOW, REASONING_EFFORTS, binding_identity_revision,
    binding_revision, validate_runtime_config,
)


def llm(**changes):
    return {"provider": "openai", "base_url": "http://127.0.0.1:1234/v1",
            "model": "vendor/model:latest", "context_window": 128000,
            "reasoning_effort": "", **changes}


def envelope(**changes):
    return {"mode": "sealed", "key_id": "a" * 64,
            "wrapped_key": base64.b64encode(b"w" * 256).decode(),
            "iv": base64.b64encode(b"i" * 12).decode(),
            "ciphertext": base64.b64encode(b"c" * 17).decode(), **changes}


def test_legacy_default_and_revision_are_unchanged():
    old = {"integration_version": 1, "profile": {
        "mode": "create", "configuration_template_ref": "connector-default"}}
    assert validate_runtime_config({}) == old
    assert binding_identity_revision("agent", "project", {}) == binding_revision("agent", "project", old)


@pytest.mark.parametrize("effort", REASONING_EFFORTS)
def test_full_configuration_and_native_efforts(effort):
    value = {"llm": llm(reasoning_effort=effort), "credential": envelope()}
    before = deepcopy(value)
    parsed = validate_runtime_config(value)
    assert parsed["llm"] == value["llm"]
    assert parsed["credential"] == value["credential"]
    parsed["llm"]["model"] = "different"
    parsed["credential"]["key_id"] = "b" * 64
    assert value == before


@pytest.mark.parametrize("endpoint", [
    "https://api.openai.com/v1", "http://localhost:11434/v1/",
    "http://[::1]:8000/v1", "https://models.internal/api/v1",
])
def test_private_endpoints_are_supported_without_network_requests(endpoint):
    assert validate_runtime_config({"llm": llm(base_url=endpoint)})["llm"]["base_url"] == endpoint


@pytest.mark.parametrize("endpoint", [
    None, True, "", "file:///tmp/model", "javascript:alert(1)",
    "https://user:secret@host/v1", "https://user@host/v1", "https://host/v1?key=secret",
    "https://host/v1?", "https://host/v1#", "https://host/v1#secret", "https:///v1",
    "https://host:99999/v1", "https://host:0/v1", "https://[invalid/v1",
    "https://host/\\other", "https://host/v1\n", " https://host/v1",
    "https://${LLM_HOST}/v1", "https://host/" + "a" * 2048,
    "https://my_llm.invalid/v1", "https://bad..invalid/v1", "https://-invalid/v1",
    "https://host/v1/%", "https://host/v1/{bad}", "https://éxample.invalid/v1",
])
def test_endpoint_rejections_never_echo_values(endpoint):
    with pytest.raises(ValueError) as error:
        validate_runtime_config({"llm": llm(base_url=endpoint)})
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("window", [None, True, False, 0, -1, 1.5, "128000", MAX_CONTEXT_WINDOW + 1])
def test_context_window_exact_native_type_and_bounds(window):
    with pytest.raises(ValueError):
        validate_runtime_config({"llm": llm(context_window=window)})


@pytest.mark.parametrize("window", [1, 128000, MAX_CONTEXT_WINDOW])
def test_context_window_valid_boundaries(window):
    assert validate_runtime_config({"llm": llm(context_window=window)})["llm"]["context_window"] == window


@pytest.mark.parametrize("model", [None, "", "model with spaces", "${LLM_MODEL}", "m\n", "x" * 257])
def test_invalid_model(model):
    with pytest.raises(ValueError):
        validate_runtime_config({"llm": llm(model=model)})


@pytest.mark.parametrize("model", ["vendor/model@revision", "model+variant"])
def test_provider_model_aliases_match_native_sdk(model):
    assert validate_runtime_config({"llm": llm(model=model)})["llm"]["model"] == model


@pytest.mark.parametrize("bad", [None, [], {"provider": "copilot"}, {"reasoning_effort": "auto"},
                                 {"reasoning_effort": None}, {"api_key": "raw-secret"},
                                 {"command": "do-not-execute"}, {"profile_path": "C:/private"}])
def test_llm_rejects_unknown_or_incomplete_fields(bad):
    value = {"llm": llm(**bad) if isinstance(bad, dict) else bad}
    with pytest.raises(ValueError) as error:
        validate_runtime_config(value)
    assert "raw-secret" not in str(error.value)


@pytest.mark.parametrize("credential", [
    None, "raw-secret", {"mode": "none", "api_key": "raw-secret"},
    {"mode": "plaintext", "api_key": "raw-secret"}, envelope(key_id="x" * 64),
    envelope(iv="not base64!"), envelope(iv=base64.b64encode(b"i" * 11).decode()),
    envelope(wrapped_key=base64.b64encode(b"w" * 257).decode()),
    envelope(ciphertext=base64.b64encode(b"c" * 4113).decode()),
    envelope(ciphertext=base64.b64encode(b"c" * 16).decode()),
    envelope(ciphertext="é"), {**envelope(), "path": "C:/private"},
])
def test_credential_only_accepts_bounded_canonical_ciphertext(credential):
    with pytest.raises(ValueError) as error:
        validate_runtime_config({"llm": llm(), "credential": credential})
    assert "raw-secret" not in str(error.value)


def test_no_auth_explicit_and_credential_requires_model_config():
    assert validate_runtime_config({"llm": llm(), "credential": {"mode": "none"}})["credential"] == {"mode": "none"}
    with pytest.raises(ValueError):
        validate_runtime_config({"credential": envelope()})


def test_desired_revision_changes_without_changing_native_profile_identity():
    initial = {"llm": llm(), "credential": envelope()}
    changed = {"llm": llm(model="new/model", reasoning_effort="high"), "credential": {"mode": "none"}}
    assert binding_revision("agent", "project", initial) != binding_revision("agent", "project", changed)
    assert binding_identity_revision("agent", "project", initial) == binding_identity_revision("agent", "project", changed)
    assert binding_identity_revision("agent", "project", initial) != binding_identity_revision("agent", "other", initial)


def test_legacy_model_override_remains_immutable_and_cannot_conflict():
    assert binding_identity_revision("agent", "project", {"model": "one"}) != binding_identity_revision("agent", "project", {"model": "two"})
    with pytest.raises(ValueError):
        validate_runtime_config({"model": "one", "llm": llm(model="two")})
