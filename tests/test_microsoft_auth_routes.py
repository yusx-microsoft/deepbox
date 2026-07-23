import base64
from concurrent.futures import ThreadPoolExecutor
import importlib
import threading
import json
import os

from fastapi.testclient import TestClient
from sqlalchemy import func, select


def _principal_header(subject: str, email: str, name: str = "Test User",
                      tenant: str = "tenant-a") -> dict[str, str]:
    payload = {
        "auth_typ": "aad",
        "claims": [
            {"typ": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier",
             "val": subject},
            {"typ": "http://schemas.microsoft.com/identity/claims/tenantid",
             "val": tenant},
            {"typ": "preferred_username", "val": email},
            {"typ": "name", "val": name},
        ],
    }
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"X-MS-CLIENT-PRINCIPAL": encoded,
            "X-MS-CLIENT-PRINCIPAL-ID": subject,
            "X-MS-CLIENT-PRINCIPAL-IDP": "aad"}


def _build_app(tmp_path, owner_emails="owner@example.com"):
    db_path = tmp_path / "identity.db"
    updates = {
        "DEEPBOX_ENV": "test",
        "DEEPBOX_SECRET": "test-secret-for-microsoft-auth",
        "DEEPBOX_DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
        "DEEPBOX_DATA_DIR": str(tmp_path / "data"),
        "DEEPBOX_PUBLIC_URL": "https://deepbox.example",
        "DEEPBOX_AUTH_MODE": "microsoft",
        "DEEPBOX_MICROSOFT_OWNER_EMAILS": owner_emails,
        "DEEPBOX_BOOTSTRAP_TOKEN_HASH": "configured-but-disabled",
        "DEEPBOX_SESSION_TTL_SECONDS": "3600",
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


def test_microsoft_callback_provisions_owner_and_reuses_identity(tmp_path):
    client, main, models = _build_app(tmp_path)
    try:
        config = client.get("/api/auth/config")
        assert config.status_code == 200
        assert config.json() == {
            "mode": "microsoft",
            "password_enabled": False,
            "microsoft_enabled": True,
            "microsoft_login_url": "/api/auth/microsoft/start",
            "microsoft_logout_url": "/api/auth/microsoft/logout",
        }
        assert client.post("/api/auth/login", json={
            "username": "owner", "password": "irrelevant",
        }).status_code == 403
        assert client.get("/api/auth/microsoft/callback").status_code == 401
        assert client.get("/api/auth/bootstrap-status").json() == {"available": False}

        headers = _principal_header("owner-subject", "OWNER@example.com", "Owner Person")
        response = client.get(
            "/api/auth/microsoft/callback", headers=headers, follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/"
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "Max-Age=3600" in cookie

        me = client.get("/api/me/user")
        assert me.status_code == 200
        assert me.json()["email"] == "owner@example.com"
        assert me.json()["auth_provider"] == "microsoft"
        assert me.json()["role"] == "owner"
        owner_id = me.json()["id"]

        again = client.get(
            "/api/auth/microsoft/callback", headers=headers, follow_redirects=False)
        assert again.status_code == 302
        with models.SessionLocal() as session:
            assert session.scalar(select(func.count()).select_from(models.User)) == 1
            assert session.scalar(select(func.count()).select_from(models.Organization)) == 1
            assert session.scalar(select(func.count()).select_from(models.Workspace)) == 1
            assert session.get(models.User, owner_id).external_subject == "owner-subject"
    finally:
        client.close()
        models._engine.dispose()


def test_concurrent_first_login_creates_one_identity_and_personal_workspace(
        tmp_path):
    first, main, models = _build_app(tmp_path)
    second = TestClient(main.app)
    try:
        headers = _principal_header(
            "race-subject", "race@example.com", "Race User")
        barrier = threading.Barrier(2)

        def callback(client):
            barrier.wait()
            return client.get(
                "/api/auth/microsoft/callback", headers=headers,
                follow_redirects=False)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(callback, (first, second)))

        assert [response.status_code for response in responses] == [302, 302]
        with models.SessionLocal() as session:
            users = session.scalars(select(models.User).where(
                models.User.external_subject == "race-subject")).all()
            assert len(users) == 1
            organizations = session.scalars(select(models.Organization).where(
                models.Organization.owner_user_id == users[0].id,
                models.Organization.is_personal.is_(True),
            )).all()
            assert len(organizations) == 1
            workspaces = session.scalars(select(models.Workspace).where(
                models.Workspace.org_id == organizations[0].id,
                models.Workspace.is_personal.is_(True),
            )).all()
            assert len(workspaces) == 1
            memberships = session.scalars(select(models.Membership).where(
                models.Membership.workspace_id == workspaces[0].id,
                models.Membership.user_id == users[0].id,
            )).all()
            assert len(memberships) == 1
            assert memberships[0].role == "owner"
    finally:
        first.close()
        second.close()
        models._engine.dispose()


def test_allowlisted_identity_claims_a_single_legacy_owner(tmp_path):
    client, main, models = _build_app(tmp_path)
    try:
        with models.SessionLocal() as session:
            legacy = models.User(
                id="legacy-owner", username="legacy", password_hash="legacy-hash",
                display_name="Legacy Owner", role=models.ROLE_OWNER,
            )
            session.add(legacy)
            session.flush()
            session.add(models.BootstrapState(id=1, owner_user_id=legacy.id))
            session.commit()

        response = client.get(
            "/api/auth/microsoft/callback",
            headers=_principal_header("claimed-subject", "owner@example.com"),
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert client.get("/api/me/user").json()["id"] == "legacy-owner"
        with models.SessionLocal() as session:
            assert session.scalar(select(func.count()).select_from(models.User)) == 1
            linked = session.get(models.User, "legacy-owner")
            assert linked.auth_provider == "microsoft"
            assert linked.external_tenant_id == "tenant-a"
            assert linked.external_subject == "claimed-subject"
            assert linked.password_hash == "legacy-hash"
    finally:
        client.close()
        models._engine.dispose()


def test_existing_microsoft_identity_recovers_missing_personal_workspace(tmp_path):
    client, main, models = _build_app(tmp_path)
    try:
        with models.SessionLocal() as session:
            user = models.User(
                id="partial-login", username="partial", password_hash="!microsoft",
                display_name="Partial Login", role=models.ROLE_MEMBER,
                email="partial@example.com", auth_provider="microsoft",
                external_tenant_id="tenant-a", external_subject="partial-subject",
            )
            session.add(user)
            session.commit()

        response = client.get(
            "/api/auth/microsoft/callback",
            headers=_principal_header(
                "partial-subject", "partial@example.com", "Recovered User"),
            follow_redirects=False,
        )
        assert response.status_code == 302
        workspaces = client.get("/api/workspaces").json()
        assert len(workspaces) == 1
        assert workspaces[0]["name"] == "Personal"
        assert workspaces[0]["is_personal"] is True
        with models.SessionLocal() as session:
            assert session.scalar(select(func.count()).select_from(
                models.Organization)) == 1
            assert session.scalar(select(func.count()).select_from(
                models.Workspace)) == 1
            assert session.scalar(select(func.count()).select_from(
                models.Membership)) == 1
    finally:
        client.close()
        models._engine.dispose()


def test_unlisted_microsoft_account_is_a_member(tmp_path):
    client, main, models = _build_app(tmp_path)
    try:
        response = client.get(
            "/api/auth/microsoft/callback",
            headers=_principal_header("member-subject", "member@example.com"),
            follow_redirects=False,
        )
        assert response.status_code == 302
        me = client.get("/api/me/user").json()
        assert me["role"] == "member"
        assert me["email"] == "member@example.com"
        workspaces = client.get("/api/workspaces").json()
        assert len(workspaces) == 1
        assert workspaces[0]["is_personal"] is True
        assert workspaces[0]["role"] == "owner"
        assert client.post("/api/workspaces", json={"name": ""}).status_code == 422
        assert client.post("/api/workspaces", json={"name": "   "}).status_code == 422
    finally:
        client.close()
        models._engine.dispose()
