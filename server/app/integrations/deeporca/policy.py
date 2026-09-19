"""DeepOrca-specific Agent, session, input and permission policy."""
from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import HTTPException

from agentbridge.integrations.deeporca.contract import RENDERER_ID
from ..policy import InputRejected, RuntimePolicy
from . import desired

if TYPE_CHECKING:
    from ...models import Agent, Session


class DeepOrcaPolicy(RuntimePolicy):
    renderer = RENDERER_ID
    restore_events = True

    def validate_create_fields(self, body: dict) -> None:
        desired.validate_create_fields(body)

    def validate_local_project(self, project_id: str | None) -> None:
        if not project_id:
            raise HTTPException(422, "DeepOrca requires a registered local project")

    def create_config(self, body: dict) -> dict:
        return desired.create_config(body)

    def initialize_agent(self, agent: Agent) -> None:
        agent.runtime_status = desired.pending_status(agent)

    def validate_agent_update(self, agent: Agent, body: dict) -> None:
        desired.validate_identity_update(agent, body, self.identity_fields)
        self.validate_update_fields(body)

    def require_retry(self) -> None:
        pass

    def retry_agent(self, agent: Agent, body: dict) -> None:
        if body:
            raise HTTPException(422, "runtime retry does not accept configuration or credentials")
        self.initialize_agent(agent)

    def observed_status(self, agent: Agent, status: object) -> dict | None:
        return desired.observed_status(agent, status)

    def status_notification(self, agent: Agent) -> dict:
        return {"type": "agent.runtime_status", "agent_id": agent.id,
                "devbox_id": agent.devbox_id, "runtime": agent.runtime,
                "renderer": self.renderer, "runtime_status": agent.runtime_status}

    def validate_project_migration(self, agent: Agent, project_id: str) -> None:
        if agent.local_project_id != project_id:
            raise HTTPException(409, "DeepOrca binding is immutable; create a new Agent/binding")

    def validate_project_removal(self) -> None:
        raise HTTPException(409, "cannot remove a project bound to DeepOrca; delete the Agent/binding first")

    def session_fields(self, agent: Agent) -> dict:
        return {**super().session_fields(agent), "surface": "structured",
                "model": (agent.runtime_config or {}).get("model")}

    def session_surface(self, surface: str | None, *, error_status: int = 422) -> str:
        if surface == "terminal":
            raise HTTPException(error_status, "DeepOrca only supports structured sessions")
        return "structured"

    def stored_surface(self, surface: str | None) -> str:
        return "structured"

    def command_rejection(self, kind: str) -> dict | None:
        if kind == "permission":
            return {"type": "error", "code": "approval_not_supported",
                    "message": "DeepOrca does not support approval responses"}
        return None

    def input_rejection(self, sess: Session, frame: dict, code: str, message: str) -> dict:
        # Correlate the original browser ID, not a canonicalized UUID. Do not
        # stringify malformed objects or echo input/options on validation errors.
        input_id = frame.get("client_input_id")
        return {"type": "input_ack", "status": "rejected",
                "client_input_id": input_id[:128] if isinstance(input_id, str) else None,
                "session_id": sess.id, "agent_id": sess.agent_id,
                "reason": code, "code": code, "message": message}

    def client_input_id(self, frame: dict) -> str:
        if "client_input_id" not in frame:
            return super().client_input_id(frame)
        input_id = frame["client_input_id"]
        if not isinstance(input_id, str) or not 1 <= len(input_id) <= 128:
            raise ValueError("invalid client_input_id")
        # Keep accepted spellings so ACKs settle the exact optimistic browser key.
        UUID(input_id)
        return input_id

    def validate_input_options(self, sess: Session, frame: dict) -> None:
        options = frame.get("options", {})
        if (frame.keys() - {"type", "session_id", "agent_id", "client_input_id", "data", "options"}
                or not isinstance(options, dict) or options.keys() - {"model"}):
            raise InputRejected(self.input_rejection(
                sess, frame, "invalid_options", "DeepOrca only supports text input and model options"))
        try:
            # Validate the safe model schema, but never add binding defaults to
            # per-turn options or import Connector/SDK implementation code.
            desired.runtime_config(options)
        except HTTPException as exc:
            raise InputRejected(self.input_rejection(
                sess, frame, "invalid_options", exc.detail)) from None

    def acknowledged_input_id(self, original: str, canonical: str) -> str | None:
        return original if len(original) <= 128 else None
