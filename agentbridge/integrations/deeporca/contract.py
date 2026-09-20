"""Path-free DeepOrca desired-state contract shared by server and Connector.

This module deliberately depends only on the standard library. It never imports
the optional runtime, resolves local files, or accepts executable configuration.
"""
from __future__ import annotations

import hashlib
import json
import re
import base64
import binascii
from urllib.parse import urlsplit

RUNTIME_ID = "deeporca"
RENDERER_ID = "deeporca-chat-v1"
EMBEDDED_API_VERSION = 1

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_LLM_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+\-]{0,255}\Z")
REASONING_EFFORTS = ("", "none", "minimal", "low", "medium", "high", "xhigh", "max")
MAX_CONTEXT_WINDOW = 2**53 - 1


def validate_llm_config(value: object) -> dict:
    """Validate declarative model settings, never paths or provider credentials.

    URLs are consumed only on the Connector. Private/loopback endpoints are
    intentional for locally hosted models; the Server never probes these URLs.
    Strings are not rewritten because the endpoint authenticates sealed keys.
    """
    fields = {"provider", "base_url", "model", "context_window", "reasoning_effort"}
    if not isinstance(value, dict) or value.keys() - fields:
        raise ValueError("DeepOrca llm must contain only supported model settings")
    if value.get("provider", "openai") != "openai":
        raise ValueError("DeepOrca web configuration requires an OpenAI-compatible provider")
    endpoint = value.get("base_url")
    if (not isinstance(endpoint, str) or not 1 <= len(endpoint) <= 2048
            or not endpoint.isascii()
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in endpoint)
            or "\\" in endpoint or "?" in endpoint or "#" in endpoint
            or "${" in endpoint):
        raise ValueError("DeepOrca endpoint must be an HTTP(S) base URL without credentials, query or fragment")
    try:
        url = urlsplit(endpoint)
        port = url.port
        valid = (url.scheme in {"http", "https"} and bool(url.hostname)
                 and url.username is None and url.password is None
                 and (port is None or 0 < port <= 65535)
                 and bool(re.fullmatch(r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]+)?", url.netloc))
                 and bool(re.fullmatch(r"[A-Za-z0-9._~!$&'()*+,;=:@/%-]*", url.path))
                 and not re.search(r"%(?![0-9A-Fa-f]{2})", url.path))
        if valid and ":" not in url.hostname:
            valid = all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                        for label in url.hostname.rstrip(".").split("."))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("DeepOrca endpoint must be an HTTP(S) base URL without credentials, query or fragment")
    model = value.get("model")
    if not isinstance(model, str) or not _LLM_MODEL.fullmatch(model):
        raise ValueError("DeepOrca model must be a provider model identifier of at most 256 characters")
    window = value.get("context_window")
    if type(window) is not int or not 0 < window <= MAX_CONTEXT_WINDOW:
        raise ValueError("DeepOrca context window must be a positive safe integer token count")
    effort = value.get("reasoning_effort", "")
    if not isinstance(effort, str) or effort not in REASONING_EFFORTS:
        raise ValueError("DeepOrca reasoning effort is not supported")
    return {"provider": "openai", "base_url": endpoint, "model": model,
            "context_window": window, "reasoning_effort": effort}


def validate_credential(value: object) -> dict:
    """Accept an explicit keyless choice or a bounded opaque sealed envelope.

    The Server has no private key. Raw API keys, arbitrary encryption schemes
    and attacker-selected local storage paths are not configuration fields.
    """
    if isinstance(value, dict) and value == {"mode": "none"}:
        return {"mode": "none"}
    fields = {"mode", "key_id", "wrapped_key", "iv", "ciphertext"}
    if (not isinstance(value, dict) or value.keys() != fields
            or value.get("mode") != "sealed"
            or not isinstance(value.get("key_id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["key_id"])):
        raise ValueError("DeepOrca credential must be a sealed Connector envelope or explicit no authentication")
    for key, minimum, maximum in (("wrapped_key", 256, 256), ("iv", 12, 12),
                                  ("ciphertext", 17, 4112)):
        text = value[key]
        if not isinstance(text, str) or len(text) > ((maximum + 2) // 3) * 4:
            raise ValueError("invalid DeepOrca sealed credential")
        try:
            raw = base64.b64decode(text, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("invalid DeepOrca sealed credential") from None
        if (not minimum <= len(raw) <= maximum
                or base64.b64encode(raw).decode("ascii") != text):
            raise ValueError("invalid DeepOrca sealed credential")
    return dict(value)


def validate_runtime_config(value: object) -> dict:
    """Return a fresh normalized config, or raise a safe, path-free ValueError.

    Omission is represented by an empty object. Explicit nulls, booleans in
    integer fields and unknown nested keys are not silently coerced.
    """
    if not isinstance(value, dict):
        raise ValueError("DeepOrca runtime_config must be an object")
    if value.keys() - {"integration_version", "profile", "model", "llm", "credential"}:
        raise ValueError("unsupported DeepOrca runtime_config field")
    version = value.get("integration_version", EMBEDDED_API_VERSION)
    if type(version) is not int or version != EMBEDDED_API_VERSION:
        raise ValueError("DeepOrca integration_version must be 1")
    profile = value.get("profile", {})
    if not isinstance(profile, dict):
        raise ValueError("DeepOrca profile must be an object")
    mode = profile.get("mode", "create")
    if mode == "bind":
        if profile.keys() != {"mode", "profile_ref", "native_stopped"}:
            raise ValueError("DeepOrca bind requires a local profile reference and native-stop confirmation")
        ref = profile["profile_ref"]
        if not isinstance(ref, str) or not re.fullmatch(r"native-[0-9a-f]{32}", ref):
            raise ValueError("DeepOrca existing profile must be a Connector-advertised reference")
        if profile["native_stopped"] is not True:
            raise ValueError("Stop native DeepOrca and confirm it stays stopped while the Connector owns this profile")
        if value.keys() & {"model", "llm", "credential"}:
            raise ValueError("Bound DeepOrca profile configuration is read-only; model and credential overrides are not accepted")
        return {"integration_version": version, "profile": {
            "mode": "bind", "profile_ref": ref, "native_stopped": True}}
    if profile.keys() - {"mode", "configuration_template_ref"}:
        raise ValueError("unsupported DeepOrca profile field")
    if mode != "create":
        raise ValueError("DeepOrca profile mode must be create or bind")
    template = profile.get("configuration_template_ref", "connector-default")
    if template != "connector-default":
        raise ValueError("DeepOrca configuration_template_ref must be connector-default")
    result = {"integration_version": version, "profile": {
        "mode": "create", "configuration_template_ref": "connector-default"}}
    if "model" in value:
        model = value["model"]
        if not isinstance(model, str) or not _MODEL.fullmatch(model) or ".." in model:
            raise ValueError("DeepOrca model must be a safe identifier of at most 128 characters")
        result["model"] = model
    if "llm" in value:
        result["llm"] = validate_llm_config(value["llm"])
        if "model" in result and result["model"] != result["llm"]["model"]:
            raise ValueError("DeepOrca legacy model override conflicts with profile model")
    if "credential" in value:
        if "llm" not in value:
            raise ValueError("DeepOrca credential requires complete model settings")
        result["credential"] = validate_credential(value["credential"])
    return result


def binding_revision(agent_id: str, local_project_id: str, runtime_config: dict) -> str:
    """Desired revision; includes model settings, excludes names/observed status."""
    if (not isinstance(agent_id, str) or not agent_id or len(agent_id) > 64
            or not isinstance(local_project_id, str) or not local_project_id
            or len(local_project_id) > 64):
        raise ValueError("DeepOrca binding requires agent and local project identifiers")
    desired = {"agent_id": agent_id, "local_project_id": local_project_id,
               "runtime_config": validate_runtime_config(runtime_config)}
    canonical = json.dumps(desired, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def binding_identity_revision(agent_id: str, local_project_id: str, runtime_config: dict) -> str:
    """Immutable profile/project identity, independent of editable LLM settings."""
    config = validate_runtime_config(runtime_config)
    config.pop("llm", None)
    config.pop("credential", None)
    return binding_revision(agent_id, local_project_id, config)
