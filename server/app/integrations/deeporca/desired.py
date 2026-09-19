"""DeepOrca's path-free desired binding and provisioning status contract.

No Connector/SDK imports or database access: the platform supplies authorized
Agent records and retains transaction and notification ownership.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from fastapi import HTTPException

from agentbridge.integrations.deeporca.contract import binding_revision, validate_runtime_config

if TYPE_CHECKING:
    from ...models import Agent


def runtime_config(value: object) -> dict:
    try:
        return validate_runtime_config(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def validate_create_fields(body: dict) -> None:
    if body.keys() - {"handle", "display_name", "runtime", "local_project_id", "runtime_config"}:
        raise HTTPException(422, "unsupported DeepOrca Agent field; paths and commands are local-only")


def create_config(body: dict) -> dict:
    config = runtime_config(body.get("runtime_config", {}))
    if (not isinstance(body.get("handle"), str) or not body["handle"].strip()
            or len(body["handle"]) > 64):
        raise HTTPException(422, "handle must be a nonempty string of at most 64 characters")
    if "display_name" in body and (not isinstance(body["display_name"], str)
                                  or len(body["display_name"]) > 200):
        raise HTTPException(422, "display_name must be a string of at most 200 characters")
    return config


def validate_identity_update(agent: Agent, body: dict, identity_fields: set[str]) -> None:
    for key in identity_fields & body.keys():
        value = runtime_config(body[key]) if key == "runtime_config" else body[key]
        if value != getattr(agent, key):
            raise HTTPException(409, "DeepOrca binding is immutable; create a new Agent/binding")


def pending_status(agent: Agent) -> dict:
    try:
        revision = binding_revision(agent.id, agent.local_project_id, agent.runtime_config)
    except ValueError:
        raise HTTPException(409, "invalid DeepOrca binding; create a new Agent/binding") from None
    return {"state": "pending", "revision": revision}


def observed_status(agent: Agent, status: object) -> dict | None:
    """Validate a report *after* the platform checks current Connector ownership."""
    if not isinstance(status, dict) or status.keys() - {"state", "code", "revision"}:
        return None
    state = status.get("state")
    if not isinstance(state, str) or state not in {
            "pending", "provisioning", "ready", "needs_configuration", "error"}:
        return None
    code = status.get("code")
    if "code" in status and (not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code)):
        return None
    try:
        revision = binding_revision(agent.id, agent.local_project_id, agent.runtime_config)
    except ValueError:
        return None
    if status.get("revision") != revision:
        return None
    accepted = {"state": state, "revision": revision}
    if code is not None:
        accepted["code"] = code
    return accepted
