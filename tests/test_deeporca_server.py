"""Focused contract tests for the Server's stateless runtime policy seam."""
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import UUID

from fastapi import HTTPException
import pytest

from server.app.integrations import InputRejected, RuntimePolicy, runtime_policy
from server.app.integrations.deeporca import DeepOrcaPolicy
from server.app.integrations.deeporca import desired


ROOT = Path(__file__).resolve().parents[1]
INPUT_ID = "{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}"
SESSION = SimpleNamespace(id="session", agent_id="agent")


def agent(runtime="deeporca"):
    return SimpleNamespace(id="agent", devbox_id="box", runtime=runtime,
                           local_project_id="project", runtime_config={}, runtime_status=None)


def test_server_extension_imports_without_app_database_connector_or_sdk(tmp_path):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AGENTBRIDGE_", "DEEPBOX_"))}
    env.update(PYTHON_DOTENV_DISABLED="1", DEEPBOX_ENV="test",
               DEEPBOX_DATABASE_URL=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
               DEEPBOX_DATA_DIR=str(tmp_path))
    code = "\n".join((
        "import sys",
        "from server.app.integrations import runtime_policy",
        "assert runtime_policy('deeporca').renderer == 'deeporca-chat-v1'",
        "assert 'server.app.main' not in sys.modules",
        "assert 'server.app.models' not in sys.modules",
        "assert 'server.app.hub' not in sys.modules",
        "assert not any(n == 'connector' or n.startswith('connector.') for n in sys.modules)",
        "assert not any(n == 'deeporca' or n.startswith('deeporca.') for n in sys.modules)",
    ))
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "test.db").exists()


def test_platform_main_has_no_deeporca_contract_or_runtime_branches():
    source = (ROOT / "server/app/main.py").read_text(encoding="utf-8")
    assert "deeporca" not in source.lower()
    assert "from .integrations import InputRejected, runtime_policy" in source


@pytest.mark.parametrize("runtime", [None, "mock", "claude-code", "codex", "copilot", "future-runtime"])
def test_default_policy_preserves_existing_runtime_behavior(runtime):
    policy = runtime_policy(runtime)
    assert type(policy) is RuntimePolicy
    record = agent(runtime)
    policy.validate_create_fields({"cwd": "legacy-path", "launch_cmd": "legacy-command"})
    policy.validate_local_project(None)
    config = {"custom_cli_setting": "unchanged"}
    assert policy.create_config({"runtime_config": config}) is config
    policy.initialize_agent(record)
    assert record.runtime_status is None
    assert policy.status_notification(record) is None
    assert policy.observed_status(record, {"state": "ready"}) is None
    policy.validate_project_migration(record, "other-project")
    policy.validate_project_removal()
    for surface in (None, "structured", "terminal"):
        assert policy.session_surface(surface) == surface
        assert policy.stored_surface(surface) == surface
    assert not policy.restore_events
    assert policy.session_fields(record) == {
        "runtime": runtime, "runtime_status": None, "renderer": None}
    assert policy.command_rejection("permission") is None
    frame = {"client_input_id": INPUT_ID, "data": "text", "options": {"permission_mode": "plan"},
             "cli_only_field": True}
    assert policy.prepare_input(SESSION, frame) == (str(UUID(INPUT_ID)), "text")
    assert policy.acknowledged_input_id(INPUT_ID, str(UUID(INPUT_ID))) == str(UUID(INPUT_ID))
    with pytest.raises(InputRejected) as failure:
        policy.prepare_input(SESSION, {"client_input_id": "not-a-uuid"})
    assert failure.value.frame == {"type": "error", "message": "invalid client_input_id"}
    with pytest.raises(HTTPException) as failure:
        policy.require_retry()
    assert failure.value.status_code == 422


def test_desired_status_policy_has_no_persistence_or_notification_side_effects():
    policy = runtime_policy("deeporca")
    assert isinstance(policy, DeepOrcaPolicy)
    record = agent()
    policy.initialize_agent(record)
    pending = deepcopy(record.runtime_status)
    ready = {"state": "ready", "revision": pending["revision"]}
    assert policy.observed_status(record, ready) == ready
    assert record.runtime_status == pending
    assert policy.status_notification(record) == {
        "type": "agent.runtime_status", "agent_id": "agent", "devbox_id": "box",
        "runtime": "deeporca", "renderer": "deeporca-chat-v1", "runtime_status": pending}
    for bad in (None, [], {**ready, "revision": "stale"}, {**ready, "state": []},
                {**ready, "state": "unknown"}, {**ready, "detail": "private-path"},
                {**ready, "code": None}, {**ready, "code": "private/path"}):
        assert policy.observed_status(record, bad) is None
    assert record.runtime_status == pending
    record.runtime_status = ready
    policy.retry_agent(record, {})
    assert record.runtime_status == pending
    with pytest.raises(HTTPException) as failure:
        policy.retry_agent(record, {"credentials": "do-not-accept"})
    assert failure.value.status_code == 422
    record.local_project_id = None
    with pytest.raises(HTTPException) as failure:
        desired.pending_status(record)
    assert failure.value.status_code == 409


def test_deeporca_input_and_renderer_policy_do_not_modify_request_options():
    policy = runtime_policy("deeporca")
    frame = {"type": "input", "session_id": "session", "client_input_id": INPUT_ID,
             "data": "text", "options": {"model": "safe-model"}}
    original = deepcopy(frame)
    assert policy.prepare_input(SESSION, frame) == (INPUT_ID, "text")
    assert frame == original  # binding defaults are not per-turn input options
    assert policy.acknowledged_input_id(INPUT_ID, str(UUID(INPUT_ID))) == INPUT_ID
    assert policy.acknowledged_input_id("x" * 129, "unused") is None
    assert policy.command_rejection("permission") == {
        "type": "error", "code": "approval_not_supported",
        "message": "DeepOrca does not support approval responses"}
    assert policy.command_rejection("input") is None
    assert policy.restore_events
    assert policy.stored_surface("terminal") == "structured"
    assert policy.session_surface(None) == "structured"
    for status in (400, 422):
        with pytest.raises(HTTPException) as failure:
            policy.session_surface("terminal", error_status=status)
        assert failure.value.status_code == status


@pytest.mark.parametrize("change,code,message", [
    ({"client_input_id": None}, "invalid_input_id", "invalid client_input_id"),
    ({"client_input_id": "x" * 200}, "invalid_input_id", "invalid client_input_id"),
    ({"data": []}, "invalid_data", "invalid input data"),
    ({"options": {"approval_mode": "never"}}, "invalid_options",
     "DeepOrca only supports text input and model options"),
    ({"options": {"model": "../private"}}, "invalid_options",
     "DeepOrca model must be a safe identifier of at most 128 characters"),
])
def test_policy_rejections_are_safe_bounded_and_correlated(change, code, message):
    frame = {"type": "stdin", "session_id": "session", "client_input_id": INPUT_ID,
             "data": "private text", **change}
    with pytest.raises(InputRejected) as failure:
        runtime_policy("deeporca").prepare_input(SESSION, frame)
    input_id = frame.get("client_input_id")
    assert failure.value.frame == {
        "type": "input_ack", "status": "rejected", "agent_id": "agent", "session_id": "session",
        "client_input_id": input_id[:128] if isinstance(input_id, str) else None,
        "reason": code, "code": code, "message": message}
