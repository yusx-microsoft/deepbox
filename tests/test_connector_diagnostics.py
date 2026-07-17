import ssl
import unittest

from connector.diagnostics import explain_connection_error, inspect_url


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


if __name__ == "__main__":
    unittest.main()
