"""Regression coverage for DeepOrca policy combined with native history lifecycle.

All runtime frames are synthetic. No Connector, native SDK, or local profile I/O.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
from uuid import UUID

import pytest

from agentbridge.integrations.deeporca.contract import RENDERER_ID
from test_session_surfaces import add_member
from test_deeporca_routes import app_client, create, machine
from test_deeporca_settings import config, sealed
from test_session_history_actions import (
    _commands, _connector_frames, _human_frames, _one, _ready,
)


ORIGIN = {"origin": "http://testserver"}
REF = "native-" + "a" * 32


def history(client, main, *, bound=False, runtime="deeporca"):
    box, headers, project = machine(client)
    descriptor = {"runtime": runtime, "agent_config": {
        "profile_modes": ["create", "bind"],
        "existing_profiles": [{"id": REF, "label": "Existing profile"}],
    }, "surfaces": [{"id": "structured", "features": {
        "session_lifecycle": 1, "context": {
            "continuity": "native" if runtime == "deeporca" else "native_resume",
            "available": True, "explicit_resume": runtime != "deeporca",
        },
    }}]}
    with main.models.SessionLocal() as db:
        db.get(main.Devbox, box).capabilities = [descriptor]
        db.commit()
    desired = ({"profile": {"mode": "bind", "profile_ref": REF, "native_stopped": True}}
               if bound else {**config(), "credential": sealed()})
    response = create(client, box, project, runtime=runtime, runtime_config=desired)
    assert response.status_code == 200, response.text
    agent = response.json()
    response = client.post(f"/api/agents/{agent['id']}/sessions", json={"surface": "structured"})
    assert response.status_code == 200, response.text
    session = response.json()
    assert str(UUID(session["id"])) == session["id"]
    with main.models.SessionLocal() as db:
        db.get(main.Session, session["id"]).launch_id = "previous-launch"
        db.commit()
    main.live_registry.get_or_create(session["id"]).mark_ended(0)
    return headers, agent, client.get(f"/api/sessions/{session['id']}").json()


@contextmanager
def connector(client, headers):
    with client.websocket_connect("/ws/devbox", headers=headers) as ws:
        assert ws.receive_json()["type"] == "hello"
        assert ws.receive_json()["type"] == "agents"
        yield ws


def ready(launch):
    return {**_ready(launch), "surface": "structured"}


def resume(client, human, runtime_socket, sid, **overrides):
    current = client.get(f"/api/sessions/{sid}").json()
    frames = _human_frames(human, {"type": "resume", "session_id": sid,
                                   "launch_id": current["launch_id"], **overrides})
    status = _one(frames, "status")
    assert status["state"] == "starting"
    launches = [frame for frame in _commands(_connector_frames(runtime_socket))
                if frame["type"] in {"open", "resume"}]
    assert len(launches) == 1, launches
    launch = launches[0]
    assert launch["launch_id"] == status["launch_id"] != current["launch_id"]
    return launch, frames


@pytest.mark.parametrize("bound", [False, True])
def test_resume_keeps_authorized_binding_and_policy_metadata(app_client, bound):
    client, main = app_client
    headers, agent, session = history(client, main, bound=bound)
    sid = session["id"]
    assert session["surface"] == "structured"
    assert session["renderer"] == RENDERER_ID
    assert session["runtime_status"] == agent["runtime_status"]
    assert session["can_rename"] and session["resume_supported"]
    assert not session["can_resume"]  # Offline, not a policy failure.
    with connector(client, headers) as runtime:
        with client.websocket_connect("/ws/term", headers=ORIGIN) as human:
            # Attaching to history must restore, never implicitly launch.
            frames = _human_frames(human, {"type": "attach", "session_id": sid})
            assert _one(frames, "restore")["kind"] == "event"
            status = _one(frames, "status")
            assert status["code"] == "resume_required"
            assert status["renderer"] == RENDERER_ID
            assert status["runtime_status"] == agent["runtime_status"]
            assert not _commands(_connector_frames(runtime))
            # A resume request is not an alternative configuration/update route.
            launch, frames = resume(client, human, runtime, sid,
                runtime="mock", runtime_config={"profile": {"mode": "bind", "profile_ref": "unowned"}},
                credential={"mode": "plaintext", "api_key": "sensitive-marker"},
                local_project_id="other-project", cwd="C:/private-profile", launch_cmd="unsafe")
            assert set(launch) == {"type", "agent_id", "session_id", "launch_id", "cols", "rows", "surface"}
            assert launch["agent_id"] == agent["id"]
            assert launch["type"] == "open"  # SDK continuation, not CLI context adoption.
            assert launch["surface"] == "structured"
            assert _one(frames, "status")["renderer"] == RENDERER_ID
            # Stale Connector frames cannot activate history. Surface facts still
            # pass through policy when the current launch becomes ready.
            _connector_frames(runtime, {**ready(launch), "launch_id": "previous-launch"})
            assert not any(f["type"] == "session.ready" for f in _human_frames(human))
            invalid = _connector_frames(runtime, {**ready(launch), "surface": "terminal"})
            assert _one(invalid, "error")["code"] == "invalid_surface"
            _connector_frames(runtime, ready(launch))
            notification = _one(_human_frames(human), "session.ready")
            assert notification["surface"] == "structured" and notification["renderer"] == RENDERER_ID
            assert client.get(f"/api/sessions/{sid}").json()["state"] == "live"
            renamed = client.patch(f"/api/sessions/{sid}", json={
                "title": "A display title", "expected_title": session["title"]})
            assert renamed.status_code == 200
            assert renamed.json()["renderer"] == RENDERER_ID
            assert renamed.json()["launch_id"] == launch["launch_id"]
            assert renamed.json()["can_resume"]
            assert _one(_human_frames(human), "session.updated")["title"] == "A display title"
    current = client.get(f"/api/agents/{agent['id']}").json()
    for key in ("runtime", "local_project_id", "runtime_config", "runtime_status"):
        assert current[key] == agent[key]


def test_launch_tagged_inputs_keep_deeporca_option_policy_and_native_ids(app_client):
    client, main = app_client
    headers, agent, session = history(client, main)
    sid = session["id"]
    with connector(client, headers) as runtime:
        with client.websocket_connect("/ws/term", headers=ORIGIN) as human:
            launch, _ = resume(client, human, runtime, sid)
            assert not _connector_frames(runtime, ready(launch))
            assert _one(_human_frames(human), "session.ready")["launch_id"] == launch["launch_id"]
            input_id = "AABBCCDD-0000-0000-0000-000000000001"
            frame = {"type": "input", "session_id": sid, "launch_id": launch["launch_id"],
                     "client_input_id": input_id, "data": "hello", "options": {"model": "safe-model"}}
            for override in ({"runtime_config": {}}, {"credential": sealed()},
                             {"options": {"profile_ref": REF}}, {"options": {"files": []}}):
                rejection = _one(_human_frames(human, {**frame, **override}), "input_ack")
                assert rejection["reason"] == "invalid_options"
                assert rejection["client_input_id"] == input_id
                assert rejection["launch_id"] == launch["launch_id"]
                assert not main.live_registry.get(sid).pending_inputs
                assert not _connector_frames(runtime)
            for kind in ("resize", "permission"):
                assert _one(_human_frames(human, {"type": kind, "session_id": sid,
                    "launch_id": launch["launch_id"]}), "error")
            stale = _human_frames(human, {**frame, "launch_id": "previous-launch"})
            assert _one(stale, "error")["code"] == "session_changed"
            assert not _connector_frames(runtime)
            assert not _human_frames(human, frame)
            assert _one(_connector_frames(runtime), "input") == {**frame, "agent_id": agent["id"]}
            assert main.live_registry.get(sid).pending_inputs == {input_id: "hello"}
            ack = {"type": "input_ack", "agent_id": agent["id"], "session_id": sid,
                   "launch_id": launch["launch_id"], "client_input_id": input_id, "status": "delivered"}
            _connector_frames(runtime, ack)
            assert _one(_human_frames(human), "input_ack") == ack
            assert not main.live_registry.get(sid).pending_inputs


@pytest.mark.parametrize("revocation", ["viewer", "remove"])
def test_deeporca_history_resume_and_rename_recheck_access(app_client, revocation):
    client, main = app_client
    headers, agent, session = history(client, main, bound=True)
    sid = session["id"]
    member, uid = add_member(client, main, sid, "operator")
    with connector(client, headers) as runtime:
        with member.websocket_connect("/ws/term", headers=ORIGIN) as human:
            _human_frames(human, {"type": "attach", "session_id": sid})
            with main.models.SessionLocal() as db:
                membership = db.query(main.Membership).filter_by(
                    user_id=uid, workspace_id=db.get(main.Session, sid).workspace_id).one()
                if revocation == "remove":
                    db.delete(membership)
                else:
                    membership.role = "viewer"
                db.commit()
            frames = _human_frames(human, {"type": "resume", "session_id": sid,
                "launch_id": session["launch_id"], "runtime_config": config(model="replacement")})
            assert _one(frames, "error")
            assert not _commands(_connector_frames(runtime))
            assert member.patch(f"/api/sessions/{sid}", json={
                "title": "forbidden", "expected_title": session["title"]}).status_code in (403, 404)
    assert client.get(f"/api/sessions/{sid}").json()["launch_id"] == session["launch_id"]
    assert client.get(f"/api/agents/{agent['id']}").json()["runtime_config"] == agent["runtime_config"]


def test_end_allows_only_validated_idle_settings_and_resume_uses_same_identity(app_client):
    client, main = app_client
    headers, agent, session = history(client, main)
    sid, url = session["id"], f"/api/agents/{agent['id']}"
    desired = config(model="replacement/model")
    desired.pop("credential")
    with connector(client, headers) as runtime:
        with client.websocket_connect("/ws/term", headers=ORIGIN) as human:
            first, _ = resume(client, human, runtime, sid)
            assert client.patch(url, json={"runtime_config": desired}).status_code == 409
            _connector_frames(runtime, ready(first))
            _human_frames(human)
            assert client.patch(url, json={"runtime_config": desired}).status_code == 409
            stopped = _one(_human_frames(human, {"type": "terminate", "session_id": sid,
                "launch_id": first["launch_id"]}), "status")
            assert stopped["state"] == "ended" and stopped["launch_id"] != first["launch_id"]
            assert _one(_commands(_connector_frames(runtime)), "terminate")["launch_id"] == first["launch_id"]
            assert main.live_registry.get(sid).ended
            for unsafe in ({"api_key": "plaintext"}, {"profile": {"mode": "create", "other": True}}):
                assert client.patch(url, json={"runtime_config": unsafe}).status_code == 422
            redirect = deepcopy(desired)
            redirect["llm"]["base_url"] = "https://other.example/v1"
            assert client.patch(url, json={"runtime_config": redirect}).status_code == 409
            assert client.patch(url, json={"runtime": "mock"}).status_code == 409
            assert client.patch(url, json={"local_project_id": "other"}).status_code == 409
            changed = client.patch(url, json={"runtime_config": desired})
            assert changed.status_code == 200, changed.text
            updated = changed.json()
            assert updated["runtime_config"] == {**desired, "credential": agent["runtime_config"]["credential"]}
            assert updated["runtime_status"]["state"] == "pending"
            assert updated["runtime_status"]["revision"] != agent["runtime_status"]["revision"]
            _connector_frames(runtime)  # Consume only the authorized directory update.
            second, frames = resume(client, human, runtime, sid, runtime_config=agent["runtime_config"])
            assert second["session_id"] == first["session_id"]
            assert _one(frames, "status")["runtime_status"] == updated["runtime_status"]
            assert "runtime_config" not in second
            assert client.get(url).json()["runtime_config"] == updated["runtime_config"]


def test_optional_library_native_data_stays_opaque_across_history_restore(app_client):
    client, main = app_client
    main.live_registry.durable_loader = main._durable_events_loader
    headers, agent, session = history(client, main)
    sid = session["id"]
    with connector(client, headers) as runtime:
        with client.websocket_connect("/ws/term", headers=ORIGIN) as human:
            launch, _ = resume(client, human, runtime, sid)
            assert not _connector_frames(runtime, ready(launch))
            assert _one(_human_frames(human), "session.ready")["launch_id"] == launch["launch_id"]
            event = {"type": "message.delta", "runtime": "deeporca", "message_id": "native-id",
                     "native": {"extension": {"unknown": [1, {"text": "native history"}]}}}
            output = {"type": "output", "agent_id": agent["id"], "session_id": sid,
                      "launch_id": launch["launch_id"], "pty_instance_id": ready(launch)["pty_instance_id"], "seq": 1,
                      "kind": "event", "data": json.dumps(event)}
            assert _one(_connector_frames(runtime, output), "ack")["seq"] == 1
            observed = _one(_human_frames(human), "output")
            assert json.loads(observed["data"]) == event
            _human_frames(human, {"type": "terminate", "session_id": sid, "launch_id": launch["launch_id"]})
            _connector_frames(runtime)
            frames = _human_frames(human, {"type": "attach", "session_id": sid})
            assert json.loads(_one(frames, "restore")["data"]) == event
            assert not _commands(_connector_frames(runtime))
