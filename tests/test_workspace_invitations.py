import base64
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import importlib
import threading
import json
import os
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from sqlalchemy import select


def _principal(subject: str, email: str, name: str) -> dict[str, str]:
    payload = {
        "auth_typ": "aad",
        "claims": [
            {"typ": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier",
             "val": subject},
            {"typ": "http://schemas.microsoft.com/identity/claims/tenantid",
             "val": "tenant-a"},
            {"typ": "preferred_username", "val": email},
            {"typ": "name", "val": name},
        ],
    }
    value = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"X-MS-CLIENT-PRINCIPAL": value,
            "X-MS-CLIENT-PRINCIPAL-ID": subject,
            "X-MS-CLIENT-PRINCIPAL-IDP": "aad"}


def _build_app(tmp_path):
    updates = {
        "DEEPBOX_ENV": "test",
        "DEEPBOX_SECRET": "test-secret-for-workspace-invitations",
        "DEEPBOX_DATABASE_URL": f"sqlite:///{(tmp_path / 'workspace.db').as_posix()}",
        "DEEPBOX_DATA_DIR": str(tmp_path / "data"),
        "DEEPBOX_PUBLIC_URL": "https://deepbox.example",
        "DEEPBOX_AUTH_MODE": "microsoft",
        "DEEPBOX_MICROSOFT_OWNER_EMAILS": "owner@example.com",
        "DEEPBOX_RATE_LIMIT_ENABLED": "0",
    }
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        import server.app.config as config_module
        import server.app.models as models_module
        import server.app.main as main_module
        importlib.reload(config_module)
        importlib.reload(models_module)
        main_module = importlib.reload(main_module)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return TestClient(main_module.app), main_module, models_module


def _login(client: TestClient, subject: str, email: str, name: str):
    return client.get(
        "/api/auth/microsoft/callback",
        headers=_principal(subject, email, name),
        follow_redirects=False,
    )


def _token_from_join_url(join_url: str) -> str:
    fragment = urlsplit(join_url).fragment
    prefix = "workspace-invite="
    assert fragment.startswith(prefix)
    return fragment[len(prefix):]


def test_invited_member_joins_workspace_and_sees_all_devboxes(tmp_path):
    client, main, models = _build_app(tmp_path)
    try:
        assert _login(client, "owner-sub", "owner@example.com", "Owner").status_code == 302
        workspace = client.post("/api/workspaces", json={"name": "Radio Lab"}).json()
        workspace_id = workspace["id"]
        devbox = client.post("/api/devboxes", json={
            "name": "Shared machine", "workspace_id": workspace_id,
        })
        assert devbox.status_code == 200
        devbox_id = devbox.json()["devbox"]["id"]

        created = client.post(
            f"/api/workspaces/{workspace_id}/invitations",
            json={"email": "Member@Example.com", "role": "viewer"},
        )
        assert created.status_code == 200
        payload = created.json()
        token = _token_from_join_url(payload["join_url"])
        assert token not in json.dumps(
            client.get(f"/api/workspaces/{workspace_id}/invitations").json())

        preview = client.post(
            "/api/workspace-invitations/preview", json={"token": token})
        assert preview.status_code == 200
        assert preview.json()["workspace_name"] == "Radio Lab"
        assert preview.json()["email_hint"] == "m***@example.com"

        client.cookies.clear()
        assert _login(client, "member-sub", "member@example.com", "Member").status_code == 302
        accepted = client.post(
            "/api/workspace-invitations/accept", json={"token": token})
        assert accepted.status_code == 200
        assert accepted.json()["workspace"] == {
            "id": workspace_id, "name": "Radio Lab",
        }
        assert accepted.json()["role"] == "viewer"
        assert accepted.json()["already_member"] is False

        replay = client.post(
            "/api/workspace-invitations/accept", json={"token": token})
        assert replay.status_code == 200
        assert replay.json()["workspace"] == accepted.json()["workspace"]
        assert replay.json()["role"] == "viewer"
        assert replay.json()["already_member"] is True

        workspaces = client.get("/api/workspaces").json()
        assert {item["name"] for item in workspaces} == {"Personal", "Radio Lab"}
        shared = next(item for item in workspaces if item["id"] == workspace_id)
        assert shared["role"] == "viewer"
        devboxes = client.get("/api/devboxes").json()
        assert devbox_id in {item["id"] for item in devboxes}
        assert client.post(
            "/api/workspace-invitations/preview", json={"token": token}
        ).status_code == 404

        with models.SessionLocal() as session:
            invite = session.get(models.WorkspaceInvitation, payload["id"])
            assert invite.token_hash != token
            assert invite.accepted_by_user_id == client.get("/api/me/user").json()["id"]
    finally:
        client.close()
        models._engine.dispose()


def test_concurrent_accept_is_single_use_and_idempotent(tmp_path):
    owner, main, models = _build_app(tmp_path)
    first = TestClient(main.app)
    second = TestClient(main.app)
    try:
        _login(owner, "owner-sub", "owner@example.com", "Owner")
        workspace_id = owner.post(
            "/api/workspaces", json={"name": "Race Lab"}).json()["id"]
        created = owner.post(
            f"/api/workspaces/{workspace_id}/invitations",
            json={"email": "member@example.com", "role": "operator"},
        ).json()
        token = _token_from_join_url(created["join_url"])
        _login(first, "member-sub", "member@example.com", "Member")
        _login(second, "member-sub", "member@example.com", "Member")

        barrier = threading.Barrier(2)

        def accept(client):
            barrier.wait()
            return client.post(
                "/api/workspace-invitations/accept", json={"token": token})

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(accept, (first, second)))

        assert [response.status_code for response in responses] == [200, 200]
        assert sorted(response.json()["already_member"]
                      for response in responses) == [False, True]
        with models.SessionLocal() as session:
            invite = session.get(models.WorkspaceInvitation, created["id"])
            member = session.scalar(select(models.User).where(
                models.User.external_subject == "member-sub"))
            memberships = session.scalars(select(models.Membership).where(
                models.Membership.workspace_id == workspace_id,
                models.Membership.user_id == member.id,
            )).all()
            assert invite.accepted_by_user_id == member.id
            assert len(memberships) == 1
            assert memberships[0].role == "operator"
    finally:
        first.close()
        second.close()
        owner.close()
        models._engine.dispose()


def test_expired_workspace_invitation_cannot_be_previewed_or_accepted(tmp_path):
    client, main, models = _build_app(tmp_path)
    try:
        _login(client, "owner-sub", "owner@example.com", "Owner")
        workspace_id = client.post(
            "/api/workspaces", json={"name": "Expired Lab"}).json()["id"]
        created = client.post(
            f"/api/workspaces/{workspace_id}/invitations",
            json={"email": "target@example.com", "role": "viewer"},
        ).json()
        token = _token_from_join_url(created["join_url"])
        with models.SessionLocal() as session:
            invitation = session.get(models.WorkspaceInvitation, created["id"])
            invitation.expires_at = (
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1))
            session.commit()

        assert client.post(
            "/api/workspace-invitations/preview", json={"token": token}
        ).status_code == 404
        client.cookies.clear()
        _login(client, "target-sub", "target@example.com", "Target")
        assert client.post(
            "/api/workspace-invitations/accept", json={"token": token}
        ).status_code == 404
        with models.SessionLocal() as session:
            invitation = session.get(models.WorkspaceInvitation, created["id"])
            assert invitation.accepted_at is None
            assert invitation.accepted_by_user_id is None
    finally:
        client.close()
        models._engine.dispose()


def test_workspace_invitation_is_email_bound_and_revocable(tmp_path):
    client, main, models = _build_app(tmp_path)
    try:
        _login(client, "owner-sub", "owner@example.com", "Owner")
        workspace_id = client.post(
            "/api/workspaces", json={"name": "Private Lab"}).json()["id"]
        created = client.post(
            f"/api/workspaces/{workspace_id}/invitations",
            json={"email": "target@example.com", "role": "operator"},
        ).json()
        token = _token_from_join_url(created["join_url"])

        client.cookies.clear()
        _login(client, "wrong-sub", "wrong@example.com", "Wrong Account")
        denied = client.post(
            "/api/workspace-invitations/accept", json={"token": token})
        assert denied.status_code == 403
        assert client.post(
            "/api/workspace-invitations/preview", json={"token": token}
        ).status_code == 200

        client.cookies.clear()
        _login(client, "owner-sub", "owner@example.com", "Owner")
        revoked = client.delete(
            f"/api/workspaces/{workspace_id}/invitations/{created['id']}")
        assert revoked.status_code == 200
        assert client.post(
            "/api/workspace-invitations/preview", json={"token": token}
        ).status_code == 404

        with models.SessionLocal() as session:
            stored = session.scalar(select(models.WorkspaceInvitation).where(
                models.WorkspaceInvitation.id == created["id"]))
            assert stored.revoked_at is not None
            assert stored.accepted_at is None
    finally:
        client.close()
        models._engine.dispose()
