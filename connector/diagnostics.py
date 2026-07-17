"""Connection diagnostics shared by connector startup and --doctor."""

from __future__ import annotations

from dataclasses import dataclass
import socket
import ssl
from urllib.parse import urlparse

import httpx


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def explain_connection_error(exc: BaseException) -> str:
    """Turn common network failures into an actionable, token-safe message."""
    chain: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(chain) < 8:
        chain.append(str(current))
        current = current.__cause__ or current.__context__
    message = " | ".join(chain).lower()

    if isinstance(exc, ssl.SSLCertVerificationError) or "certificate_verify_failed" in message:
        return (
            "TLS certificate verification failed. Use the exact HTTPS hostname "
            "shown by Tailscale Serve; do not use https:// with a bare IP."
        )
    if isinstance(exc, socket.gaierror) or "getaddrinfo failed" in message or "name or service" in message:
        return "DNS lookup failed. Check Tailscale status, MagicDNS, and the server hostname."
    if isinstance(exc, (ConnectionRefusedError, httpx.ConnectError)) and "refused" in message:
        return "Connection refused. Check that deepbox Server and Tailscale Serve are running."
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)) or "timed out" in message:
        return "Connection timed out. Check Tailnet connectivity and Tailscale ACLs."
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 401:
            return "Authentication failed (401). Check or rotate this Devbox token."
        return f"Server returned HTTP {status}."
    return f"Connection failed: {exc.__class__.__name__}: {exc}"


def inspect_url(server_url: str) -> Check:
    parsed = urlparse(server_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return Check("server URL", False, "must be an http:// or https:// URL with a hostname")
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not local:
        return Check("server URL", False, "remote connectors must use HTTPS/WSS")
    return Check("server URL", True, server_url.rstrip("/"))


def run_doctor(server_url: str, token: str, protocol_version: int) -> list[Check]:
    """Probe URL, liveness, protocol compatibility, and token authentication."""
    checks = [inspect_url(server_url)]
    if not checks[-1].ok:
        return checks

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(base_url=server_url.rstrip("/"), timeout=10.0) as client:
            health_response = client.get("/api/health")
            health_response.raise_for_status()
            health = health_response.json()
            checks.append(Check("server health", health.get("status") == "ok", health.get("status", "invalid response")))

            server_protocol = health.get("protocol_version")
            checks.append(Check(
                "protocol",
                server_protocol == protocol_version,
                f"connector={protocol_version}, server={server_protocol}",
            ))

            if not token:
                checks.append(Check("authentication", False, "DEEPBOX_TOKEN is missing"))
            else:
                me_response = client.get("/api/me", headers=headers)
                me_response.raise_for_status()
                me = me_response.json()
                checks.append(Check(
                    "authentication", True,
                    f"Devbox {me.get('name', me.get('devbox_id', 'unknown'))}",
                ))
    except Exception as exc:
        checks.append(Check("connection", False, explain_connection_error(exc)))
    return checks
