"""Route-level tests for P1 Cut 1: bootstrap, roles, invitations, lifecycle.

All tests run against an isolated file-backed SQLite DB and the FastAPI
TestClient. They never touch live Azure or user resources.
"""
import hashlib
import importlib
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

_tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def build_app(bootstrap_token=None, registration_enabled=False):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DEEPBOX_")}
    dbfile = tempfile.mktemp(suffix=".db", dir=_tmpdir.name)
    env["DEEPBOX_DATABASE_URL"] = f"sqlite:///{dbfile.replace(os.sep, '/')}"
    env["DEEPBOX_REGISTRATION_ENABLED"] = "true" if registration_enabled else "false"
    if bootstrap_token is not None:
        env["DEEPBOX_BOOTSTRAP_TOKEN"] = bootstrap_token
    with patch.dict(os.environ, env, clear=True):
        import server.app.config as config
        importlib.reload(config)
        import server.app.models as models
        importlib.reload(models)
        import server.app.main as main
        importlib.reload(main)
        from fastapi.testclient import TestClient
        return TestClient(main.app), dbfile


class WebInviteSecurityTests(unittest.TestCase):
    def test_invite_link_uses_fragment_and_is_removed_from_address_bar(self):
        source = (Path(__file__).parents[1] / "web" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("#invite=${encodeURIComponent(res.token)}", source)
        self.assertNotIn("?invite=${encodeURIComponent(res.token)}", source)
        self.assertIn("history.replaceState(null, '', location.pathname)", source)
        self.assertIn('value="${escapeHtml(inviteFromUrl)}"', source)


class MigrationTests(unittest.TestCase):
    def test_legacy_user_gets_member_role_without_owner_guessing(self):
        import sqlite3
        import server.app.models as models
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        db = sqlite3.connect(path)
        db.execute(
            'CREATE TABLE user (id VARCHAR PRIMARY KEY, username VARCHAR UNIQUE, '
            'password_hash VARCHAR NOT NULL, display_name VARCHAR NOT NULL, '
            'created_at DATETIME NOT NULL)'
        )
        db.execute(
            'INSERT INTO user VALUES (?, ?, ?, ?, ?)',
            ("legacy-id", "legacy", "hash", "Legacy", "2026-01-01 00:00:00"),
        )
        db.commit()
        db.close()

        models.init_db(f"sqlite:///{path}")
        db = sqlite3.connect(path)
        columns = {row[1] for row in db.execute('PRAGMA table_info(user)')}
        role, disabled_at = db.execute(
            'SELECT role, disabled_at FROM user WHERE id = ?', ("legacy-id",)
        ).fetchone()
        db.close()
        models._engine.dispose()
        self.assertIn("role", columns)
        self.assertIn("disabled_at", columns)
        self.assertEqual(role, "member")
        self.assertIsNone(disabled_at)


class BootstrapTests(unittest.TestCase):
    def test_status_available_then_unavailable(self):
        client, _ = build_app(bootstrap_token="tok-123")
        self.assertTrue(client.get("/api/auth/bootstrap-status").json()["available"])
        r = client.post("/api/auth/bootstrap", json={
            "token": "tok-123", "username": "boss", "password": "pw-strong"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(client.get("/api/auth/bootstrap-status").json()["available"])

    def test_bootstrap_creates_owner_role(self):
        client, _ = build_app(bootstrap_token="tok-123")
        created = client.post("/api/auth/bootstrap", json={
            "token": "tok-123", "username": "boss", "password": "pw-strong"})
        self.assertEqual(created.json()["role"], "owner")
        me = client.get("/api/me/user").json()
        self.assertEqual(me["role"], "owner")

    def test_bootstrap_only_once(self):
        client, _ = build_app(bootstrap_token="tok-123")
        client.post("/api/auth/bootstrap", json={
            "token": "tok-123", "username": "boss", "password": "pw-strong"})
        r = client.post("/api/auth/bootstrap", json={
            "token": "tok-123", "username": "boss2", "password": "pw-strong"})
        self.assertEqual(r.status_code, 404)

    def test_wrong_token_generic_404_no_leak(self):
        client, _ = build_app(bootstrap_token="tok-123")
        r = client.post("/api/auth/bootstrap", json={
            "token": "wrong", "username": "x", "password": "pw-strong"})
        self.assertEqual(r.status_code, 404)
        body = r.text
        self.assertNotIn("tok-123", body)
        self.assertNotIn(hashlib.sha256(b"tok-123").hexdigest(), body)

    def test_status_never_leaks_token_or_hash(self):
        client, _ = build_app(bootstrap_token="tok-123")
        body = client.get("/api/auth/bootstrap-status").text
        self.assertNotIn("tok-123", body)
        self.assertNotIn(hashlib.sha256(b"tok-123").hexdigest(), body)

    def test_no_token_configured_returns_404(self):
        client, _ = build_app(bootstrap_token=None)
        self.assertFalse(client.get("/api/auth/bootstrap-status").json()["available"])
        r = client.post("/api/auth/bootstrap", json={
            "token": "anything", "username": "x", "password": "pw"})
        self.assertEqual(r.status_code, 404)

    def test_concurrent_bootstrap_single_winner(self):
        client, _ = build_app(bootstrap_token="tok-123")
        results = []
        lock = threading.Lock()

        def attempt(name):
            r = client.post("/api/auth/bootstrap", json={
                "token": "tok-123", "username": name, "password": "pw-strong"})
            with lock:
                results.append(r.status_code)

        threads = [threading.Thread(target=attempt, args=(f"u{i}",))
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(200), 1)
        self.assertTrue(all(c in (200, 404) for c in results))


class RegistrationInviteTests(unittest.TestCase):
    def _owner_client(self):
        client, _ = build_app(bootstrap_token="tok-123")
        client.post("/api/auth/bootstrap", json={
            "token": "tok-123", "username": "boss", "password": "pw-strong"})
        return client

    def test_owner_only_invitation_endpoints(self):
        client = self._owner_client()
        # Register a member via invite, then log in as them.
        inv = client.post("/api/invitations", json={"note": "a"}).json()
        client.post("/api/auth/register", json={
            "username": "mem", "password": "pw-strong",
            "invite_code": inv["token"]})
        # now logged in as member; owner endpoints must be forbidden
        r = client.get("/api/invitations")
        self.assertEqual(r.status_code, 403)
        r = client.get("/api/users")
        self.assertEqual(r.status_code, 403)

    def test_invitation_one_time_and_member_role(self):
        client = self._owner_client()
        inv = client.post("/api/invitations", json={}).json()
        token = inv["token"]
        r = client.post("/api/auth/register", json={
            "username": "mem", "password": "pw-strong", "invite_code": token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(client.get("/api/me/user").json()["role"], "member")
        # Second use must fail (single-use).
        r2 = client.post("/api/auth/register", json={
            "username": "mem2", "password": "pw-strong", "invite_code": token})
        self.assertEqual(r2.status_code, 404)

    def test_invitation_list_and_no_plaintext(self):
        client = self._owner_client()
        inv = client.post("/api/invitations", json={"note": "x"}).json()
        listing = client.get("/api/invitations")
        self.assertNotIn(inv["token"], listing.text)
        self.assertNotIn(hashlib.sha256(inv["token"].encode()).hexdigest(),
                         listing.text)
        row = listing.json()[0]
        self.assertTrue(row["created_at"].endswith("+00:00"))
        self.assertTrue(row["expires_at"].endswith("+00:00"))

    def test_invitation_db_never_stores_plaintext(self):
        client, dbfile = build_app(bootstrap_token="tok-123")
        client.post("/api/auth/bootstrap", json={
            "token": "tok-123", "username": "boss", "password": "pw-strong"})
        inv = client.post("/api/invitations", json={}).json()
        with open(dbfile, "rb") as f:
            raw = f.read()
        self.assertNotIn(inv["token"].encode(), raw)

    def test_invitation_revoke(self):
        client = self._owner_client()
        inv = client.post("/api/invitations", json={}).json()
        r = client.delete(f"/api/invitations/{inv['id']}")
        self.assertEqual(r.status_code, 200)
        r2 = client.post("/api/auth/register", json={
            "username": "mem", "password": "pw-strong",
            "invite_code": inv["token"]})
        self.assertEqual(r2.status_code, 404)

    def test_invitation_expiry(self):
        import datetime as dt
        import server.app.models as models
        client = self._owner_client()
        inv = client.post("/api/invitations", json={}).json()
        # Force-expire the invite directly in the DB.
        s = models.SessionLocal()
        row = s.get(models.Invitation, inv["id"])
        row.expires_at = models.now() - dt.timedelta(hours=1)
        s.commit()
        s.close()
        r = client.post("/api/auth/register", json={
            "username": "mem", "password": "pw-strong",
            "invite_code": inv["token"]})
        self.assertEqual(r.status_code, 404)

    def test_invalid_invite_generic_404(self):
        client = self._owner_client()
        r = client.post("/api/auth/register", json={
            "username": "mem", "password": "pw-strong",
            "invite_code": "nope"})
        self.assertEqual(r.status_code, 404)
        # A bad code must not reveal that an account name already exists.
        occupied = client.post("/api/auth/register", json={
            "username": "boss", "password": "pw-strong",
            "invite_code": "nope"})
        self.assertEqual(occupied.status_code, 404)
        self.assertEqual(occupied.json()["detail"], r.json()["detail"])


class LifecycleTests(unittest.TestCase):
    def _owner_and_member(self):
        client, _ = build_app(bootstrap_token="tok-123")
        client.post("/api/auth/bootstrap", json={
            "token": "tok-123", "username": "boss", "password": "pw-strong"})
        inv = client.post("/api/invitations", json={}).json()
        client.post("/api/auth/register", json={
            "username": "mem", "password": "pw-strong",
            "invite_code": inv["token"]})
        # Return to owner session.
        client.post("/api/auth/login", json={
            "username": "boss", "password": "pw-strong"})
        users = client.get("/api/users").json()
        member = next(u for u in users if u["username"] == "mem")
        return client, member

    def test_disable_and_reenable_member(self):
        client, member = self._owner_and_member()
        r = client.post(f"/api/users/{member['id']}/disable")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["disabled"])
        # Disabled member cannot log in.
        r2 = client.post("/api/auth/login", json={
            "username": "mem", "password": "pw-strong"})
        self.assertEqual(r2.status_code, 401)
        self.assertEqual(r2.json()["detail"], "bad credentials")
        # Re-enable via owner (re-login as owner first).
        client.post("/api/auth/login", json={
            "username": "boss", "password": "pw-strong"})
        r3 = client.post(f"/api/users/{member['id']}/enable")
        self.assertEqual(r3.status_code, 200)
        r4 = client.post("/api/auth/login", json={
            "username": "mem", "password": "pw-strong"})
        self.assertEqual(r4.status_code, 200)

    def test_disabled_member_session_rejected(self):
        client, member = self._owner_and_member()
        from fastapi.testclient import TestClient
        import server.app.main as main
        # Member session in its own cookie jar.
        member_client = TestClient(main.app)
        member_client.post("/api/auth/login", json={
            "username": "mem", "password": "pw-strong"})
        self.assertEqual(member_client.get("/api/me/user").status_code, 200)
        token = member_client.post(
            "/api/devboxes", json={"name": "member-box"}
        ).json()["token"]
        bearer = {"Authorization": f"Bearer {token}"}
        self.assertEqual(member_client.get("/api/me", headers=bearer).status_code, 200)
        # Owner disables the member.
        client.post(f"/api/users/{member['id']}/disable")
        # Existing browser sessions and connector credentials now fail.
        self.assertEqual(member_client.get("/api/me/user").status_code, 403)
        self.assertEqual(member_client.get("/api/me", headers=bearer).status_code, 401)

    def test_cannot_disable_last_owner(self):
        client, _ = build_app(bootstrap_token="tok-123")
        client.post("/api/auth/bootstrap", json={
            "token": "tok-123", "username": "boss", "password": "pw-strong"})
        me = client.get("/api/me/user").json()
        r = client.post(f"/api/users/{me['id']}/disable")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
