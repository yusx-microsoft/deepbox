"""Hermetic DeepOrca desired/observed state and HTTP/WebSocket regressions."""
import asyncio
import datetime as dt
import importlib
import os
from unittest.mock import AsyncMock, patch

import pytest

from agentbridge.integrations.deeporca.contract import (
    EMBEDDED_API_VERSION, RENDERER_ID, RUNTIME_ID,
    binding_revision, validate_runtime_config,
)


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(("AGENTBRIDGE_", "DEEPBOX_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("DEEPBOX_DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("DEEPBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEEPBOX_ENV", "test")
    monkeypatch.setenv("DEEPBOX_REGISTRATION_ENABLED", "true")
    from server.app import config, models, main, live
    from server.app.hub import Hub
    from fastapi.testclient import TestClient

    importlib.reload(config)
    importlib.reload(models)
    importlib.reload(main)
    monkeypatch.setattr(main, "hub", Hub())
    monkeypatch.setattr(live, "DATA_DIR", tmp_path / "recordings")
    live.DATA_DIR.mkdir()
    monkeypatch.setattr(main, "live_registry", live.LiveRegistry())
    with TestClient(main.app) as client:
        assert client.post("/api/auth/register", json={
            "username": "owner", "password": "strong-password"}).status_code == 200
        yield client, main
    for session in main.live_registry._sessions.values():
        session._cast.close()
    models._engine.dispose()


def machine(client, name="box"):
    result = client.post("/api/devboxes", json={"name": name}).json()
    headers = {"authorization": "Bearer " + result["token"]}
    project = name + "-project"
    response = client.post(f"/api/devboxes/{result['devbox']['id']}/projects", headers=headers, json={
        "projects": [{"id": project, "handle": "repo", "name": "Repository"}]})
    assert response.status_code == 200, response.text
    return result["devbox"]["id"], headers, project


def create(client, box, project, **extra):
    return client.post(f"/api/devboxes/{box}/agents", json={
        "handle": "orca", "runtime": RUNTIME_ID, "local_project_id": project, **extra})


def attach(ws, session_id):
    ws.send_json({"type": "attach", "session_id": session_id})
    frames = [ws.receive_json() for _ in range(3)]
    assert {frame["type"] for frame in frames} == {"collaboration", "restore", "status"}
    return frames


def assert_input_rejected(ack, frame, agent_id, code, message):
    input_id = frame.get("client_input_id")
    assert ack == {
        "type": "input_ack", "status": "rejected", "session_id": frame["session_id"],
        "agent_id": agent_id,
        "client_input_id": input_id[:128] if isinstance(input_id, str) else None,
        "reason": code, "code": code, "message": message,
    }


def test_runtime_policy_is_not_consulted_before_http_authorization(app_client):
    client, main = app_client
    box, _, project = machine(client)
    agent = create(client, box, project).json()
    assert client.post("/api/auth/register", json={
        "username": "outsider", "password": "strong-password"}).status_code == 200
    # Deliberately invalid bodies must not reveal runtime policy to nonmembers.
    with patch.object(main, "runtime_policy", side_effect=AssertionError("policy before authorization")):
        assert client.post(f"/api/devboxes/{box}/agents", json={
            "runtime": RUNTIME_ID, "cwd": "private"}).status_code == 404
        assert client.patch(f"/api/agents/{agent['id']}", json={
            "runtime_config": None}).status_code == 404
        assert client.post(f"/api/agents/{agent['id']}/runtime/retry", json={
            "credentials": "private"}).status_code == 404
        assert client.post(f"/api/agents/{agent['id']}/sessions", json={
            "surface": "terminal"}).status_code == 404


def test_shared_contract_default_and_canonical_revision():
    assert (RUNTIME_ID, RENDERER_ID, EMBEDDED_API_VERSION) == ("deeporca", "deeporca-chat-v1", 1)
    config = validate_runtime_config({})
    assert config == {"integration_version": 1, "profile": {
        "mode": "create", "configuration_template_ref": "connector-default"}}
    assert binding_revision("a", "p", {}) == binding_revision("a", "p", config)
    assert len(binding_revision("a", "p", config)) == 16
    assert binding_revision("a", "p", config) != binding_revision("b", "p", config)
    assert binding_revision("a", "p", config) != binding_revision("a", "q", config)
    assert binding_revision("a", "p", config) != binding_revision("a", "p", {"model": "model-1"})
    config["profile"]["mode"] = "bad"
    assert validate_runtime_config({})["profile"]["mode"] == "create"


INVALID_CONFIGS = [
    None, [], "path", {"integration_version": True}, {"integration_version": 1.0},
    {"integration_version": 2}, {"integration_version": "1"},
    {"cwd": "C:/private"}, {"api_key": "secret"}, {"approval_mode": "never"},
    {"isolation": False}, {"command": "python"}, {"python_module": "evil"},
    {"profile": None}, {"profile": []}, {"profile": {"mode": "bind"}},
    {"profile": {"mode": "create", "path": "C:/private"}},
    {"profile": {"configuration_template_ref": "C:/private"}},
    {"profile": {"mode": True}}, {"model": "../secret"}, {"model": "C:\\private"},
    {"model": "model;exec"}, {"model": "x" * 129}, {"model": True},
]


@pytest.mark.parametrize("config", INVALID_CONFIGS)
def test_shared_contract_rejects_unknown_or_unsafe_configuration(config):
    with pytest.raises(ValueError):
        validate_runtime_config(config)


def test_create_validation_pending_directory_and_offline_sessions(app_client):
    client, main = app_client
    box, headers, project = machine(client)
    for config in INVALID_CONFIGS:
        response = create(client, box, project, runtime_config=config)
        assert response.status_code == 422, (config, response.text)
        assert "C:/private" not in response.text
    for key in ("cwd", "launch_cmd", "renderer", "runtime_status", "api_key"):
        assert create(client, box, project, **{key: "not-allowed"}).status_code == 422
    assert create(client, box, None).status_code == 422
    response = create(client, box, project, runtime_config={"model": "model-1"})
    assert response.status_code == 200, response.text
    agent = response.json()
    revision = binding_revision(agent["id"], project, agent["runtime_config"])
    assert agent["runtime_status"] == {"state": "pending", "revision": revision}
    assert agent["renderer"] == RENDERER_ID
    assert agent["runtime_config"]["model"] == "model-1"
    directory = client.get("/api/me", headers=headers).json()["agents"]
    assert directory[0]["runtime_status"] == agent["runtime_status"]
    assert directory[0]["renderer"] == RENDERER_ID
    assert client.get("/api/devboxes").json()[0]["agents"][0] == agent
    assert client.get(f"/api/agents/{agent['id']}").json() == agent
    response = client.post(f"/api/agents/{agent['id']}/sessions", json={})
    assert response.status_code == 200
    session = response.json()
    assert session["surface"] == "structured"
    assert session["renderer"] == RENDERER_ID
    assert session["model"] == "model-1"
    assert session["runtime_status"] == agent["runtime_status"]
    persisted = client.get(f"/api/sessions/{session['id']}").json()
    # SQLite drops timezone information on reload, unlike the freshly-created
    # timestamp; compare instants without weakening the runtime-field checks.
    assert dt.datetime.fromisoformat(persisted["created_at"]).replace(tzinfo=dt.timezone.utc) == (
        dt.datetime.fromisoformat(session["created_at"]))
    assert persisted == {**session, "created_at": persisted["created_at"]}
    assert client.get(f"/api/agents/{agent['id']}/sessions").json() == [persisted]
    assert client.post(f"/api/agents/{agent['id']}/sessions", json={"surface": "terminal"}).status_code == 422
    with client.websocket_connect("/ws/term", headers={"origin": "http://testserver"}) as ws:
        ws.send_json({"type": "attach", "session_id": session["id"]})
        frames = [ws.receive_json() for _ in range(3)]
        assert next(f for f in frames if f["type"] == "restore")["kind"] == "event"
        status = next(f for f in frames if f["type"] == "status")
        assert status["state"] == "offline" and status["renderer"] == RENDERER_ID
        assert next(f for f in frames if f["type"] == "collaboration")["keyboard"]["required"] is False


def test_permissions_project_isolation_and_retry(app_client):
    client, main = app_client
    box, _, project = machine(client)
    other_box, _, other_project = machine(client, "other")
    assert create(client, box, other_project).status_code == 422
    agent = create(client, box, project).json()
    retry = f"/api/agents/{agent['id']}/runtime/retry"
    assert client.post(retry, json={"api_key": "secret"}).status_code == 422
    with main.models.SessionLocal() as db:
        owner_id = db.get(main.Devbox, box).owner_user_id
        workspace_id = db.get(main.Devbox, box).workspace_id
    from fastapi.testclient import TestClient
    with TestClient(main.app) as outsider:
        assert outsider.post("/api/auth/register", json={
            "username": "outsider", "password": "strong-password"}).status_code == 200
        assert create(outsider, box, project).status_code == 404
        assert outsider.post(retry).status_code == 404
        assert outsider.get(f"/api/agents/{agent['id']}").status_code == 404
    with main.models.SessionLocal() as db:
        membership = db.scalar(main.select(main.Membership).where(
            main.Membership.workspace_id == workspace_id, main.Membership.user_id == owner_id))
        for role in ("viewer", "operator"):
            membership.role = role
            db.commit()
            # Workspace role denials intentionally conceal the resource.
            assert create(client, box, project, handle="blocked").status_code == 404
            assert client.post(retry).status_code == 404
        membership.role = "admin"
        db.commit()
    with patch.object(main, "_push_agent_directory", new_callable=AsyncMock) as push:
        result = client.post(retry)
        assert result.status_code == 200
        assert result.json()["runtime_status"] == agent["runtime_status"]
        push.assert_awaited_once_with(box)


def test_observed_status_ws_spoof_stale_and_sanitization(app_client):
    client, main = app_client
    box, headers, project = machine(client)
    other_box, _, other_project = machine(client, "other")
    agent = create(client, box, project).json()
    other = create(client, other_box, other_project).json()
    generic = create(client, box, project, handle="generic", runtime="mock").json()
    revision = agent["runtime_status"]["revision"]
    with client.websocket_connect("/ws/devbox", headers=headers) as connector:
        assert connector.receive_json()["type"] == "hello"
        assert connector.receive_json()["type"] == "agents"
        with patch.object(main.hub, "to_users", new_callable=AsyncMock) as broadcast:
            invalid = [
                {"state": "ready", "revision": "stale"},
                {"state": "ready", "revision": revision, "path": "private"},
                {"state": "ready", "revision": revision, "code": "C:/secret"},
                {"state": "ready", "revision": revision, "code": "exception text"},
                {"state": "ready", "revision": revision, "code": "x" * 65},
                {"state": "unknown", "revision": revision},
                {"state": [], "revision": revision},
            ]
            for status in invalid:
                connector.send_json({"type": "agent.runtime_status", "agent_id": agent["id"],
                                     "runtime_status": status})
            for target in (other, generic):
                connector.send_json({"type": "agent.runtime_status", "agent_id": target["id"],
                    "runtime_status": {"state": "ready", "revision": (
                        other["runtime_status"]["revision"] if target is other else revision)}})
            connector.send_json({"type": "heartbeat"})
            assert connector.receive_json()["type"] == "heartbeat_ack"
            broadcast.assert_not_awaited()
            assert client.get(f"/api/agents/{agent['id']}").json()["runtime_status"] == agent["runtime_status"]
            status = {"state": "needs_configuration", "code": "missing_credentials", "revision": revision}
            connector.send_json({"type": "agent.runtime_status", "agent_id": agent["id"],
                                 "runtime_status": status, "renderer": "https://evil.invalid"})
            connector.send_json({"type": "heartbeat"})
            assert connector.receive_json()["type"] == "heartbeat_ack"
            assert client.get(f"/api/agents/{agent['id']}").json()["runtime_status"] == status
            broadcast.assert_awaited_once()
            user_ids, notification = broadcast.call_args.args
            with main.models.SessionLocal() as db:
                assert user_ids == {db.get(main.Devbox, box).owner_user_id}
            assert notification["renderer"] == RENDERER_ID
            assert notification["runtime_status"] == status
            # Duplicate observations are idempotent; an old revision cannot
            # overwrite a successfully accepted observation either.
            for observed in (status, {"state": "error", "revision": "stale"}):
                connector.send_json({"type": "agent.runtime_status", "agent_id": agent["id"],
                                     "runtime_status": observed})
            connector.send_json({"type": "heartbeat"})
            assert connector.receive_json()["type"] == "heartbeat_ack"
            broadcast.assert_awaited_once()
        from server.app.hub import DevboxConn
        superseded = DevboxConn(ws=None, devbox_id=box, agent_ids={agent["id"]})
        with main.models.SessionLocal() as db:
            assert asyncio.run(main._accept_runtime_status(db, superseded, {
                "agent_id": agent["id"], "runtime_status": {"state": "error", "revision": revision}})) is False
            # Database ownership is independently enforced even if a cached
            # directory were to contain an agent belonging to another device.
            superseded.agent_ids.add(other["id"])
            with patch.object(main.hub, "is_current_devbox", return_value=True):
                assert asyncio.run(main._accept_runtime_status(db, superseded, {
                    "agent_id": other["id"], "runtime_status": {
                        "state": "ready", "revision": other["runtime_status"]["revision"]}})) is False
    # Persisted readiness survives transport loss and is not conflated with presence.
    assert client.get(f"/api/agents/{agent['id']}").json()["runtime_status"] == status
    assert client.get(f"/api/agents/{other['id']}").json()["runtime_status"] == other["runtime_status"]
    assert client.get(f"/api/agents/{generic['id']}").json()["runtime_status"] is None


def test_binding_immutable_delete_only_pushes_directory_and_generic_unchanged(app_client):
    client, main = app_client
    box, headers, project = machine(client)
    agent = create(client, box, project, runtime_config={"model": "model-1"}).json()
    url = f"/api/agents/{agent['id']}"
    assert client.patch(url, json={"display_name": "Renamed"}).json()["display_name"] == "Renamed"
    for change in ({"local_project_id": "other"}, {"runtime": "mock"},
                   {"runtime_config": {"model": "model-2"}}):
        assert client.patch(url, json=change).status_code == 409
    assert client.patch(url, json={"runtime_config": {"profile": {"mode": "bind"}}}).status_code == 422
    assert client.get(url).json()["runtime_status"] == agent["runtime_status"]
    assert client.post(f"/api/devboxes/{box}/projects", headers=headers, json={"projects": []}).status_code == 409
    generic = create(client, box, project, handle="generic", runtime="mock",
                     runtime_config={"custom": "kept"}, cwd="legacy-local", launch_cmd="custom").json()
    assert generic["runtime_config"] == {"custom": "kept"}
    assert generic["runtime_status"] is None and generic["renderer"] is None
    assert client.post(f"/api/agents/{generic['id']}/sessions", json={}).json()["surface"] is None
    assert client.post(f"/api/agents/{generic['id']}/runtime/retry").status_code == 422
    # Real reconciliation proves deletion emits only desired-directory removal,
    # never a native profile/file-purge instruction.
    with client.websocket_connect("/ws/devbox", headers=headers) as connector:
        assert connector.receive_json()["type"] == "hello"
        assert connector.receive_json()["type"] == "agents"
        late = create(client, box, project, handle="late").json()
        assert late["presence"] == "online"
        assert connector.receive_json()["type"] == "agents"
        assert client.delete(url).status_code == 200
        frame = connector.receive_json()
        assert frame["type"] == "agents"
        assert {a["id"] for a in frame["agents"]} == {generic["id"], late["id"]}
        # Disconnect without another inbound frame: the WebSocket ORM session
        # still holds the deleted agent and has never loaded the new one.
    with main.models.SessionLocal() as db:
        assert db.get(main.Agent, agent["id"]) is None
        assert db.get(main.Agent, generic["id"]).presence == "offline"
        survivor = db.get(main.Agent, late["id"])
        assert survivor.presence == "offline"
        assert survivor.runtime_status == late["runtime_status"]
    assert client.get(url).status_code == 404
    assert client.get("/api/devboxes").json()[0]["projects"][0]["id"] == project


def test_runtime_descriptors_preserve_agent_configuration_and_features(app_client):
    client, main = app_client
    box, headers, _ = machine(client)
    other_box, _, _ = machine(client, "other")
    descriptor = {
        "id": RUNTIME_ID, "available": True, "surface": "structured", "renderer": RENDERER_ID,
        "agent_config": {"integration_version": 1, "profile_modes": ["create"],
                         "local_project_required": True},
        "features": {"agent_binding": True, "native_profile_bind": False},
    }
    capabilities = {"runtimes": [descriptor]}
    assert client.post(f"/api/devboxes/{other_box}/runtimes", headers=headers,
                       json={"capabilities": capabilities}).status_code == 403
    assert client.post(f"/api/devboxes/{box}/runtimes", headers=headers,
                       json={"capabilities": capabilities}).status_code == 200

    def reported():
        return next(d for d in client.get("/api/devboxes").json() if d["id"] == box)["capabilities"]

    assert reported() == capabilities
    descriptor["features"]["native_profile_bind"] = True
    descriptor["agent_config"]["profile_modes"].append("bind")
    with client.websocket_connect("/ws/devbox", headers=headers) as connector:
        assert connector.receive_json()["type"] == "hello"
        assert connector.receive_json()["type"] == "agents"
        connector.send_json({"type": "runtimes", "capabilities": capabilities})
        connector.send_json({"type": "heartbeat"})
        assert connector.receive_json()["type"] == "heartbeat_ack"
        assert reported() == capabilities
    with main.models.SessionLocal() as db:
        assert db.get(main.Devbox, box).capabilities == capabilities
        assert db.get(main.Devbox, other_box).capabilities is None


def test_previous_database_migrates_runtime_status_and_deeporca_surface(app_client):
    client, main = app_client
    box, _, project = machine(client)
    agent = create(client, box, project).json()
    sid = client.post(f"/api/agents/{agent['id']}/sessions", json={}).json()["id"]
    generic = create(client, box, project, handle="generic", runtime="mock").json()
    generic_sid = client.post(f"/api/agents/{generic['id']}/sessions", json={}).json()["id"]
    from sqlalchemy import inspect, text
    engine = main.models._engine
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE agent DROP COLUMN runtime_status"))
        conn.execute(text("UPDATE session SET surface=NULL"))
    main.models._migrate(engine)
    main.models._migrate(engine)  # additive/idempotent upgrade
    assert "runtime_status" in {c["name"] for c in inspect(engine).get_columns("agent")}
    with main.models.SessionLocal() as db:
        assert db.get(main.Agent, agent["id"]).runtime_status is None
        assert db.get(main.Session, sid).surface == "structured"
        assert db.get(main.Session, generic_sid).surface is None
    assert client.get(f"/api/sessions/{sid}").json()["renderer"] == RENDERER_ID


def test_deeporca_permission_rejected_after_attachment_and_role_authorization(app_client):
    client, main = app_client
    box, _, project = machine(client)
    agent = create(client, box, project).json()
    sid = client.post(f"/api/agents/{agent['id']}/sessions", json={}).json()["id"]
    permission = {"type": "permission", "session_id": sid,
                  "request_id": "approval-request", "decision": "allow"}
    invalid_input = {"type": "input", "session_id": sid, "data": "hello",
                     "client_input_id": "00000000-0000-0000-0000-000000000001",
                     "options": {"api_key": "sensitive-api-key"}}
    barrier = {"type": "input", "session_id": sid, "client_input_id": "invalid"}
    read_only = {"type": "error", "code": "read_only",
                 "message": "session control requires current operator access and attachment"}
    with patch.object(main.hub, "to_devbox", new_callable=AsyncMock, return_value=True) as send:
        with client.websocket_connect("/ws/term", headers={"origin": "http://testserver"}) as ws:
            for frame in (permission, invalid_input):
                ws.send_json(frame)
                assert ws.receive_json() == read_only
            send.assert_not_awaited()
            attach(ws, sid)
            send.reset_mock()
            for frame in (permission, invalid_input):
                ws.send_json({**frame, "agent_id": "wrong-agent"})
                assert ws.receive_json() == read_only
            for decision in ("allow", "deny"):
                ws.send_json({**permission, "decision": decision})
                ws.send_json(barrier)
                rejected = ws.receive_json()
                assert rejected["type"] == "error"
                assert rejected["code"] == "approval_not_supported"
                assert "approval-request" not in str(rejected)
                assert ws.receive_json()["message"] == "invalid client_input_id"
            send.assert_not_awaited()
            # The same attached socket must reauthorize a downgraded member
            # before disclosing either runtime-specific validation error.
            with main.models.SessionLocal() as db:
                devbox = db.get(main.Devbox, box)
                membership = db.scalar(main.select(main.Membership).where(
                    main.Membership.workspace_id == devbox.workspace_id,
                    main.Membership.user_id == devbox.owner_user_id))
                membership_id = membership.id
                membership.role = "viewer"
                db.commit()
            for frame in (permission, invalid_input):
                ws.send_json(frame)
                rejected = ws.receive_json()
                if frame["type"] == "permission":
                    assert rejected == read_only
                else:
                    assert_input_rejected(rejected, frame, agent["id"], "read_only", read_only["message"])
                assert "sensitive-api-key" not in str(rejected)
            # A removed member must not learn runtime/session metadata, even
            # from a previously attached socket or a malformed submission.
            with main.models.SessionLocal() as db:
                db.delete(db.get(main.Membership, membership_id))
                db.commit()
            for frame in (permission, invalid_input, barrier):
                ws.send_json(frame)
                assert ws.receive_json() == read_only
            send.assert_not_awaited()
            assert main.live_registry.get(sid).pending_inputs == {}


@pytest.mark.parametrize("surface", ["structured", "terminal", None])
def test_generic_permission_and_runtime_specific_input_options_still_forward(app_client, surface):
    client, main = app_client
    box, _, project = machine(client)
    agent = create(client, box, project, runtime="mock").json()
    sid = client.post(f"/api/agents/{agent['id']}/sessions", json={"surface": surface}).json()["id"]
    frames = [
        {"type": "permission", "session_id": sid, "request_id": "cli-approval", "decision": "allow"},
        {"type": "input", "session_id": sid, "data": "hello",
         "client_input_id": "00000000-0000-0000-0000-000000000001",
         "options": {"permission_mode": "default", "custom": True}, "files": ["cli-file"]},
    ]
    with patch.object(main.hub, "to_devbox", new_callable=AsyncMock, return_value=True) as send:
        with client.websocket_connect("/ws/term", headers={"origin": "http://testserver"}) as ws:
            attach(ws, sid)
            send.reset_mock()
            with patch.object(main.live_registry.get(sid), "queue_input") as queue:
                for fields, message in (
                    ({"client_input_id": "invalid"}, "invalid client_input_id"),
                    ({"data": {"api_key": "sensitive-api-key"}}, "invalid input data"),
                ):
                    ws.send_json({**frames[1], **fields})
                    assert ws.receive_json() == {"type": "error", "message": message}
                queue.assert_not_called()
                send.assert_not_awaited()
            for frame in frames:
                ws.send_json(frame)
                ws.send_json({"type": "input", "session_id": sid, "client_input_id": "invalid"})
                assert ws.receive_json() == {"type": "error", "message": "invalid client_input_id"}
                send.assert_awaited_once_with(agent["id"], {**frame, "agent_id": agent["id"]})
                send.reset_mock()


@pytest.mark.parametrize("kind", ["input", "stdin"])
def test_deeporca_text_input_options_rejected_before_queue_or_forward(app_client, kind):
    client, main = app_client
    box, _, project = machine(client)
    agent = create(client, box, project).json()
    sid = client.post(f"/api/agents/{agent['id']}/sessions", json={}).json()["id"]
    frame = {"type": kind, "session_id": sid, "data": "sensitive prompt",
             "client_input_id": "AABBCCDD-0000-0000-0000-000000000001"}
    invalid_options = [
        None, [], "model-1", True, 1, {"unknown": True},
        {"permission_mode": "default"}, {"files": []},
        {"profile": {}}, {"integration_version": 1},
        {"api_key": "sensitive-api-key"}, {"cwd": "C:/private"},
        *({"model": model} for model in (
            None, True, 1, [], {}, "", "../secret", "C:/private", "model;exec", "x" * 129)),
    ]
    invalid_fields = [
        *({"options": options} for options in invalid_options),
        {"permission_mode": "default"}, {"files": []}, {"unknown": True},
        {"api_key": "sensitive-api-key"}, {"files": ["C:/private"]},
    ]
    with patch.object(main.hub, "to_devbox", new_callable=AsyncMock, return_value=True) as send:
        with client.websocket_connect("/ws/term", headers={"origin": "http://testserver"}) as ws:
            attach(ws, sid)
            send.reset_mock()
            with patch.object(main.live_registry.get(sid), "queue_input") as queue:
                for fields in invalid_fields:
                    ws.send_json({**frame, **fields})
                    ws.send_json({"type": "input", "session_id": sid, "client_input_id": "invalid"})
                    rejected = ws.receive_json()
                    options = fields.get("options")
                    message = ("DeepOrca model must be a safe identifier of at most 128 characters"
                               if isinstance(options, dict) and set(options) == {"model"} else
                               "DeepOrca only supports text input and model options")
                    assert_input_rejected(rejected, frame, agent["id"], "invalid_options", message)
                    for sensitive in ("C:/private", "sensitive-api-key", "sensitive prompt"):
                        assert sensitive not in str(rejected)
                    assert ws.receive_json()["message"] == "invalid client_input_id"
                for data in ([], {}, None, True, 1, {"api_key": "sensitive-api-key"}, ["C:/private"]):
                    ws.send_json({**frame, "data": data})
                    assert_input_rejected(ws.receive_json(), frame, agent["id"],
                                          "invalid_data", "invalid input data")
                queue.assert_not_called()
                send.assert_not_awaited()
                for fields in ({}, {"options": {}}, {"options": {"model": "model-1"}},
                               {"options": {"model": "x" * 128}}):
                    ws.send_json({**frame, **fields})
                    ws.send_json({"type": "input", "session_id": sid, "client_input_id": "invalid"})
                    assert ws.receive_json()["message"] == "invalid client_input_id"
                    send.assert_awaited_once_with(agent["id"], {
                        **frame, **fields, "agent_id": agent["id"]})
                    queue.assert_called_once_with(frame["client_input_id"], frame["data"])
                    send.reset_mock()
                    queue.reset_mock()
            # No approval transport does not disable ordinary runtime controls.
            for kind in ("interrupt", "terminate"):
                ws.send_json({"type": kind, "session_id": sid})
                ws.send_json({"type": "input", "session_id": sid, "client_input_id": "invalid"})
                response = ws.receive_json()
                if kind == "terminate":
                    assert response["type"] == "status" and response["state"] == "ended"
                    response = ws.receive_json()
                    # Termination replaces the generation immediately; even
                    # this invalid-input barrier belongs to the old launch.
                    assert response["code"] == "session_changed"
                else:
                    assert response["message"] == "invalid client_input_id"
                expected = {"type": kind, "session_id": sid, "agent_id": agent["id"]}
                if kind == "terminate":
                    expected["launch_id"] = None
                send.assert_awaited_once_with(agent["id"], expected)
                send.reset_mock()


@pytest.mark.parametrize("kind", ["input", "stdin"])
def test_deeporca_invalid_input_ids_are_bounded_correlated_rejections(app_client, kind):
    client, main = app_client
    box, _, project = machine(client)
    agent = create(client, box, project).json()
    sid = client.post(f"/api/agents/{agent['id']}/sessions", json={}).json()["id"]
    with patch.object(main.hub, "to_devbox", new_callable=AsyncMock, return_value=True) as send:
        with client.websocket_connect("/ws/term", headers={"origin": "http://testserver"}) as ws:
            attach(ws, sid)
            send.reset_mock()
            with patch.object(main.live_registry.get(sid), "queue_input") as queue:
                for input_id in ("invalid", "x" * 4096, "", None, False, 0, 1, [],
                                 {"api_key": "sensitive-api-key"}, ["C:/private"]):
                    frame = {"type": kind, "session_id": sid, "client_input_id": input_id,
                             "data": {"prompt": "sensitive prompt"},
                             "options": {"api_key": "sensitive-api-key"}}
                    ws.send_json(frame)
                    # A different response acts as a barrier to detect duplicate
                    # error frames as well as a rejected ACK for each submission.
                    ws.send_json({"type": "permission", "session_id": sid})
                    rejected = ws.receive_json()
                    assert_input_rejected(rejected, frame, agent["id"],
                                          "invalid_input_id", "invalid client_input_id")
                    assert len(rejected["client_input_id"] or "") <= 128
                    for sensitive in ("sensitive-api-key", "sensitive prompt", "C:/private"):
                        assert sensitive not in str(rejected)
                    assert ws.receive_json()["code"] == "approval_not_supported"
                queue.assert_not_called()
                send.assert_not_awaited()


@pytest.mark.parametrize("runtime", [RUNTIME_ID, "mock"])
def test_native_input_ids_survive_forward_and_connector_ack_without_canonicalization(app_client, runtime):
    client, main = app_client
    box, headers, project = machine(client)
    agent = create(client, box, project, runtime=runtime).json()
    sid = client.post(f"/api/agents/{agent['id']}/sessions", json={"surface": "structured"}).json()["id"]
    with client.websocket_connect("/ws/devbox", headers=headers) as connector:
        assert connector.receive_json()["type"] == "hello"
        assert connector.receive_json()["type"] == "agents"
        with client.websocket_connect("/ws/term", headers={"origin": "http://testserver"}) as ws:
            attach(ws, sid)
            assert connector.receive_json()["type"] == "open"
            for input_id in ("AABBCCDD-0000-0000-0000-000000000001",
                             "aabbccdd000000000000000000000002"):
                frame = {"type": "input", "session_id": sid, "client_input_id": input_id,
                         "data": "hello", "options": {"model": "model-1"}}
                ws.send_json(frame)
                expected_id = input_id if runtime == RUNTIME_ID else str(main.UUID(input_id))
                assert connector.receive_json() == {
                    **frame, "agent_id": agent["id"], "client_input_id": expected_id}
                assert main.live_registry.get(sid).pending_inputs == {expected_id: "hello"}
                ack = {"type": "input_ack", "agent_id": agent["id"], "session_id": sid,
                       "client_input_id": input_id, "status": "rejected", "reason": "session_not_ready"}
                connector.send_json(ack)
                assert ws.receive_json() == {**ack, "client_input_id": expected_id}
                assert main.live_registry.get(sid).pending_inputs == {}
