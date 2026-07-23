"""Pure-function tests for Microsoft principal parsing."""
import base64
import json

from server.app.identity import normalize_email, parse_microsoft_principal, username_base


def encoded_principal(claims):
    payload = {"auth_typ": "aad", "claims": [
        {"typ": key, "val": value} for key, value in claims.items()
    ]}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def test_parses_easy_auth_principal_without_retaining_tokens():
    principal = parse_microsoft_principal({
        "X-MS-CLIENT-PRINCIPAL-ID": "object-123",
        "X-MS-CLIENT-PRINCIPAL-IDP": "aad",
        "X-MS-CLIENT-PRINCIPAL-NAME": "fallback@example.com",
        "X-MS-CLIENT-PRINCIPAL": encoded_principal({
            "tid": "tenant-456",
            "email": "Person@Example.COM",
            "name": "Person Name",
        }),
        "X-MS-TOKEN-AAD-ACCESS-TOKEN": "must-not-be-read",
    })

    assert principal is not None
    assert principal.provider == "microsoft"
    assert principal.subject == "object-123"
    assert principal.tenant_id == "tenant-456"
    assert principal.email == "person@example.com"
    assert principal.display_name == "Person Name"
    assert not hasattr(principal, "access_token")


def test_rejects_missing_or_wrong_provider_and_tolerates_bad_claim_blob():
    assert parse_microsoft_principal({}) is None
    assert parse_microsoft_principal({
        "x-ms-client-principal-id": "abc",
        "x-ms-client-principal-idp": "github",
    }) is None

    principal = parse_microsoft_principal({
        "x-ms-client-principal-id": "abc",
        "x-ms-client-principal-name": "USER@example.com",
        "x-ms-client-principal": "not-base64",
    })
    assert principal is not None
    assert principal.email == "user@example.com"
    assert principal.tenant_id == "microsoft-consumer"


def test_email_and_username_normalization_are_conservative():
    assert normalize_email(" A@Example.com ") == "a@example.com"
    assert normalize_email("not-an-email") is None
    assert username_base("Sixing.Yu@example.com", "", "abc") == "sixing.yu"
    assert username_base(None, "Ada Lovelace", "abc") == "ada-lovelace"
