import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install.ps1"
INSTALLER_SH = ROOT / "scripts" / "install.sh"
POWERSHELL = shutil.which("powershell.exe")
SOURCE_ZIP = "https://github.com/yusx-swapp/AgentBridge/archive/refs/heads/main.zip"
INSTALL_PS1_URL = "https://raw.githubusercontent.com/yusx-swapp/AgentBridge/main/scripts/install.ps1"
INSTALL_SH_URL = "https://raw.githubusercontent.com/yusx-swapp/AgentBridge/main/scripts/install.sh"
PINNED_SOURCE_ZIP = "https://example.invalid/fork/AgentBridge-feature.zip?ref=pinned&version=2"
LEGACY_SOURCE_ZIP = "https://example.invalid/fork/deepbox-main.zip?ref=legacy&version=1"
SOURCE_ZIP_CASES = [
    pytest.param(None, None, SOURCE_ZIP, id="default-canonical-repository"),
    pytest.param(None, LEGACY_SOURCE_ZIP, LEGACY_SOURCE_ZIP, id="legacy-override"),
    pytest.param(PINNED_SOURCE_ZIP, None, PINNED_SOURCE_ZIP, id="canonical-override"),
    pytest.param(PINNED_SOURCE_ZIP, LEGACY_SOURCE_ZIP, PINNED_SOURCE_ZIP, id="canonical-wins"),
    pytest.param(PINNED_SOURCE_ZIP, "", PINNED_SOURCE_ZIP, id="canonical-beats-empty-legacy"),
    pytest.param("", LEGACY_SOURCE_ZIP, None, id="empty-canonical-blocks-legacy"),
    pytest.param("", None, None, id="empty-canonical-blocks-default"),
    pytest.param(None, "", None, id="empty-legacy-blocks-default"),
    pytest.param("", "", None, id="both-empty"),
]
ARCHIVE_CASES = [
    (folder, None) for folder in (
        "AgentBridge-main", "AgentBridge-feature-rename", "deepbox-main",
        "deepbox-feature-rename", "deepbox-pinned-branch", "arbitrary-source-root",
    )
] + [
    (folder, missing)
    for folder in ("AgentBridge-main", "deepbox-main")
    for missing in (
        "agentbridge/__init__.py", "agentbridge/__main__.py",
        "connector/__init__.py", "requirements-connector.txt",
    )
]


def _helper_prefix() -> str:
    text = INSTALLER.read_text(encoding="utf-8")
    prefix, marker, _rest = text.partition("# --- Config")
    assert marker
    return prefix


def _test_env(**overrides: str) -> dict[str, str]:
    # No real configuration, credentials, shell startup files, or Python imports
    # may leak into these extracted-helper / disposable-venv fixtures.
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith(("AGENTBRIDGE_", "DEEPBOX_"))
        and key.upper() not in {"PYTHONPATH", "PYTHONHOME", "BASH_ENV", "ENV"}
    }
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env.update(overrides)
    return env


def _run_powershell(
    script: str, tmp_path: Path, *, env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    script_path = tmp_path / "installer-helper-test.ps1"
    script_path.write_text(script, encoding="utf-8")
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script_path)],
        env=_test_env() if env is None else env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_windows_installer_stops_connector_before_replacing_source():
    text = INSTALLER.read_text(encoding="utf-8")
    refresh = text.index("# Refresh the app folder")
    stop = text.index("Stop-RunningConnectors -VenvPython $VenvPy", refresh)
    remove = text.index("Remove-DirectoryWithRetry -Path $Src", refresh)
    copy = text.index("Copy-Item -Recurse -Force", refresh)
    assert refresh < stop < remove < copy
    assert "Command lines are inspected but never printed" in text


def test_windows_installer_creates_stable_command_outside_app():
    text = INSTALLER.read_text(encoding="utf-8")
    command_body = text.split('$commandBody = @"', 1)[1].split('"@', 1)[0]

    assert "$Command = Join-Path $Bin 'agentbridge.cmd'" in text
    assert "$LegacyCommand = Join-Path $Bin 'deepbox.cmd'" in text
    assert 'if /I "%~1"=="upgrade" goto upgrade' in command_body
    assert '-I -u -m agentbridge %*' in command_body
    assert command_body.count("powershell.exe") == 1
    assert "DEEPBOX_INSTALL_ONLY=1" in command_body
    assert "AGENTBRIDGE_INSTALL_ONLY=1" in command_body
    assert 'for %%I in ("%~dp0..") do set "INSTALL_ROOT=%%~fI"' in command_body
    assert 'set "AGENTBRIDGE_HOME=%INSTALL_ROOT%"' in command_body
    assert 'set "DEEPBOX_HOME=%INSTALL_ROOT%"' in command_body
    assert "DisableDelayedExpansion" in command_body
    assert "%USERPROFILE%\\.deepbox" not in command_body
    assert "PYTHONPATH" not in command_body
    assert "Remove-DirectoryWithRetry" not in command_body
    assert "deepbox-app.pth" in text
    assert "Path(sys.prefix).parent / 'app'" in text
    assert "Add-UserPathEntry -Path $Bin" in text


def test_fallback_connector_dependencies_include_skill_frontmatter_parser():
    assert "'PyYAML>=6.0'" in INSTALLER.read_text(encoding="utf-8")
    assert "'PyYAML>=6.0'" in INSTALLER_SH.read_text(encoding="utf-8")


def test_unix_installer_creates_stable_command_outside_app():
    text = INSTALLER_SH.read_text(encoding="utf-8")
    command_body = text.split("cat > \"$COMMAND\" <<'EOF'", 1)[1].split("\nEOF", 1)[0]

    assert 'COMMAND="${BIN}/agentbridge"' in text
    assert 'LEGACY_COMMAND="${BIN}/deepbox"' in text
    assert 'if [ "${1:-}" = "upgrade" ]; then' in command_body
    assert '-I -u -m agentbridge "$@"' in command_body
    assert command_body.count("curl -fsSL") == 1
    assert "DEEPBOX_INSTALL_ONLY=1" in command_body
    assert "AGENTBRIDGE_INSTALL_ONLY=1" in command_body
    assert '${BASH_SOURCE[0]}' in command_body
    assert 'export DEEPBOX_HOME="$ROOT"' in command_body
    assert 'export AGENTBRIDGE_HOME="$ROOT"' in command_body
    assert "_APP_BIN" in text
    assert "shlex.quote" in text
    assert 'BIN_LITERAL="$("$PY" -c' in text
    assert "$PYTHON" not in text
    assert "PYTHONPATH" not in command_body
    assert 'rm -rf "$SRC"' not in command_body
    assert "deepbox-app.pth" in text
    assert "Path(sys.prefix).parent / 'app'" in text
    assert "agentbridge command path" in text


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
def test_connector_process_matcher_is_scoped_to_venv_and_module(tmp_path):
    target = r"C:\Users\Test User\.agentbridge\venv\Scripts\python.exe"
    other = r"C:\Python312\python.exe"
    cases = [
        {"ExecutablePath": target, "CommandLine": f'"{target}" -u -m connector'},
        {"ExecutablePath": target, "CommandLine": f'"{target}" -u -m connector.cli connect'},
        {"ExecutablePath": target, "CommandLine": f'"{target}" -m pip list'},
        {"ExecutablePath": other, "CommandLine": f'"{other}" -m connector'},
        {"ExecutablePath": None, "CommandLine": f'"{target}" -m connector --mode supervisor'},
        {"ExecutablePath": target, "CommandLine": f'"{target}" -m connector_tools'},
        {"ExecutablePath": target, "CommandLine": f'"{target}" -I -u -m agentbridge connect'},
        {"ExecutablePath": None, "CommandLine": f'"{target}" -I -m agentbridge doctor'},
        {"ExecutablePath": other, "CommandLine": f'"{other}" -m agentbridge'},
        {"ExecutablePath": target, "CommandLine": f'"{target}" -m agentbridge_tools'},
        {"ExecutablePath": target, "CommandLine": f'"{target}" -m agentbridge.cli'},
        {"ExecutablePath": target, "CommandLine": f'"{target}" -c "print(\' -m agentbridge \')"'},
        {"ExecutablePath": target, "CommandLine": f'"{target}" -m pip -m agentbridge'},
        {"ExecutablePath": target, "CommandLine": f'"{target}" script.py -m connector'},
        {"ExecutablePath": None, "CommandLine": f'"{target}.other" -m agentbridge'},
        {"ExecutablePath": target, "CommandLine": f'"{target}" -I -m "agentbridge" connect'},
        {"ExecutablePath": None, "CommandLine": f'"{other}" -m agentbridge --python "{target}"'},
    ]
    payload = json.dumps(cases).replace("'", "''")
    target_ps = target.replace("'", "''")
    result = _run_powershell(
        _helper_prefix()
        + f"\n$target = '{target_ps}'\n"
        + f"$cases = ConvertFrom-Json '{payload}'\n"
        + "$results = @($cases | ForEach-Object { "
        + "Test-ConnectorProcess -Process $_ -VenvPython $target })\n"
        + "ConvertTo-Json -Compress -InputObject @($results)\n",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == [
        True,
        True,
        False,
        False,
        True,
        False,
        True,
        True,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        True,
        False,
    ]


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
def test_process_tree_includes_only_connector_descendants(tmp_path):
    processes = [
        {"ProcessId": 10, "ParentProcessId": 1},
        {"ProcessId": 11, "ParentProcessId": 10},
        {"ProcessId": 12, "ParentProcessId": 11},
        {"ProcessId": 20, "ParentProcessId": 1},
    ]
    payload = json.dumps(processes).replace("'", "''")
    result = _run_powershell(
        _helper_prefix()
        + f"\n$processes = ConvertFrom-Json '{payload}'\n"
        + "$ids = @(Get-ProcessTreeIds -Processes $processes "
        + "-RootProcessIds 10) | Sort-Object\n"
        + "ConvertTo-Json -Compress -InputObject @($ids)\n",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == [10, 11, 12]


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
@pytest.mark.parametrize("module,root_name", [
    ("connector", ".deepbox"),
    ("connector.cli", "Custom Install ! & (kept)"),
    ("agentbridge", ".agentbridge"),
])
def test_installer_discovers_and_stops_a_running_venv_connector(tmp_path, module, root_name):
    # Never import/start the real connector: this existing isolated fixture is
    # a temporary package whose only behavior is sleeping with a child process.
    venv = tmp_path / root_name / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=_test_env(),
    )
    venv_python = venv / "Scripts" / "python.exe"
    work = tmp_path / "work"
    package = work / module.split(".")[0]
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    # File existence must mean the full PID is readable, not just that open() ran.
    (package / ("cli.py" if module == "connector.cli" else "__main__.py")).write_text(
        "from pathlib import Path\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)'])\n"
        "Path('child.pid.tmp').write_text(str(child.pid), encoding='ascii')\n"
        "Path('child.pid.tmp').replace('child.pid')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    sleeper = subprocess.Popen(
        [str(venv_python), "-m", module],
        cwd=work,
        env=_test_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    child_pid_file = work / "child.pid"
    child_pid = None
    child_stopped = False
    try:
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text(encoding="ascii"))
        assert sleeper.poll() is None
        target_ps = str(venv_python).replace("'", "''")
        result = _run_powershell(
            _helper_prefix()
            + f"\nStop-RunningConnectors -VenvPython '{target_ps}'\n",
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        sleeper.wait(timeout=5)
        assert "Existing connector stopped." in result.stdout
        child_check = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"if (Get-Process -Id {child_pid} -ErrorAction SilentlyContinue) {{ exit 1 }}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert child_check.returncode == 0
        child_stopped = True
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)
        if child_pid is not None and not child_stopped:
            subprocess.run(
                [
                    POWERSHELL,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"Stop-Process -Id {child_pid} -Force -ErrorAction SilentlyContinue",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
def test_remove_directory_helper_removes_an_unlocked_tree(tmp_path):
    target = tmp_path / "app"
    target.mkdir()
    (target / "connector.py").write_text("pass\n", encoding="utf-8")
    target_ps = str(target).replace("'", "''")
    result = _run_powershell(
        _helper_prefix()
        + f"\nRemove-DirectoryWithRetry -Path '{target_ps}' "
        + "-Attempts 2 -DelayMilliseconds 1\n",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert not target.exists()


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
def test_remove_directory_helper_retries_a_transient_cwd_lock(tmp_path):
    target = tmp_path / "locked-app"
    target.mkdir()
    ready = tmp_path / "locker-ready"
    env = _test_env(INSTALLER_TEST_LOCK_PATH=str(target), INSTALLER_TEST_READY_PATH=str(ready))
    locker = subprocess.Popen(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Set-Location -LiteralPath $env:INSTALLER_TEST_LOCK_PATH; "
            "[IO.File]::WriteAllText($env:INSTALLER_TEST_READY_PATH, 'ready'); "
            "Start-Sleep -Milliseconds 700",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        target_ps = str(target).replace("'", "''")
        result = _run_powershell(
            _helper_prefix()
            + f"\nRemove-DirectoryWithRetry -Path '{target_ps}' "
            + "-Attempts 20 -DelayMilliseconds 100\n",
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert not target.exists()
    finally:
        if locker.poll() is None:
            locker.kill()
        locker.wait(timeout=5)


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
def test_windows_environment_boundary_preserves_empty_canonical_values(tmp_path):
    result = _run_powershell(
        _helper_prefix()
        + "\n$cases = @(\n"
        + "(Get-ProductEnvironment TOKEN 'default' @{}),\n"
        + "(Get-ProductEnvironment TOKEN 'default' @{DEEPBOX_TOKEN='old'}),\n"
        + "(Get-ProductEnvironment TOKEN 'default' @{AGENTBRIDGE_TOKEN='new';DEEPBOX_TOKEN='old'}),\n"
        + "(Get-ProductEnvironment TOKEN 'default' @{AGENTBRIDGE_TOKEN='';DEEPBOX_TOKEN='old'}),\n"
        + "(Get-ProductEnvironment INSTALL_ONLY '0' @{AGENTBRIDGE_INSTALL_ONLY='';DEEPBOX_INSTALL_ONLY='1'}),\n"
        + "(Get-ProductEnvironment TOKEN 'default')\n)\n"
        + "ConvertTo-Json -Compress -InputObject $cases\n",
        tmp_path,
        env=_test_env(AGENTBRIDGE_TOKEN="", DEEPBOX_TOKEN="must-not-be-revived"),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == ["default", "old", "new", "", "", ""]
    assert "must-not-be-revived" not in result.stdout + result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
@pytest.mark.parametrize("canonical,legacy,expected", SOURCE_ZIP_CASES)
def test_windows_source_zip_precedence_and_empty_validation(tmp_path, canonical, legacy, expected):
    # Extract only the actual SOURCE_ZIP assignment/guard and pure helpers, not
    # the installer body: no downloads, process discovery, home changes, or CLI.
    lines = INSTALLER.read_text(encoding="utf-8").splitlines()
    assignment = next(line for line in lines if line.startswith("$SourceZip = "))
    validation = next(line for line in lines if line.startswith("if ([string]::IsNullOrWhiteSpace($SourceZip))"))
    env = _test_env()
    if canonical is not None:
        env["AGENTBRIDGE_SOURCE_ZIP"] = canonical
    if legacy is not None:
        env["DEEPBOX_SOURCE_ZIP"] = legacy
    result = _run_powershell(
        _helper_prefix() + f"\n{assignment}\n{validation}\n"
        + "ConvertTo-Json -Compress -InputObject $SourceZip\n",
        tmp_path, env=env,
    )
    if expected is None:
        assert result.returncode != 0
        assert "SOURCE_ZIP setting is empty" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.strip()) == expected


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
@pytest.mark.parametrize("layout,setting,expected", [
    ("fresh", "none", ".agentbridge"),
    ("legacy", "none", ".deepbox"),
    ("both", "none", ".agentbridge"),
    ("both", "legacy", "custom root ! & (kept)"),
    ("legacy", "canonical", "custom root ! & (kept)"),
    ("legacy", "empty", None),
])
def test_windows_root_selection_keeps_existing_and_custom_data(tmp_path, layout, setting, expected):
    home = tmp_path / "home"
    home.mkdir()
    if layout in {"legacy", "both"}:
        (home / ".deepbox").mkdir()
    if layout == "both":
        (home / ".agentbridge").mkdir()
    custom = home / "custom root ! & (kept)"
    custom.mkdir()
    sentinel = custom / "identity-and-spool-fixture.txt"
    sentinel.write_text("fixture state must stay in place", encoding="utf-8")
    env = _test_env(INSTALLER_TEST_HOME=str(home))
    if setting == "legacy":
        env["DEEPBOX_HOME"] = str(custom)
    elif setting in {"canonical", "empty"}:
        env["DEEPBOX_HOME"] = str(home / ".deepbox")
        env["AGENTBRIDGE_HOME"] = "" if setting == "empty" else str(custom)
    result = _run_powershell(
        _helper_prefix() + "\nGet-InstallRoot -HomeDirectory $env:INSTALLER_TEST_HOME\n",
        tmp_path, env=env,
    )
    if expected is None:
        assert result.returncode != 0
        assert "HOME setting is empty" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert Path(result.stdout.strip()) == home / expected
    assert sentinel.read_text(encoding="utf-8") == "fixture state must stay in place"
    if layout == "legacy":
        assert not (home / ".agentbridge").exists()


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
def test_windows_path_preference_is_pure_and_preserves_unrelated_entries(tmp_path):
    # Exercise only the pure helper: never modify this machine's user/system PATH.
    target = r"C:\User Space ! & (x)\custom\bin"
    existing = ';'.join([r"%SystemRoot%\System32", f'"{target.upper()}\\"', r"C:\Other Bin", target])
    result = _run_powershell(
        _helper_prefix()
        + f"\nGet-PreferredPath -Path '{target}' -CurrentPath '{existing}'\n",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ';'.join([target, r"%SystemRoot%\System32", r"C:\Other Bin"])
    text = INSTALLER.read_text(encoding="utf-8")
    assert "SetEnvironmentVariable('Path', $newUserPath, 'User')" in text
    assert "SetEnvironmentVariable('Path', $env:Path" not in text
    assert "setx" not in text.lower()


def _archive_fixture(tmp_path: Path, folder: str, missing: str | None) -> tuple[Path, Path]:
    extract = tmp_path / "extracted"
    source = extract / folder
    for name in ("agentbridge/__init__.py", "agentbridge/__main__.py", "connector/__init__.py", "requirements-connector.txt"):
        if name != missing:
            file = source / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("", encoding="utf-8")
    return extract, source


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
@pytest.mark.parametrize("folder,missing", ARCHIVE_CASES)
def test_windows_archive_requires_both_packages_before_replacement(tmp_path, folder, missing):
    extract, source = _archive_fixture(tmp_path, folder, missing)
    result = _run_powershell(
        _helper_prefix() + "\nGet-ArchiveSource -ExtractDirectory $env:INSTALLER_TEST_EXTRACT\n",
        tmp_path, env=_test_env(INSTALLER_TEST_EXTRACT=str(extract)),
    )
    if missing:
        assert result.returncode != 0
        assert "existing installation was not replaced" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert Path(result.stdout.strip()) == source


@pytest.mark.parametrize("folder,missing", ARCHIVE_CASES)
def test_unix_archive_selection_uses_the_same_package_contract(tmp_path, monkeypatch, capsys, folder, missing):
    # Execute only the archive-validation Python fragment, with synthetic files.
    text = INSTALLER_SH.read_text(encoding="utf-8")
    code = text.split('INNER="$("$PY" - "$EXTRACT" <<\'PY\'\n', 1)[1].split("\nPY", 1)[0]
    extract, source = _archive_fixture(tmp_path, folder, missing)
    monkeypatch.setattr(sys, "argv", ["archive-fixture", str(extract)])
    if missing:
        with pytest.raises(SystemExit, match="existing installation was not replaced"):
            exec(compile(code, "<installer-archive-fixture>", "exec"), {})
    else:
        exec(compile(code, "<installer-archive-fixture>", "exec"), {})
        assert Path(capsys.readouterr().out.strip()) == source


@pytest.mark.parametrize("installer,install_url", [
    (INSTALLER, INSTALL_PS1_URL), (INSTALLER_SH, INSTALL_SH_URL),
])
def test_installer_headers_downloads_and_upgrades_use_only_the_canonical_repository(installer, install_url):
    text = installer.read_text(encoding="utf-8")
    # Check every GitHub URL, not just the presence of one correct URL beside a
    # stale fallback. The fork and old/nonexistent repositories are never defaults.
    github_urls = set(re.findall(r"https?://(?:raw\.githubusercontent\.com|github\.com)/[^\s'\"`<>]+", text))
    assert github_urls == {SOURCE_ZIP, install_url}
    assert install_url in text.split("# --- Config", 1)[0]
    if installer == INSTALLER:
        assert f"$SourceZip = Get-ProductEnvironment -Stem 'SOURCE_ZIP' -Default '{SOURCE_ZIP}'" in text
        assert "Invoke-WebRequest -Uri $SourceZip -OutFile $tmpZip" in text
        assert f"$InstallScriptUrl = '{install_url}'" in text
        upgrade = _powershell_template("commandBody").split(":upgrade\n", 1)[1]
        assert "irm '$InstallScriptUrl' | iex" in upgrade
    else:
        assert f"SOURCE_ZIP=\"$(product_env SOURCE_ZIP '{SOURCE_ZIP}')\"" in text
        assert 'curl -fsSL "$SOURCE_ZIP" -o "$ZIP"' in text
        assert 'wget -qO "$ZIP" "$SOURCE_ZIP"' in text
        upgrade = _shell_template("COMMAND").split('if [ "${1:-}" = "upgrade" ]; then', 1)[1]
        assert f'URL="{install_url}"' in upgrade
        assert 'curl -fsSL "$URL" | bash' in upgrade
        assert 'wget -qO- "$URL" | bash' in upgrade


def test_installers_copy_only_selected_packages_and_preserve_environment_boundary():
    windows = INSTALLER.read_text(encoding="utf-8")
    unix = INSTALLER_SH.read_text(encoding="utf-8")
    assert "foreach ($package in @('agentbridge', 'connector'))" in windows
    assert windows.index("$archiveSource = Get-ArchiveSource") < windows.index("Remove-DirectoryWithRetry -Path $Src")
    assert "for package in agentbridge connector; do" in unix
    assert unix.index('INNER="$("$PY"') < unix.index('rm -rf "$SRC"')
    # Product environment resolution happens only at the compatibility boundary,
    # not via token expansion in generated scripts or shell profiles.
    assert "$env:DEEPBOX_TOKEN" not in windows and "$env:AGENTBRIDGE_TOKEN" not in windows
    assert "${DEEPBOX_TOKEN:-" not in unix and "${AGENTBRIDGE_TOKEN:-" not in unix
    assert "Get-ProductEnvironment -Stem 'INSTALL_ONLY'" in windows
    assert 'INSTALL_ONLY="$(product_env INSTALL_ONLY 0)"' in unix


def _powershell_template(name: str) -> str:
    text = INSTALLER.read_text(encoding="utf-8")
    return text.split(f'${name} = @"\n', 1)[1].split('\n"@', 1)[0] + "\n"


def _shell_template(variable: str) -> str:
    text = INSTALLER_SH.read_text(encoding="utf-8")
    return text.split(f'cat > "${variable}" <<\'EOF\'\n', 1)[1].split("\nEOF", 1)[0] + "\n"


@pytest.fixture
def shim_installation(tmp_path):
    # Same isolated --without-pip fixture style as process discovery above.
    # Only fake packages exist here; neither installer nor real connector runs.
    root = tmp_path / "custom root ! & (kept) %literal% O'Brien \u540d" / ".deepbox"
    venv = root / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)],
        check=True, capture_output=True, text=True, timeout=30, env=_test_env(),
    )
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    site_packages = json.loads(subprocess.run(
        [str(python), "-I", "-c", "import json, site; print(json.dumps(site.getsitepackages()[0]))"],
        check=True, capture_output=True, text=True, timeout=10, env=_test_env(),
    ).stdout.strip())
    if POWERSHELL is not None:
        result = _run_powershell(
            _helper_prefix()
            + "\n$actual = Get-VenvSitePackages -Python $env:INSTALLER_TEST_PYTHON\n"
            + "if ($actual -cne $env:INSTALLER_TEST_SITE) { throw 'Wrong Unicode site-packages path' }\n"
            + "Write-Output 'ok'\n",
            tmp_path,
            env=_test_env(INSTALLER_TEST_PYTHON=str(python), INSTALLER_TEST_SITE=site_packages),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "ok"
    (Path(site_packages) / "deepbox-app.pth").write_text(
        "import sys; from pathlib import Path; sys.path.insert(0, str(Path(sys.prefix).parent / 'app'))\n",
        encoding="ascii",
    )
    for name in ("agentbridge", "connector"):
        package = root / "app" / name
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
    (root / "app/agentbridge/__main__.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "print(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd(),\n"
        "    'root': os.environ.get('AGENTBRIDGE_HOME'),\n"
        "    'legacy_root': os.environ.get('DEEPBOX_HOME'),\n"
        "    'package': str(Path(__file__).resolve()), 'prefix': sys.prefix}))\n"
        "sys.exit(7 if '--fixture-fail' in sys.argv else 0)\n",
        encoding="utf-8",
    )
    (root / "bin").mkdir()
    windows = os.name == "nt"
    commands = {
        root / ("bin/agentbridge.cmd" if windows else "bin/agentbridge"):
            _powershell_template("commandBody") if windows else _shell_template("COMMAND"),
        root / ("bin/deepbox.cmd" if windows else "bin/deepbox"):
            _powershell_template("aliasBody") if windows else _shell_template("ALIAS_TMP"),
        root / ("deepbox-connect.cmd" if windows else "deepbox-connect.sh"):
            _powershell_template("launcherBody") if windows else _shell_template("LAUNCHER"),
    }
    for path, body in commands.items():
        # Fixture hard stop: even an accidental upgrade dispatch cannot download.
        body = body.replace("powershell.exe -NoProfile", "exit /b 98\nrem -NoProfile")
        body = body.replace('if [ "${1:-}" = "upgrade" ]; then', 'if [ "${1:-}" = "upgrade" ]; then\n  exit 98')
        path.write_text(body, encoding="ascii", newline="\r\n" if windows else "\n")
        if not windows:
            path.chmod(0o700)
    caller = tmp_path / "untrusted-caller"
    (caller / "agentbridge").mkdir(parents=True)
    (caller / "agentbridge/__init__.py").write_text("raise RuntimeError('untrusted cwd imported')", encoding="utf-8")
    sentinel = root / "identity-and-spool-fixture.txt"
    sentinel.write_bytes(b"not a real credential; do not relocate this fixture")
    return root, caller, commands, sentinel


def test_native_shims_share_one_root_and_venv_without_reinstalling(shim_installation, tmp_path):
    root, caller, commands, sentinel = shim_installation
    snapshots = {path: path.read_bytes() for path in commands}
    state_before = sentinel.read_bytes()
    env = _test_env(
        AGENTBRIDGE_HOME=str(tmp_path / "unrelated-canonical"),
        DEEPBOX_HOME=str(tmp_path / "unrelated-legacy"),
        AGENTBRIDGE_TOKEN="fake-secret-not-to-be-printed",
        DEEPBOX_TOKEN="fake-legacy-secret-not-to-be-printed",
        PYTHONPATH=str(caller),
    )
    for command in commands:
        for failure in (False, True):
            args = ["--folder", "space ! & (x) %literal%"]
            if failure:
                args += ["--fixture-fail"]
            expected = ["connect", *args]
            supplied = args if command.name.startswith("deepbox-connect") else expected
            if os.name == "nt":
                runner = tmp_path / "shim-fixture.cmd"
                # The real shim is ASCII and root-relative. Pass the Unicode
                # fixture path via the process environment, not an OEM-encoded
                # batch literal; preserve literal percent signs in arguments.
                invocation = '"%INSTALLER_TEST_COMMAND%" ' + subprocess.list2cmdline(supplied).replace("%", "%%")
                runner.write_text("@echo off\nsetlocal DisableDelayedExpansion\n" + invocation + "\n", encoding="ascii")
                argv = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(runner)]
            else:
                argv = [str(command), *supplied]
            result = subprocess.run(
                argv, cwd=caller, env={**env, "INSTALLER_TEST_COMMAND": str(command)},
                capture_output=True, text=True, timeout=15,
            )
            assert result.returncode == (7 if failure else 0), result.stdout + result.stderr
            payload = json.loads(result.stdout.strip())
            assert payload["args"] == expected
            assert Path(payload["cwd"]) == caller
            assert Path(payload["root"]) == root
            assert Path(payload["legacy_root"]) == root
            assert Path(payload["prefix"]) == root / "venv"
            assert Path(payload["package"]) == root / "app/agentbridge/__main__.py"
            assert "fake-secret" not in result.stdout + result.stderr
            assert "fake-legacy-secret" not in result.stdout + result.stderr
    assert sentinel.read_bytes() == state_before
    assert {path: path.read_bytes() for path in commands} == snapshots
    assert not (tmp_path / "unrelated-canonical").exists()
    assert not (tmp_path / "unrelated-legacy").exists()


def test_local_cmd_launchers_delegate_configuration_without_token_expansion():
    connector = (ROOT / "scripts/start-connector.cmd").read_text(encoding="utf-8")
    server = (ROOT / "scripts/start-server.cmd").read_text(encoding="utf-8")
    assert '".venv\\Scripts\\python.exe" -u -m agentbridge connect %*' in connector
    assert "set /p" not in connector.lower()
    assert "%DEEPBOX_TOKEN%" not in connector and "%AGENTBRIDGE_TOKEN%" not in connector
    assert '".venv\\Scripts\\python.exe" -m server' in server
    for text in (connector, server):
        assert "DisableDelayedExpansion" in text
        assert "[agentbridge]" in text and "[deepbox]" not in text
        assert 'cd /d "%~dp0.."' in text


# On Windows use Git Bash only, never the WSL launcher (which may provision a distro).
if os.name == "nt":
    BASH = next((str(p) for p in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Git/bin/bash.exe",
    ) if p.is_file()), None)
else:
    BASH = shutil.which("bash")


def _run_bash_fragment(fragment: str, tmp_path: Path, env: dict[str, str]):
    path = tmp_path / "extracted-shell-helper.sh"
    path.write_text(fragment, encoding="utf-8", newline="\n")
    return subprocess.run(
        [BASH, "--noprofile", "--norc", path.as_posix()],
        env=env, capture_output=True, text=True, timeout=15,
    )


@pytest.mark.skipif(BASH is None, reason="Native Bash is unavailable")
@pytest.mark.parametrize("canonical,legacy,expected", [
    (None, None, "default"), (None, "old", "old"),
    ("new", "old", "new"), ("", "old", ""), (None, "", ""),
])
def test_bash_environment_boundary_preserves_explicit_empty(tmp_path, canonical, legacy, expected):
    fragment = INSTALLER_SH.read_text(encoding="utf-8").split("# --- Config", 1)[0]
    env = _test_env()
    if canonical is not None:
        env["AGENTBRIDGE_TOKEN"] = canonical
    if legacy is not None:
        env["DEEPBOX_TOKEN"] = legacy
    result = _run_bash_fragment(fragment + "\nproduct_env TOKEN default\n", tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


@pytest.mark.skipif(BASH is None, reason="Native Bash is unavailable")
@pytest.mark.parametrize("canonical,legacy,expected", SOURCE_ZIP_CASES)
def test_bash_source_zip_precedence_and_empty_validation(tmp_path, canonical, legacy, expected):
    # Execute only the helpers and SOURCE_ZIP configuration; no installer/CLI.
    text = INSTALLER_SH.read_text(encoding="utf-8")
    lines = text.splitlines()
    assignment = next(line for line in lines if line.startswith("SOURCE_ZIP="))
    validation = next(line for line in lines if line.startswith('[ -n "$SOURCE_ZIP" ]'))
    env = _test_env()
    if canonical is not None:
        env["AGENTBRIDGE_SOURCE_ZIP"] = canonical
    if legacy is not None:
        env["DEEPBOX_SOURCE_ZIP"] = legacy
    result = _run_bash_fragment(
        text.split("# --- Config", 1)[0] + f"\n{assignment}\n{validation}\n"
        + 'printf \'%s\' "$SOURCE_ZIP"\n',
        tmp_path, env,
    )
    if expected is None:
        assert result.returncode != 0
        assert "SOURCE_ZIP setting is empty" in result.stdout + result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout == expected


@pytest.mark.skipif(BASH is None, reason="Native Bash is unavailable")
@pytest.mark.parametrize("layout,setting,expected", [
    ("fresh", "none", ".agentbridge"), ("legacy", "none", ".deepbox"),
    ("both", "none", ".agentbridge"), ("legacy", "legacy", "custom root"),
    ("legacy", "canonical", "custom root"), ("legacy", "empty", ""),
])
def test_bash_root_selection_never_migrates_data(tmp_path, layout, setting, expected):
    home = tmp_path / "home"
    home.mkdir()
    if layout in {"legacy", "both"}:
        (home / ".deepbox").mkdir()
    if layout == "both":
        (home / ".agentbridge").mkdir()
    env = _test_env(INSTALLER_TEST_HOME=home.as_posix())
    if setting == "legacy":
        env["DEEPBOX_HOME"] = (home / "custom root").as_posix()
    elif setting in {"canonical", "empty"}:
        env["DEEPBOX_HOME"] = (home / ".deepbox").as_posix()
        env["AGENTBRIDGE_HOME"] = "" if setting == "empty" else (home / "custom root").as_posix()
    fragment = INSTALLER_SH.read_text(encoding="utf-8").split("# --- Config", 1)[0]
    result = _run_bash_fragment(fragment + '\nselect_install_root "$INSTALLER_TEST_HOME"\n', tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ((home / expected).as_posix() if expected else "")
    if layout == "legacy":
        assert not (home / ".agentbridge").exists()
    assert not (home / "custom root").exists()


@pytest.mark.skipif(BASH is None, reason="Native Bash is unavailable")
@pytest.mark.parametrize("canonical,legacy,expected", [
    (None, None, "/home/deepbox"), (None, "/home/deepbox", "/home/deepbox"),
    (None, "/home/existing-custom", "/home/existing-custom"),
    ("/home/canonical", "/home/deepbox", "/home/canonical"),
    ("", "/home/deepbox", ""), (None, "", ""),
])
def test_azure_startup_environment_bridge_keeps_persisted_legacy_data(tmp_path, canonical, legacy, expected):
    # Extract only environment normalization; never start Gunicorn or bind a port.
    startup = (ROOT / "azure-startup.sh").read_text(encoding="utf-8")
    fragment = startup.split("app_root=", 1)[0]
    assert "exec " not in fragment
    env = _test_env(AGENTBRIDGE_TOKEN="", DEEPBOX_TOKEN="do-not-revive-or-log")
    if canonical is not None:
        env["AGENTBRIDGE_DATA_DIR"] = canonical
    if legacy is not None:
        env["DEEPBOX_DATA_DIR"] = legacy
    result = _run_bash_fragment(
        fragment + '\n[ "$AGENTBRIDGE_TOKEN" = "" ] || exit 99\nprintf \'%s\' "$AGENTBRIDGE_DATA_DIR"\n',
        tmp_path, env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
    assert "do-not-revive-or-log" not in result.stdout + result.stderr


@pytest.mark.skipif(BASH is None, reason="Native Bash is unavailable")
def test_azure_startup_with_no_legacy_keys_still_defaults_outside_wwwroot(tmp_path):
    fragment = (ROOT / "azure-startup.sh").read_text(encoding="utf-8").split("app_root=", 1)[0]
    result = _run_bash_fragment(
        fragment + '\nprintf \'%s\' "$AGENTBRIDGE_DATA_DIR"\n', tmp_path, _test_env(),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "/home/deepbox"


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
def test_windows_path_helper_rejects_path_separators_without_changing_path(tmp_path):
    result = _run_powershell(
        _helper_prefix()
        + "\nGet-PreferredPath -Path 'C:\\custom;root\\bin' -CurrentPath 'C:\\Untouched'\n",
        tmp_path,
    )
    assert result.returncode != 0
    assert "PATH cannot represent" in result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
def test_windows_full_installer_parses_without_execution(tmp_path):
    result = _run_powershell(
        "$ErrorActionPreference = 'Stop'\n$tokens = $null; $errors = $null\n"
        "[Management.Automation.Language.Parser]::ParseFile("
        "$env:INSTALLER_TEST_SOURCE, [ref]$tokens, [ref]$errors) | Out-Null\n"
        "if ($errors.Count) { throw 'Installer syntax errors' }\n",
        tmp_path, env=_test_env(INSTALLER_TEST_SOURCE=str(INSTALLER)),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(BASH is None, reason="Native Bash is unavailable")
@pytest.mark.parametrize("source", [INSTALLER_SH, ROOT / "azure-startup.sh"])
def test_shell_scripts_parse_without_execution(tmp_path, source):
    fixture = tmp_path / "parse-only.sh"
    fixture.write_bytes(source.read_bytes())
    result = subprocess.run(
        [BASH, "--noprofile", "--norc", "-n", fixture.as_posix()],
        env=_test_env(), capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
@pytest.mark.parametrize("kind", ["fresh", "old-source", "canonical", "unknown"])
def test_windows_existing_command_is_preserved_and_old_source_is_explained(tmp_path, kind):
    command = tmp_path / "agentbridge.cmd"
    before = None
    if kind != "fresh":
        marker = "agentbridge-stable-shim-v1" if kind != "unknown" else "user-owned"
        url = INSTALL_PS1_URL if kind == "canonical" else "https://example.invalid/old-install.ps1"
        before = f"@echo off\r\nrem {marker}\r\nrem {url}\r\n".encode("ascii")
        command.write_bytes(before)
    text = INSTALLER.read_text(encoding="utf-8")
    block = text[text.index('$commandBody = @"'):text.index('$aliasBody = @"')]
    # Only command-file generation, never source refresh/process stop/pip/connect.
    result = _run_powershell(
        _helper_prefix() + '\n$Command = $env:INSTALLER_TEST_COMMAND\n'
        + f"$InstallScriptUrl = '{INSTALL_PS1_URL}'\n" + block,
        tmp_path, env=_test_env(INSTALLER_TEST_COMMAND=str(command)),
    )
    assert (result.returncode == 0) == (kind != "unknown"), result.stderr
    if before is not None:
        assert command.read_bytes() == before
    else:
        assert INSTALL_PS1_URL in command.read_text(encoding="ascii")
    output = result.stdout + result.stderr
    if kind == "old-source":
        assert "keeps its original upgrade source" in output
        assert f"irm {INSTALL_PS1_URL} | iex" in output
    elif kind != "unknown":
        assert "keeps its original upgrade source" not in output
    else:
        assert "unrecognized command" in output


@pytest.mark.skipif(BASH is None, reason="Native Bash is unavailable")
@pytest.mark.parametrize("kind", ["fresh", "old-source", "canonical", "unknown"])
def test_unix_existing_command_is_preserved_and_old_source_is_explained(tmp_path, kind):
    command = tmp_path / "agentbridge"
    before = None
    if kind != "fresh":
        marker = "agentbridge-stable-shim-v1" if kind != "unknown" else "user-owned"
        url = INSTALL_SH_URL if kind == "canonical" else "https://example.invalid/old-install.sh"
        before = f"#!/usr/bin/env bash\n# {marker}\n# {url}\n".encode("ascii")
        command.write_bytes(before)
    text = INSTALLER_SH.read_text(encoding="utf-8")
    block = text[text.index('if [ ! -e "$COMMAND" ]; then'):text.index('chmod +x "$COMMAND"')]
    result = _run_bash_fragment(
        text.split("# --- Config", 1)[0]
        + '\nCOMMAND="$INSTALLER_TEST_COMMAND"\n' + block,
        tmp_path, _test_env(INSTALLER_TEST_COMMAND=command.as_posix()),
    )
    assert (result.returncode == 0) == (kind != "unknown"), result.stderr
    if before is not None:
        assert command.read_bytes() == before
    else:
        assert INSTALL_SH_URL in command.read_text(encoding="ascii")
    output = result.stdout + result.stderr
    if kind == "old-source":
        assert "keeps its original upgrade source" in output
        assert f"curl -fsSL {INSTALL_SH_URL} | bash" in output
    elif kind != "unknown":
        assert "keeps its original upgrade source" not in output
    else:
        assert "unrecognized command" in output
