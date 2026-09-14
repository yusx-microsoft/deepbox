import ssl
import unittest

from connector.diagnostics import checked_server_url, explain_connection_error, inspect_url


class ConnectorDiagnosticsTests(unittest.TestCase):
    def test_remote_http_is_rejected(self):
        check = inspect_url("http://deepbox.example.ts.net")
        self.assertFalse(check.ok)
        self.assertIn("HTTPS", check.detail)

    def test_local_http_is_allowed(self):
        self.assertTrue(inspect_url("http://127.0.0.1:8077").ok)
        self.assertTrue(inspect_url("http://localhost:8077").ok)

    def test_tls_error_message_is_actionable(self):
        exc = ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED")
        detail = explain_connection_error(exc)
        self.assertIn("Tailscale Serve", detail)
        self.assertNotIn("token", detail.lower())

    def test_dns_error_message_is_actionable(self):
        detail = explain_connection_error(OSError("getaddrinfo failed"))
        self.assertIn("MagicDNS", detail)

    def test_unknown_error_never_echoes_credentials_or_request_urls(self):
        secret = "Bearer do-not-print-this-token"
        detail = explain_connection_error(RuntimeError(
            f"request https://example.test/?token={secret} failed"))
        self.assertIn("RuntimeError", detail)
        self.assertNotIn(secret, detail)
        self.assertNotIn("https://", detail)

    def test_invalid_and_credential_bearing_server_urls_are_rejected_safely(self):
        urls = [
            "https://[invalid", "https://example.test:99999",
            "https://user:do-not-print@example.test",
            "https://example.test?token=do-not-print",
            "https://example.test/#do-not-print",
        ]
        for url in urls:
            with self.subTest(url=url):
                check = inspect_url(url)
                self.assertFalse(check.ok)
                self.assertNotIn("do-not-print", check.detail)

    def test_connector_rejects_unsafe_urls_before_creating_runtime_state(self):
        from connector.client import Connector

        for url in ("http://server.example", "https://user:secret@server.example"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                Connector(url, "test-token")
        self.assertEqual(checked_server_url("HTTPS://server.example/"), "https://server.example")

    def test_websocket_url_uses_the_same_https_validation(self):
        from connector.transport import ws_url

        self.assertEqual(ws_url("HTTPS://server.example/"), "wss://server.example/ws/devbox")
        self.assertEqual(ws_url("http://[::1]:12345/"), "ws://[::1]:12345/ws/devbox")
        with self.assertRaises(ValueError):
            ws_url("http://server.example")


if __name__ == "__main__":
    unittest.main()
