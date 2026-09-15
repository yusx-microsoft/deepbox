"""Offline product rename contract: aliases must not migrate machine identity."""

import ast
import asyncio
import hashlib
import hmac
import importlib
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agentbridge import product


@pytest.fixture(autouse=True)
def isolated_product_env(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(("AGENTBRIDGE_", "DEEPBOX_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("DEEPBOX_DATA_DIR", str(tmp_path / "data"))
    for key in ("PORT", "WEBSITES_PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


def test_display_name_is_separate_from_machine_identifiers(tmp_path):
    assert product.DISPLAY_NAME == "AgentBridge"
    assert product.NAME == "agentbridge"
    assert product.LEGACY_NAME == "deepbox"
    assert product.DISPLAY_NAME != product.NAME
    assert product.local_home() == tmp_path / ".agentbridge"
    assert not (tmp_path / ".AgentBridge").exists()


@pytest.mark.parametrize("key", ["TOKEN", "AGENTBRIDGE_TOKEN", "DEEPBOX_TOKEN"])
def test_env_precedence_including_explicit_empty(monkeypatch, key):
    assert product.env(key) is None
    assert product.env(key, "default") == "default"
    monkeypatch.setenv("DEEPBOX_TOKEN", "synthetic-legacy-token")
    assert product.env(key) == "synthetic-legacy-token"
    monkeypatch.setenv("AGENTBRIDGE_TOKEN", "synthetic-new-token")
    assert product.env(key) == "synthetic-new-token"
    monkeypatch.setenv("AGENTBRIDGE_TOKEN", "")
    assert product.env(key, "default") == ""
    monkeypatch.delenv("AGENTBRIDGE_TOKEN")
    monkeypatch.setenv("DEEPBOX_TOKEN", "")
    assert product.env(key, "default") == ""


def test_install_home_prefers_new_and_reuses_existing_legacy_without_io(tmp_path):
    canonical = tmp_path / ".agentbridge"
    legacy = tmp_path / ".deepbox"
    assert product.local_home() == canonical
    assert not canonical.exists()
    legacy.mkdir()
    with patch.object(Path, "open", side_effect=AssertionError("no credential reads")), \
            patch.object(Path, "mkdir", side_effect=AssertionError("no migration")):
        assert product.local_home() == legacy
    assert not canonical.exists()
    canonical.mkdir()
    assert product.local_home() == canonical


def test_install_home_env_precedence(monkeypatch, tmp_path):
    legacy = tmp_path / "old-install"
    canonical = tmp_path / "new-install"
    monkeypatch.setenv("DEEPBOX_HOME", str(legacy))
    assert product.local_home() == legacy
    monkeypatch.setenv("AGENTBRIDGE_HOME", str(canonical))
    assert product.local_home() == canonical
    assert not legacy.exists() and not canonical.exists()
    monkeypatch.setenv("AGENTBRIDGE_HOME", "")
    with pytest.raises(ValueError, match="HOME.*must not be empty"):
        product.local_home()


def test_imports_are_inert_and_entrypoint_delegates(monkeypatch):
    from agentbridge import __main__ as entrypoint
    from connector import cli

    calls = []
    monkeypatch.setattr(cli, "main", lambda argv=None: calls.append(argv) or 17)
    with patch.object(Path, "mkdir", side_effect=AssertionError("no import writes")), \
            patch.object(Path, "open", side_effect=AssertionError("no import reads")):
        importlib.reload(product)
        importlib.reload(entrypoint)
    assert calls == []
    assert entrypoint.main(["--help"]) == 17
    assert calls == [["--help"]]


@pytest.mark.parametrize("prefix", ["AGENTBRIDGE_", "DEEPBOX_"])
def test_server_config_accepts_both_prefixes(monkeypatch, tmp_path, prefix):
    from server.app.config import load_settings

    values = {
        "ENV": "production", "PLATFORM": "azure-app-service", "AUTH_MODE": "microsoft",
        "SECRET": "synthetic-session-secret-at-least-32-bytes",
        "PUBLIC_URL": "https://agentbridge.test", "ALLOWED_ORIGINS": "https://agentbridge.test",
        "COOKIE_SECURE": "true", "PORT": "8765", "HOST": "0.0.0.0",
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'existing.db').as_posix()}",
        "DATA_DIR": str(tmp_path / "recordings"), "REGISTRATION_ENABLED": "true",
        "DB_SIZE_WARN_MB": "7.5", "RATE_LIMIT_ENABLED": "false",
        "MICROSOFT_ALLOWED_TENANT_IDS": "tenant-one, tenant-two",
        "MICROSOFT_OWNER_EMAILS": "owner@agentbridge.test",
        "WORKSPACE_INVITATION_TTL_DAYS": "9",
    }
    for stem, value in values.items():
        monkeypatch.setenv(prefix + stem, value)
    settings = load_settings()
    assert settings.production and settings.platform == "azure-app-service"
    assert settings.auth_mode == "microsoft" and settings.cookie_secure
    assert settings.port == 8765 and settings.host == "0.0.0.0"
    assert settings.database_url == values["DATABASE_URL"]
    assert settings.data_dir == Path(values["DATA_DIR"]).resolve()
    assert settings.registration_enabled and not settings.rate_limit_enabled
    assert settings.db_size_warn_mb == 7.5
    assert settings.microsoft_allowed_tenant_ids == frozenset({"tenant-one", "tenant-two"})
    assert settings.workspace_invitation_ttl_days == 9


def test_server_canonical_and_empty_values_mask_legacy(monkeypatch):
    from server.app.config import load_settings

    monkeypatch.setenv("DEEPBOX_SECRET", "synthetic-legacy-secret")
    monkeypatch.setenv("AGENTBRIDGE_SECRET", "synthetic-canonical-secret")
    monkeypatch.setenv("DEEPBOX_PORT", "8765")
    monkeypatch.setenv("AGENTBRIDGE_PORT", "")
    monkeypatch.setenv("PORT", "8766")
    monkeypatch.setenv("DEEPBOX_REGISTRATION_ENABLED", "true")
    monkeypatch.setenv("AGENTBRIDGE_REGISTRATION_ENABLED", "")
    monkeypatch.setenv("DEEPBOX_PUBLIC_URL", "https://old.test")
    monkeypatch.setenv("AGENTBRIDGE_PUBLIC_URL", "")
    monkeypatch.setenv("DEEPBOX_BOOTSTRAP_TOKEN", "synthetic-must-not-be-used")
    monkeypatch.setenv("AGENTBRIDGE_BOOTSTRAP_TOKEN", "")
    settings = load_settings()
    assert settings.secret == "synthetic-canonical-secret"
    assert settings.port == 8766 and not settings.registration_enabled
    assert settings.public_url is None and settings.bootstrap_token_hash is None
    assert "AGENTBRIDGE_BOOTSTRAP_TOKEN" not in os.environ
    assert "DEEPBOX_BOOTSTRAP_TOKEN" not in os.environ


def test_empty_canonical_secret_fails_closed_in_production(monkeypatch):
    from server.app.config import load_settings

    monkeypatch.setenv("AGENTBRIDGE_ENV", "production")
    monkeypatch.setenv("DEEPBOX_SECRET", "synthetic-legacy-secret")
    monkeypatch.setenv("AGENTBRIDGE_SECRET", "")
    with pytest.raises(RuntimeError, match="AGENTBRIDGE_SECRET"):
        load_settings()


def test_bootstrap_hash_uses_canonical_and_scrubs_both_aliases(monkeypatch, capsys):
    from server.app.config import load_settings

    monkeypatch.setenv("DEEPBOX_BOOTSTRAP_TOKEN", "synthetic-old-bootstrap")
    monkeypatch.setenv("AGENTBRIDGE_BOOTSTRAP_TOKEN", "synthetic-new-bootstrap")
    settings = load_settings()
    assert settings.bootstrap_token_hash == hashlib.sha256(b"synthetic-new-bootstrap").hexdigest()
    assert "AGENTBRIDGE_BOOTSTRAP_TOKEN" not in os.environ
    assert "DEEPBOX_BOOTSTRAP_TOKEN" not in os.environ
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("prefix", ["AGENTBRIDGE_", "DEEPBOX_"])
def test_connector_env_reaches_only_mocked_transport(monkeypatch, prefix):
    from connector import client

    values = {"SERVER_URL": "https://agentbridge.test", "TOKEN": "synthetic-token",
              "MODE": "transport", "IPC_ENDPOINT": "synthetic-endpoint"}
    for stem, value in values.items():
        monkeypatch.setenv(prefix + stem, value)
    transport = AsyncMock()
    monkeypatch.setattr(client, "run_transport", transport)
    asyncio.run(client.main([]))
    transport.assert_awaited_once_with(values["SERVER_URL"], values["TOKEN"], values["IPC_ENDPOINT"])


def test_connector_empty_token_does_not_reuse_legacy_or_log_it(monkeypatch, capsys):
    from connector import client

    monkeypatch.setenv("DEEPBOX_TOKEN", "synthetic-must-not-be-logged")
    monkeypatch.setenv("AGENTBRIDGE_TOKEN", "")
    transport = AsyncMock()
    monkeypatch.setattr(client, "run_transport", transport)
    with pytest.raises(SystemExit, match="AGENTBRIDGE_TOKEN") as caught:
        asyncio.run(client.main(["--mode", "transport"]))
    transport.assert_not_awaited()
    captured = capsys.readouterr()
    assert "synthetic-must-not-be-logged" not in str(caught.value) + captured.out + captured.err


def test_empty_mode_is_safe_error_not_legacy_fallback(monkeypatch, capsys):
    from connector import client

    monkeypatch.setenv("DEEPBOX_MODE", "transport")
    monkeypatch.setenv("AGENTBRIDGE_MODE", "")
    with pytest.raises(SystemExit) as caught:
        asyncio.run(client.main(["--status"]))
    assert caught.value.code == 2
    assert "AGENTBRIDGE_MODE" in capsys.readouterr().err


def test_status_is_canonical_uses_override_and_never_outputs_tokens(monkeypatch, capsys):
    from connector import client

    monkeypatch.setenv("DEEPBOX_SERVER_URL", "https://old.test")
    monkeypatch.setenv("AGENTBRIDGE_SERVER_URL", "https://new.test/")
    monkeypatch.setenv("DEEPBOX_IPC_ENDPOINT", "old-endpoint")
    monkeypatch.setenv("AGENTBRIDGE_IPC_ENDPOINT", "new-endpoint")
    monkeypatch.setenv("AGENTBRIDGE_TOKEN", "synthetic-private-token")
    monkeypatch.setattr(client, "endpoint_exists", lambda endpoint: endpoint == "new-endpoint")
    monkeypatch.setattr(client, "read_secret", lambda: b"synthetic-private-ipc-secret")
    with pytest.raises(SystemExit) as caught:
        asyncio.run(client.main(["--status"]))
    assert caught.value.code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["product"] == "agentbridge" and payload["protocol_version"] == 3
    assert payload["server_url"] == "https://new.test"
    assert payload["ipc_endpoint"] == "new-endpoint"
    assert payload["ipc_endpoint_present"] and payload["supervisor_secret_present"]
    assert "synthetic-private" not in captured.out + captured.err


def test_ops_and_version_share_alias_precedence(monkeypatch):
    from server.app import version
    from server.ops.backup import _load_database_url

    monkeypatch.setattr(version, "_run_git", lambda args: None)
    monkeypatch.setenv("DEEPBOX_DATABASE_URL", "sqlite:///legacy.db")
    monkeypatch.setenv("DEEPBOX_GIT_COMMIT", "legacy-commit")
    assert _load_database_url(None) == "sqlite:///legacy.db"
    version.git_commit.cache_clear()
    assert version.git_commit() == "legacy-commit"
    monkeypatch.setenv("AGENTBRIDGE_DATABASE_URL", "sqlite:///canonical.db")
    monkeypatch.setenv("AGENTBRIDGE_GIT_COMMIT", "canonical-commit")
    assert _load_database_url(None) == "sqlite:///canonical.db"
    assert _load_database_url("sqlite:///explicit.db") == "sqlite:///explicit.db"
    version.git_commit.cache_clear()
    assert version.git_commit() == "canonical-commit"
    assert version.git_dirty() is False
    monkeypatch.setenv("AGENTBRIDGE_DATABASE_URL", "")
    monkeypatch.setenv("AGENTBRIDGE_GIT_COMMIT", "")
    assert _load_database_url(None) == ""
    version.git_commit.cache_clear()
    assert version.git_commit() == "unknown"
    version.git_commit.cache_clear()


@pytest.mark.parametrize("is_windows", [False, True])
def test_local_state_and_spool_keep_existing_roots(monkeypatch, tmp_path, is_windows):
    from connector import local_store, spool

    native_windows = local_store.IS_WIN
    monkeypatch.setattr(local_store, "IS_WIN", is_windows)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    expected = tmp_path / "deepbox"
    assert Path(local_store.default_state_root()) == expected
    assert Path(spool.default_spool_root()) == expected / "spool"
    monkeypatch.setattr(local_store, "IS_WIN", native_windows)
    project_dir = tmp_path / "synthetic-project"
    project_dir.mkdir()
    with local_store.LocalProjectStore() as store:
        before = store.add(str(project_dir), name="Existing project")
    monkeypatch.setenv("AGENTBRIDGE_HOME", str(tmp_path / "new-install"))
    with local_store.LocalProjectStore() as store:
        after = store.list_projects()
    assert [item.id for item in after] == [before.id]
    assert after[0].path == before.path
    assert not (tmp_path / "agentbridge").exists()


def test_spool_reopens_legacy_namespace_with_pending_ack_state(monkeypatch, tmp_path):
    from connector import local_store, spool

    monkeypatch.setattr(local_store, "IS_WIN", False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    server, token = "https://agentbridge.test", "synthetic-persisted-token"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    namespace = hashlib.sha256(f"{server}\x00{token_hash}".encode()).hexdigest()[:32]
    legacy_path = tmp_path / "deepbox" / "spool" / namespace / "spool.db"
    old = spool.DiskSpool(str(legacy_path))
    frame = {"type": "output", "session_id": "opaque-session", "pty_instance_id": "opaque-pty"}
    try:
        old.enqueue_output(dict(frame, data="acked"))
        old.enqueue_output(dict(frame, data="pending"))
        assert old.ack("opaque-session", "opaque-pty", 1)
    finally:
        old.close()
    reopened = spool.open_spool(server, token)
    try:
        assert [(r.seq, r.frame["data"]) for r in reopened.records()] == [(2, "pending")]
        assert reopened.last_acked("opaque-session", "opaque-pty") == 1
        assert reopened.enqueue_output(dict(frame, data="new")) == 3
    finally:
        reopened.close()


def test_ipc_identity_and_auth_domain_are_unchanged(monkeypatch, tmp_path):
    from connector import ipc

    monkeypatch.setattr(ipc, "IS_WIN", True)
    suffix = "persisted-user-key"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert ipc.default_endpoint(suffix) == r"\\.\pipe\deepbox-sessiond-persisted-user-key"
    legacy_secret = tmp_path / "deepbox" / f"sessiond-{suffix}.secret"
    assert Path(ipc.secret_path(suffix)) == legacy_secret
    legacy_secret.parent.mkdir()
    legacy_secret.write_text("synthetic-persisted-ipc-secret", encoding="ascii")
    with patch.object(ipc.secrets, "token_hex", side_effect=AssertionError("no identity rotation")):
        assert ipc.ensure_secret(suffix) == "synthetic-persisted-ipc-secret"
    monkeypatch.setattr(ipc, "IS_WIN", False)
    assert Path(ipc.default_endpoint(suffix)) == tmp_path / "deepbox" / f"sessiond-{suffix}.sock"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setattr(ipc.os.path, "expanduser", lambda path: str(tmp_path))
    assert Path(ipc.default_endpoint(suffix)) == tmp_path / ".deepbox" / "deepbox" / f"sessiond-{suffix}.sock"
    secret = "synthetic-persisted-ipc-secret"
    for role in ("client", "server"):
        expected = hmac.new(secret.encode("ascii"), f"{role}:nonce".encode("ascii"), hashlib.sha256).hexdigest()
        assert ipc._mac(secret, role, "nonce") == expected


def test_token_format_and_hash_remain_compatible():
    from server.app.util import hash_token, new_token

    existing = "hpc_box_" + "01" * 32
    assert hash_token(existing) == hashlib.sha256(existing.encode()).hexdigest()
    with patch("server.app.util.secrets.token_hex", return_value="01" * 32):
        token, hashed, preview = new_token()
    assert token == existing and hashed == hash_token(existing)
    assert preview == "hpc_box_010101…"


def test_product_env_reads_have_one_boundary():
    root = Path(__file__).resolve().parents[1]
    violations = []
    for package in ("connector", "server"):
        for path in (root / package).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_bytes())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in {"get", "getenv"} and node.args:
                        key = node.args[0]
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            if key.value.startswith(("AGENTBRIDGE_", "DEEPBOX_")):
                                violations.append(f"{path.name}:{node.lineno}")
                if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
                    if node.value.attr == "environ" and isinstance(node.ctx, ast.Load):
                        key = node.slice
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            if key.value.startswith(("AGENTBRIDGE_", "DEEPBOX_")):
                                violations.append(f"{path.name}:{node.lineno}")
    assert violations == []
