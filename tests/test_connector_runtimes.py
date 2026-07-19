"""Unit tests for the connector runtime adapter registry (planning.md Cut 7).

These tests are connector-only and require no real CLI to be installed.
"""
from __future__ import annotations

import sys

import pytest

from connector import runtimes
from connector.pty_session import resolve_cmd


# ---------------------------------------------------------------------------
# Registry: uniqueness and lookup
# ---------------------------------------------------------------------------

def test_registry_ids_are_unique():
    ids = runtimes.runtime_ids()
    assert len(ids) == len(set(ids)), "runtime ids must be unique"


def test_expected_runtimes_registered():
    for rid in ("mock", "claude-code", "copilot-cli", "codex-cli"):
        assert runtimes.has(rid)
        assert runtimes.get(rid).id == rid


def test_register_rejects_duplicate():
    existing = runtimes.get("claude-code")
    with pytest.raises(ValueError):
        runtimes.register(runtimes.RuntimeAdapter(
            id="claude-code", label="dup", base_argv=("claude",)))
    # Original untouched.
    assert runtimes.get("claude-code") is existing


def test_get_unknown_raises_unknown_runtime():
    with pytest.raises(runtimes.UnknownRuntimeError):
        runtimes.get("does-not-exist")


def test_build_command_unknown_runtime_fails():
    with pytest.raises(runtimes.UnknownRuntimeError):
        runtimes.build_command("nope")


# ---------------------------------------------------------------------------
# Exact command argv per runtime / model / permission mode
# ---------------------------------------------------------------------------

def test_mock_base_command_uses_current_interpreter():
    assert runtimes.build_command("mock") == [
        sys.executable, "-u", "-m", "connector.mockcli"]


def test_claude_default_is_base_argv():
    # No model / permission -> exactly the historical base command.
    assert runtimes.build_command("claude-code") == ["claude"]


def test_claude_model_and_permission_argv():
    assert runtimes.build_command(
        "claude-code", model="opus", permission_mode="plan") == [
        "claude", "--model", "opus", "--permission-mode", "plan"]


def test_claude_bypass_permissions_argv():
    assert runtimes.build_command(
        "claude-code", permission_mode="bypassPermissions") == [
        "claude", "--dangerously-skip-permissions"]


def test_copilot_model_and_allow_all_argv():
    assert runtimes.build_command(
        "copilot-cli", model="gpt-5", permission_mode="allowAll") == [
        "copilot", "--model", "gpt-5", "--allow-all-tools"]


def test_codex_full_auto_argv():
    assert runtimes.build_command(
        "codex-cli", model="gpt-5-codex", permission_mode="full-auto") == [
        "codex", "--model", "gpt-5-codex",
        "--ask-for-approval", "never", "--sandbox", "workspace-write"]


def test_codex_default_permission_argv():
    assert runtimes.build_command("codex-cli", permission_mode="default") == [
        "codex", "--ask-for-approval", "on-request"]


def test_unsupported_model_rejected():
    with pytest.raises(runtimes.InvalidCommandError):
        runtimes.build_command("claude-code", model="totally-fake-model")


def test_unsupported_permission_mode_rejected():
    with pytest.raises(runtimes.InvalidCommandError):
        runtimes.build_command("claude-code", permission_mode="fake-mode")


# ---------------------------------------------------------------------------
# Security: executable / argv validation, no shell metacharacters
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "claude; rm -rf /", "cla ude", "../bin/claude", "a|b", "$(x)", "a&b", "",
])
def test_validate_executable_rejects_bad_names(bad):
    with pytest.raises(runtimes.InvalidCommandError):
        runtimes.validate_executable(bad)


def test_validate_program_allows_paths_but_blocks_metachars():
    # Absolute interpreter path is fine.
    assert runtimes.validate_program(sys.executable) == sys.executable
    with pytest.raises(runtimes.InvalidCommandError):
        runtimes.validate_program("/bin/sh; echo hi")


@pytest.mark.parametrize("tok", ["a;b", "a|b", "`x`", "$(x)", "a\nb", ""])
def test_validate_argv_rejects_bad_tokens(tok):
    with pytest.raises(runtimes.InvalidCommandError):
        runtimes.validate_argv(["claude", tok])


def test_register_rejects_pathy_executable():
    with pytest.raises(runtimes.InvalidCommandError):
        runtimes.register(runtimes.RuntimeAdapter(
            id="pathy", label="x", base_argv=("/bin/sh",)))


# ---------------------------------------------------------------------------
# Adding an adapter is localized (no edits to builder/other adapters needed)
# ---------------------------------------------------------------------------

def test_adding_adapter_is_localized():
    before = set(runtimes.runtime_ids())
    assert "temp-runtime" not in before
    new = runtimes.RuntimeAdapter(
        id="temp-runtime",
        label="Temp",
        base_argv=("mytool",),
        model_flag="-m",
        models=("x1",),
        permission_modes={"": (), "safe": ("--safe",)},
    )
    try:
        runtimes.register(new)
        # The *shared* builder handles it with zero changes.
        assert runtimes.build_command("temp-runtime") == ["mytool"]
        assert runtimes.build_command(
            "temp-runtime", model="x1", permission_mode="safe") == [
            "mytool", "-m", "x1", "--safe"]
        # Every other adapter's output is unchanged.
        assert runtimes.build_command("claude-code") == ["claude"]
    finally:
        runtimes._REGISTRY.pop("temp-runtime", None)
    assert set(runtimes.runtime_ids()) == before


# ---------------------------------------------------------------------------
# resolve_cmd integration preserves CLI behavior
# ---------------------------------------------------------------------------

def test_resolve_cmd_defaults_preserved():
    assert resolve_cmd("mock", None) == [
        sys.executable, "-u", "-m", "connector.mockcli"]
    assert resolve_cmd("claude-code", None) == ["claude"]


def test_resolve_cmd_unknown_runtime_falls_back_to_mock():
    assert resolve_cmd("bogus", None) == [
        sys.executable, "-u", "-m", "connector.mockcli"]


def test_resolve_cmd_explicit_launch_cmd_wins_and_is_validated():
    assert resolve_cmd("claude-code", "claude --model opus") == [
        "claude", "--model", "opus"]
    with pytest.raises(runtimes.InvalidCommandError):
        resolve_cmd("claude-code", "claude; rm -rf /")


def test_resolve_cmd_passes_model_and_permission():
    assert resolve_cmd("codex-cli", None, model="o4-mini",
                       permission_mode="auto") == [
        "codex", "--model", "o4-mini", "--ask-for-approval", "on-failure"]


def test_capabilities_blob_has_no_secrets():
    caps = runtimes.get("claude-code").capabilities(installed=True, version="1.2.3")
    assert caps["runtime"] == "claude-code"
    assert caps["installed"] is True
    assert "features" in caps
    # Sanity: nothing that looks like a token/secret key.
    text = repr(caps).lower()
    assert "token" not in text and "secret" not in text and "password" not in text
