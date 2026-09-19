"""Path-free DeepOrca desired-state contract shared by server and Connector.

This module deliberately depends only on the standard library. It never imports
the optional runtime, resolves local files, or accepts executable configuration.
"""
from __future__ import annotations

import hashlib
import json
import re

RUNTIME_ID = "deeporca"
RENDERER_ID = "deeporca-chat-v1"
EMBEDDED_API_VERSION = 1

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def validate_runtime_config(value: object) -> dict:
    """Return a fresh normalized config, or raise a safe, path-free ValueError.

    Omission is represented by an empty object. Explicit nulls, booleans in
    integer fields and unknown nested keys are not silently coerced.
    """
    if not isinstance(value, dict):
        raise ValueError("DeepOrca runtime_config must be an object")
    if value.keys() - {"integration_version", "profile", "model"}:
        raise ValueError("unsupported DeepOrca runtime_config field")
    version = value.get("integration_version", EMBEDDED_API_VERSION)
    if type(version) is not int or version != EMBEDDED_API_VERSION:
        raise ValueError("DeepOrca integration_version must be 1")
    profile = value.get("profile", {})
    if not isinstance(profile, dict):
        raise ValueError("DeepOrca profile must be an object")
    mode = profile.get("mode", "create")
    if mode == "bind":
        raise ValueError("DeepOrca bind mode is not supported until native profile lock support is available")
    if profile.keys() - {"mode", "configuration_template_ref"}:
        raise ValueError("unsupported DeepOrca profile field")
    if mode != "create":
        raise ValueError("DeepOrca profile mode must be create")
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
    return result


def binding_revision(agent_id: str, local_project_id: str, runtime_config: dict) -> str:
    """Stable binding identity; excludes display names and observed status."""
    if (not isinstance(agent_id, str) or not agent_id or len(agent_id) > 64
            or not isinstance(local_project_id, str) or not local_project_id
            or len(local_project_id) > 64):
        raise ValueError("DeepOrca binding requires agent and local project identifiers")
    desired = {"agent_id": agent_id, "local_project_id": local_project_id,
               "runtime_config": validate_runtime_config(runtime_config)}
    canonical = json.dumps(desired, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
