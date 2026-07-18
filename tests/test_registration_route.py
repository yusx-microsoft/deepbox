"""Route-level tests for registration gating."""
import importlib
import os
import tempfile
import unittest
from unittest.mock import patch

_tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def build_app(registration_enabled: bool):
    """Reload the app with a fresh file DB and the given registration flag."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("DEEPBOX_")}
    dbfile = tempfile.mktemp(suffix=".db", dir=_tmpdir.name)
    env["DEEPBOX_DATABASE_URL"] = f"sqlite:///{dbfile.replace(os.sep, '/')}"
    env["DEEPBOX_REGISTRATION_ENABLED"] = "true" if registration_enabled else "false"
    with patch.dict(os.environ, env, clear=True):
        import server.app.config as config
        importlib.reload(config)
        import server.app.main as main
        importlib.reload(main)
        from fastapi.testclient import TestClient
        return TestClient(main.app)


class RegistrationRouteTests(unittest.TestCase):
    def test_register_rejected_when_disabled(self):
        client = build_app(registration_enabled=False)
        r = client.post("/api/auth/register",
                        json={"username": "demo", "password": "demo"})
        self.assertEqual(r.status_code, 403)

    def test_register_allowed_when_enabled(self):
        client = build_app(registration_enabled=True)
        r = client.post("/api/auth/register",
                        json={"username": "alice", "password": "s3cret-pw"})
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
