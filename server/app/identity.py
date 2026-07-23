"""Microsoft identity header parsing and account-name helpers.

Azure App Service Authentication (Easy Auth) validates Microsoft tokens before
requests reach deepbox.  This module deliberately only parses the trusted
headers; it never accepts browser-supplied bearer tokens or stores OAuth tokens.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Mapping

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_USERNAME_RE = re.compile(r"[^a-z0-9._-]+")


@dataclass(frozen=True)
class MicrosoftPrincipal:
    provider: str
    subject: str
    tenant_id: str
    email: str | None
    display_name: str


def normalize_email(value: object) -> str | None:
    """Return a conservative, case-insensitive email key or ``None``."""
    candidate = str(value or "").strip().casefold()
    if not candidate or len(candidate) > 320 or not _EMAIL_RE.fullmatch(candidate):
        return None
    return candidate


def normalize_tenant_id(value: object) -> str:
    """Normalize a tenant/issuer key for allow-list comparisons."""
    return str(value or "").strip().casefold()[:512]


def username_base(email: str | None, display_name: str, subject: str) -> str:
    """Produce a readable, URL-safe seed; callers add a uniqueness suffix."""
    source = email.split("@", 1)[0] if email else display_name
    source = _USERNAME_RE.sub("-", source.casefold()).strip("-._")
    if not source:
        source = f"microsoft-{subject[:8].casefold()}"
    return source[:48]


def normalize_username_hint(value: object) -> str:
    """Produce a stable username seed from an email or display-name hint."""
    candidate = str(value or "").strip()
    return username_base(normalize_email(candidate), candidate, "user")


def _decode_principal(raw: str) -> dict | None:
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.b64decode(padded, validate=True)
        value = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def parse_microsoft_principal(headers: Mapping[str, str]) -> MicrosoftPrincipal | None:
    """Parse the principal injected by Azure App Service Authentication.

    The caller must gate this helper behind an explicit Azure-auth setting.  A
    principal ID is mandatory; claim data only enriches that stable identity.
    """
    lower = {str(key).casefold(): str(value) for key, value in headers.items()}
    subject = lower.get("x-ms-client-principal-id", "").strip()
    provider = lower.get("x-ms-client-principal-idp", "aad").strip().casefold()
    if not subject or provider not in {"aad", "microsoft"}:
        return None

    principal = _decode_principal(lower.get("x-ms-client-principal", "")) or {}
    claims: dict[str, str] = {}
    for item in principal.get("claims", []):
        if not isinstance(item, dict):
            continue
        claim_type = str(item.get("typ") or "").strip()
        claim_value = str(item.get("val") or "").strip()
        if claim_type and claim_value and claim_type not in claims:
            claims[claim_type] = claim_value

    def claim(*names: str) -> str:
        for name in names:
            if claims.get(name):
                return claims[name]
        return ""

    tenant_id = claim(
        "tid",
        "http://schemas.microsoft.com/identity/claims/tenantid",
        "iss",
    ) or "microsoft-consumer"
    email = None
    for candidate in (
        claim(
            "email",
            "emails",
            "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
        ),
        claim("preferred_username", "unique_name", "upn"),
        lower.get("x-ms-client-principal-name", ""),
    ):
        email = normalize_email(candidate)
        if email:
            break
    display_name = claim("name", "given_name")
    if not display_name:
        display_name = email or lower.get("x-ms-client-principal-name", "") or "Microsoft user"
    return MicrosoftPrincipal(
        provider="microsoft",
        subject=subject,
        tenant_id=normalize_tenant_id(tenant_id),
        email=email,
        display_name=display_name[:120],
    )


def build_microsoft_principal(
    headers: Mapping[str, str], *, allow_debug_headers: bool = False,
) -> MicrosoftPrincipal | None:
    """Parse Easy Auth headers, optionally accepting explicit local-test headers.

    Debug headers are deliberately opt-in and the application disables them in
    production.  They make local identity integration tests possible without
    teaching the app to accept bearer tokens.
    """
    principal = parse_microsoft_principal(headers)
    if principal is not None or not allow_debug_headers:
        return principal
    lower = {str(key).casefold(): str(value) for key, value in headers.items()}
    subject = lower.get("x-deepbox-identity-subject", "").strip()
    if not subject:
        return None
    email = normalize_email(lower.get("x-deepbox-identity-email", ""))
    display_name = lower.get("x-deepbox-identity-name", "").strip()
    return MicrosoftPrincipal(
        provider="microsoft",
        subject=subject,
        tenant_id=normalize_tenant_id(
            lower.get("x-deepbox-identity-tenant", "microsoft-consumer")),
        email=email,
        display_name=(display_name or email or "Microsoft user")[:120],
    )
