import os
import unittest
from pathlib import Path
from unittest.mock import patch

from server.app.config import DEFAULT_SECRET, Settings, load_settings


class SettingsTests(unittest.TestCase):
    def test_development_allows_local_origin_without_allowlist(self):
        settings = Settings(
            environment="development", secret=DEFAULT_SECRET,
            database_url="sqlite:///test.db", data_dir=Path("test-data"),
            public_url=None, allowed_origins=frozenset(),
            cookie_secure=False, cookie_samesite="lax", host="127.0.0.1", port=8077,
        )
        self.assertTrue(settings.origin_allowed("http://localhost:8077"))
        self.assertTrue(settings.origin_allowed(None))

    def test_production_requires_secret_origin_and_secure_cookie(self):
        settings = Settings(
            environment="production", secret=DEFAULT_SECRET,
            database_url="sqlite:///test.db", data_dir=Path("test-data"),
            public_url=None, allowed_origins=frozenset(),
            cookie_secure=False, cookie_samesite="lax", host="127.0.0.1", port=8077,
        )
        with self.assertRaisesRegex(RuntimeError, "DEEPBOX_SECRET"):
            settings.validate()

    def test_public_url_becomes_allowed_origin(self):
        env = {
            "DEEPBOX_ENV": "production",
            "DEEPBOX_SECRET": "a-long-production-secret",
            "DEEPBOX_PUBLIC_URL": "https://deepbox.example.ts.net/",
            "DEEPBOX_COOKIE_SECURE": "true",
            "DEEPBOX_PORT": "8077",
        }
        with patch.dict(os.environ, env, clear=False):
            settings = load_settings()
        self.assertEqual(settings.public_url, "https://deepbox.example.ts.net")
        self.assertTrue(settings.origin_allowed("https://deepbox.example.ts.net"))
        self.assertFalse(settings.origin_allowed("https://evil.example"))
        self.assertFalse(settings.origin_allowed(None))


if __name__ == "__main__":
    unittest.main()
