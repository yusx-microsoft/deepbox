"""Hermetic Server coverage for editable, sealed DeepOrca profile settings."""
import base64
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest

from agentbridge.integrations.deeporca.contract import binding_revision, validate_runtime_config
from test_deeporca_routes import app_client, attach, create, machine


def sealed(seed=b"a"):
    # Structural opaque envelopes, not real API keys or native SDK encryption.
    return {"mode": "sealed", "key_id": "a" * 64,
            "wrapped_key": base64.b64encode(seed * 256).decode(),
            "iv": base64.b64encode(seed * 12).decode(),
            "ciphertext": base64.b64encode(seed * 32).decode()}


def config(**llm):
    return validate_runtime_config({
        "llm": {"provider": "openai", "base_url": "https://models.example/v1",
                "model": "vendor/model:latest", "context_window": 128000,
                "reasoning_effort": "", **llm},
        "credential": {"mode": "none"},
    })


def mark_ready(main, agent):
    ready = {"state": "ready", "revision": agent["runtime_status"]["revision"]}
    with main.models.SessionLocal() as db:
        db.get(main.Agent, agent["id"]).runtime_status = ready
        db.commit()
    return ready


def test_create_complete_model_settings_accepts_only_safe_desired_payload(app_client, caplog):
    client, _ = app_client
    box, headers, project = machine(client)
    complete = {**config(), "credential": sealed()}
    invalid = [
        {**complete, "api_key": "sensitive-marker"},
        {**complete, "credential": "sensitive-marker"},
        {**complete, "credential": {"mode": "plaintext", "api_key": "sensitive-marker"}},
        {**complete, "credential": {**sealed(), "api_key": "sensitive-marker"}},
        {**complete, "credential": {"mode": "none", "api_key": "sensitive-marker"}},
        {**complete, "credential": {**sealed(), "ciphertext": "sensitive-marker"}},
        {**complete, "llm": {**complete["llm"], "api_key": "sensitive-marker"}},
        {**complete, "llm": {**complete["llm"], "provider": "unsupported"}},
        {**complete, "llm": {**complete["llm"], "context_window": True}},
        {**complete, "llm": {**complete["llm"], "context_window": 0}},
        {**complete, "llm": {**complete["llm"], "reasoning_effort": "unsupported"}},
        {**complete, "llm": {**complete["llm"], "base_url": "https://sensitive-marker@host/v1"}},
        {**complete, "llm": {**complete["llm"], "base_url": "https://host/v1?key=sensitive-marker"}},
        {"credential": sealed()},
    ]
    for desired in invalid:
        response = create(client, box, project, runtime_config=desired)
        assert response.status_code == 422, response.text
        assert "sensitive-marker" not in response.text
    assert create(client, box, project, api_key="sensitive-marker").status_code == 422
    assert "sensitive-marker" not in caplog.text
    for index, desired in enumerate((complete, config(), {"llm": config()["llm"]}, {})):
        response = create(client, box, project, handle=f"orca-{index}", runtime_config=desired)
        assert response.status_code == 200, response.text
        agent = response.json()
        assert agent["runtime_config"] == validate_runtime_config(desired)
        assert agent["runtime_status"] == {
            "state": "pending", "revision": binding_revision(agent["id"], project, desired)}
        assert client.get(f"/api/agents/{agent['id']}").json()["runtime_config"] == agent["runtime_config"]
    directory = client.get("/api/me", headers=headers).json()["agents"]
    assert directory[0]["runtime_config"]["credential"] == complete["credential"]


def test_settings_update_persists_broadcasts_snapshot_and_rejects_stale_status(app_client):
    client, main = app_client
    box, headers, project = machine(client)
    agent = create(client, box, project, runtime_config=config()).json()
    url = f"/api/agents/{agent['id']}"
    old_ready = mark_ready(main, agent)
    desired = config(model="new/model", context_window=256000, reasoning_effort="high")
    with client.websocket_connect("/ws/devbox", headers=headers) as connector:
        assert connector.receive_json()["type"] == "hello"
        assert connector.receive_json()["type"] == "agents"
        with patch.object(main.hub, "to_users", new_callable=AsyncMock) as broadcast:
            response = client.patch(url, json={"display_name": " Renamed ", "runtime_config": desired})
            assert response.status_code == 200, response.text
            updated = response.json()
            assert updated["display_name"] == "Renamed"
            assert updated["runtime_config"] == desired
            assert updated["runtime_status"] == {
                "state": "pending", "revision": binding_revision(agent["id"], project, desired)}
            assert updated["runtime_status"]["revision"] != old_ready["revision"]
            snapshot = connector.receive_json()
            assert snapshot["type"] == "agents"
            assert snapshot["agents"][0]["runtime_config"] == desired
            assert snapshot["agents"][0]["runtime_status"] == updated["runtime_status"]
            assert client.get(url).json()["runtime_config"] == desired
            broadcast.assert_awaited_once()
            assert broadcast.call_args.args[1]["runtime_status"] == updated["runtime_status"]
            broadcast.reset_mock()
            connector.send_json({"type": "agent.runtime_status", "agent_id": agent["id"],
                                 "runtime_status": {**old_ready, "state": "error", "code": "stale"}})
            connector.send_json({"type": "heartbeat"})
            assert connector.receive_json()["type"] == "heartbeat_ack"
            assert client.get(url).json()["runtime_status"] == updated["runtime_status"]
            broadcast.assert_not_awaited()
            ready = {**updated["runtime_status"], "state": "ready"}
            connector.send_json({"type": "agent.runtime_status", "agent_id": agent["id"],
                                 "runtime_status": ready})
            connector.send_json({"type": "heartbeat"})
            assert connector.receive_json()["type"] == "heartbeat_ack"
            assert client.get(url).json()["runtime_status"] == ready


def test_credential_omission_retains_envelope_without_mutating_input(app_client):
    client, main = app_client
    box, _, project = machine(client)
    original = {**config(), "credential": sealed()}
    agent = create(client, box, project, runtime_config=original).json()
    url = f"/api/agents/{agent['id']}"
    desired = config(model="replacement/model")
    desired.pop("credential")
    before = deepcopy(desired)
    with main.models.SessionLocal() as db:
        record = db.get(main.Agent, agent["id"])
        prepared = main.runtime_policy(record.runtime).validate_agent_update(record, {"runtime_config": desired})
        assert desired == before
        assert record.runtime_config == original
        assert prepared == {**desired, "credential": original["credential"]}
    response = client.patch(url, json={"runtime_config": desired})
    assert response.status_code == 200, response.text
    assert response.json()["runtime_config"] == {**desired, "credential": original["credential"]}
    assert response.json()["runtime_status"]["revision"] != agent["runtime_status"]["revision"]
    # An omitted retained credential on a no-op must not reset observed ready.
    ready = mark_ready(main, response.json())
    with patch.object(main, "_broadcast_runtime_status", new_callable=AsyncMock) as broadcast:
        response = client.patch(url, json={"runtime_config": desired, "display_name": "Independent"})
        assert response.status_code == 200
        assert response.json()["runtime_status"] == ready
        assert response.json()["display_name"] == "Independent"
        broadcast.assert_not_awaited()


def test_endpoint_change_requires_fresh_envelope_or_explicit_no_authentication(app_client):
    client, main = app_client
    box, _, project = machine(client)
    original = {**config(), "credential": sealed()}
    agent = create(client, box, project, runtime_config=original).json()
    url = f"/api/agents/{agent['id']}"
    ready = mark_ready(main, agent)
    redirected = config(base_url="https://different.example/v1")
    redirected.pop("credential")
    with patch.object(main, "_push_agent_directory", new_callable=AsyncMock) as push:
        for desired in (redirected, {**redirected, "credential": sealed()}):
            response = client.patch(url, json={"runtime_config": desired, "display_name": "Must not apply"})
            assert response.status_code == 409
            assert "different.example" not in response.text
            assert "models.example" not in response.text
            assert "new sealed credential" in response.text
        push.assert_not_awaited()
    persisted = client.get(url).json()
    assert persisted["runtime_config"] == original
    assert persisted["runtime_status"] == ready
    assert persisted["display_name"] == agent["display_name"]
    fresh = {**redirected, "credential": sealed(b"b")}
    response = client.patch(url, json={"runtime_config": fresh})
    assert response.status_code == 200, response.text
    assert response.json()["runtime_config"] == fresh
    cleared = config(base_url="http://127.0.0.1:9000/v1")
    response = client.patch(url, json={"runtime_config": cleared})
    assert response.status_code == 200, response.text
    assert response.json()["runtime_config"] == cleared
    # Once explicitly keyless, another endpoint edit can retain mode=none.
    keyless = config(base_url="http://localhost:9001/v1")
    keyless.pop("credential")
    response = client.patch(url, json={"runtime_config": keyless})
    assert response.status_code == 200
    assert response.json()["runtime_config"]["credential"] == {"mode": "none"}


def test_identity_fences_and_plaintext_rejection_apply_to_settings(app_client, caplog):
    client, main = app_client
    box, _, project = machine(client)
    original = config()
    agent = create(client, box, project, runtime_config=original).json()
    url = f"/api/agents/{agent['id']}"
    ready = mark_ready(main, agent)
    for body, status in (
        ({"runtime": "mock"}, 409),
        ({"local_project_id": "another-project"}, 409),
        ({"devbox_id": "another-box"}, 422),
        ({"handle": "another-handle"}, 422),
        ({"cwd": "sensitive-marker"}, 422),
        ({"launch_cmd": "sensitive-marker"}, 422),
        ({"runtime_config": {"model": "legacy-model"}}, 409),
        ({"runtime_config": {**original, "integration_version": 2}}, 422),
        ({"runtime_config": {**original, "profile": {"mode": "bind"}}}, 422),
        ({"runtime_config": {**original, "profile": {"configuration_template_ref": "sensitive-marker"}}}, 422),
        ({"runtime_config": {**original, "credential": {"mode": "plain", "api_key": "sensitive-marker"}}}, 422),
        ({"runtime_config": {**original, "llm": {**original["llm"], "api_key": "sensitive-marker"}}}, 422),
        ({"api_key": "sensitive-marker"}, 422),
        ({"runtime_config": None}, 422),
    ):
        response = client.patch(url, json={"display_name": "Must not apply", **body})
        assert response.status_code == status, response.text
        assert "sensitive-marker" not in response.text
    assert "sensitive-marker" not in caplog.text
    persisted = client.get(url).json()
    assert persisted["runtime_config"] == original
    assert persisted["runtime_status"] == ready
    assert persisted["display_name"] == agent["display_name"]


@pytest.mark.parametrize("activity", ["connector", "idle_attached", "queued_input"])
def test_any_active_conversation_blocks_changes_but_not_noop_or_rename(app_client, activity):
    client, main = app_client
    from server.app.hub import DevboxConn, HumanConn

    box, _, project = machine(client)
    original = config()
    agent = create(client, box, project, runtime_config=original).json()
    url = f"/api/agents/{agent['id']}"
    # Protect ANY session, not merely the requester's last or busy session.
    sessions = [client.post(url + "/sessions", json={}).json() for _ in range(2)]
    sid = sessions[-1]["id"]
    ready = mark_ready(main, agent)
    if activity == "connector":
        conn = DevboxConn(ws=None, devbox_id=box, agent_ids={agent["id"]}, active_session_ids={sid})
        main.hub.devboxes[box] = conn
        main.hub.agent_to_devbox[agent["id"]] = box
    elif activity == "idle_attached":
        main.hub.session_watchers[sid] = {HumanConn(ws=None, user_id="viewer")}
    else:
        main.live_registry.get_or_create(sid, 120, 30).pending_inputs["queued"] = "input"
    with patch.object(main, "_push_agent_directory", new_callable=AsyncMock) as push, patch.object(
            main.hub, "to_devbox", new_callable=AsyncMock) as command, patch.object(
            main, "_broadcast_runtime_status", new_callable=AsyncMock) as broadcast:
        response = client.patch(url, json={"runtime_config": config(model="new-model")})
        assert response.status_code == 409, response.text
        assert "close active conversations" in response.text
        push.assert_not_awaited()
        command.assert_not_awaited()  # never terminate running work for settings
        broadcast.assert_not_awaited()
        response = client.patch(url, json={"runtime_config": original, "display_name": "Still editable"})
        assert response.status_code == 200, response.text
        assert response.json()["runtime_status"] == ready
        assert response.json()["runtime_config"] == original
        assert response.json()["display_name"] == "Still editable"
        broadcast.assert_not_awaited()


def test_real_idle_browser_attachment_blocks_profile_edit_until_detached(app_client):
    client, _ = app_client
    box, _, project = machine(client)
    agent = create(client, box, project, runtime_config=config()).json()
    url = f"/api/agents/{agent['id']}"
    session = client.post(url + "/sessions", json={}).json()
    with client.websocket_connect("/ws/term", headers={"origin": "http://testserver"}) as browser:
        attach(browser, session["id"])
        response = client.patch(url, json={"runtime_config": config(model="new-model")})
        assert response.status_code == 409
    # Disconnected historical sessions have no active work and do not block.
    response = client.patch(url, json={"runtime_config": config(model="new-model")})
    assert response.status_code == 200, response.text


def test_ended_or_other_agents_sessions_do_not_block_settings(app_client):
    client, main = app_client
    from server.app.hub import HumanConn

    box, _, project = machine(client)
    agent = create(client, box, project, runtime_config=config()).json()
    other = create(client, box, project, handle="other", runtime_config=config()).json()
    url = f"/api/agents/{agent['id']}"
    ended = client.post(url + "/sessions", json={}).json()
    unrelated = client.post(f"/api/agents/{other['id']}/sessions", json={}).json()
    for session in (ended, unrelated):
        main.hub.session_watchers[session["id"]] = {HumanConn(ws=None, user_id="viewer")}
    main.live_registry.get_or_create(ended["id"], 120, 30).mark_ended(0)
    response = client.patch(url, json={"runtime_config": config(model="new-model")})
    assert response.status_code == 200, response.text


def test_settings_authorization_precedes_runtime_validation_for_existing_roles(app_client):
    client, main = app_client
    box, _, project = machine(client)
    agent = create(client, box, project, runtime_config=config()).json()
    url = f"/api/agents/{agent['id']}"
    with main.models.SessionLocal() as db:
        devbox = db.get(main.Devbox, box)
        membership = db.scalar(main.select(main.Membership).where(
            main.Membership.workspace_id == devbox.workspace_id,
            main.Membership.user_id == devbox.owner_user_id))
        for role in ("viewer", "operator"):
            membership.role = role
            db.commit()
            with patch.object(main, "runtime_policy", side_effect=AssertionError("policy before ACL")):
                response = client.patch(url, json={"runtime_config": {"credential": "sensitive-marker"}})
                assert response.status_code == 404
                assert "sensitive-marker" not in response.text
        membership.role = "admin"
        db.commit()
    assert client.patch(url, json={"runtime_config": config(model="new-model")}).status_code == 200


def test_legacy_configs_and_ordinary_runtimes_keep_existing_behavior(app_client):
    client, main = app_client
    box, _, project = machine(client)
    legacy = create(client, box, project).json()
    ready = mark_ready(main, legacy)
    url = f"/api/agents/{legacy['id']}"
    assert client.patch(url, json={"runtime_config": {}}).json()["runtime_status"] == ready
    response = client.patch(url, json={"runtime_config": config()})
    assert response.status_code == 200, response.text
    assert response.json()["runtime_status"]["revision"] != ready["revision"]
    fixed = create(client, box, project, handle="legacy-model", runtime_config={"model": "fixed"}).json()
    response = client.patch(f"/api/agents/{fixed['id']}", json={"runtime_config": config()})
    assert response.status_code == 409  # legacy top-level model is identity
    compatible = {**config(model="fixed"), "model": "fixed"}
    response = client.patch(f"/api/agents/{fixed['id']}", json={"runtime_config": compatible})
    assert response.status_code == 200, response.text
    for runtime in ("mock", "claude-code", "codex", "copilot", "future-runtime"):
        agent = create(client, box, project, handle=runtime, runtime=runtime,
                       runtime_config={"custom": "unchanged"}).json()
        url = f"/api/agents/{agent['id']}"
        response = client.patch(url, json={"runtime_config": config()})
        assert response.status_code == 422
        assert response.json()["detail"] == "this endpoint only supports display renames"
        response = client.patch(url, json={"display_name": "Ordinary rename"})
        assert response.status_code == 200, response.text
        assert response.json()["runtime_config"] == {"custom": "unchanged"}
        assert response.json()["runtime_status"] is None
