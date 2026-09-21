<#
.SYNOPSIS
  Install the agentbridge local command (Windows / PowerShell).

.DESCRIPTION
  Downloads agentbridge and its connector, creates an isolated virtual environment,
  installs dependencies, and adds `agentbridge` (plus the `deepbox` alias) to the
  user PATH. Fresh installs use %USERPROFILE%\.agentbridge; existing .deepbox
  roots are reused. Daily `agentbridge connect` calls never replace installed files.

  Run once:

      irm https://raw.githubusercontent.com/yusx-swapp/AgentBridge/main/scripts/install.ps1 | iex

  Then set AGENTBRIDGE_SERVER_URL and AGENTBRIDGE_TOKEN and run:

      agentbridge connect

  Upgrade explicitly with `agentbridge upgrade`. The installer never stores your
  token on disk; the connector reads it from the process environment.

.NOTES
  Requires Python 3.10+ (https://www.python.org/downloads/ or `winget install
  Python.Python.3.12`). The connector runs your local Claude Code / Copilot CLI
  / Codex agents; those tools are NOT installed by this script.
#>

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "[agentbridge] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[agentbridge] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "[agentbridge] $msg" -ForegroundColor Yellow }

# One compatibility boundary: presence, not truthiness, selects the new key.
# An explicitly empty AGENTBRIDGE_* value must not revive a legacy credential.
function Get-ProductEnvironment {
    param(
        [Parameter(Mandatory=$true)] [string] $Stem,
        $Default = $null,
        [System.Collections.IDictionary] $Values = [Environment]::GetEnvironmentVariables('Process')
    )
    foreach ($prefix in @('AGENTBRIDGE_', 'DEEPBOX_')) {
        $name = $prefix + $Stem
        if ($Values.Contains($name)) { return [string]$Values[$name] }
    }
    return $Default
}

function Get-InstallRoot {
    param([Parameter(Mandatory=$true)] [string] $HomeDirectory)

    $configured = Get-ProductEnvironment -Stem 'HOME'
    if ($null -ne $configured) {
        if ([string]::IsNullOrWhiteSpace($configured)) {
            throw 'The selected HOME setting is empty. Set AGENTBRIDGE_HOME to an installation directory or unset it.'
        }
        return [IO.Path]::GetFullPath($configured)
    }
    $canonical = Join-Path $HomeDirectory '.agentbridge'
    $legacy = Join-Path $HomeDirectory '.deepbox'
    if (-not (Test-Path -LiteralPath $canonical) -and (Test-Path -LiteralPath $legacy -PathType Container)) {
        return $legacy
    }
    return $canonical
}

function Get-ArchiveSource {
    param([Parameter(Mandatory=$true)] [string] $ExtractDirectory)

    # Do not guess a repository/branch name, or destroy a working app for an old ZIP.
    $folders = @(Get-ChildItem -LiteralPath $ExtractDirectory -Directory)
    if ($folders.Count -ne 1) { throw 'Unexpected archive layout (expected one source folder).' }
    $source = $folders[0].FullName
    foreach ($package in @('agentbridge', 'connector')) {
        if (-not (Test-Path -LiteralPath (Join-Path $source "$package\__init__.py") -PathType Leaf)) {
            throw "Source archive is missing the $package package; the existing installation was not replaced."
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $source 'agentbridge\__main__.py') -PathType Leaf)) {
        throw 'Source archive is missing the agentbridge entrypoint; the existing installation was not replaced.'
    }
    if (-not ((Test-Path -LiteralPath (Join-Path $source 'requirements-connector.txt') -PathType Leaf) -or
              (Test-Path -LiteralPath (Join-Path $source 'requirements.txt') -PathType Leaf))) {
        throw 'Source archive is missing requirements; the existing installation was not replaced.'
    }
    return $source
}

# Match only connector processes launched by this installation's virtualenv.
# Command lines are inspected but never printed because they may contain tokens.
function Test-ConnectorProcess {
    param(
        [Parameter(Mandatory=$true)] $Process,
        [Parameter(Mandatory=$true)] [string] $VenvPython
    )

    $commandLine = [string]$Process.CommandLine
    # Anchor at the interpreter invocation: '-m agentbridge' inside a -c string,
    # script argument, or another module's arguments must never match.
    $invocationPattern = '(?i)^\s*(?:"[^"]+"|\S+)\s+(?:-[bBdEIOPqRsSuv]+\s+)*-m\s+(?:"(?:agentbridge|connector(?:\.cli)?)"|(?:agentbridge|connector(?:\.cli)?))(?:\s|$)'
    if (-not $commandLine -or
        $commandLine -notmatch $invocationPattern) {
        return $false
    }

    try { $target = [IO.Path]::GetFullPath($VenvPython) }
    catch { return $false }

    $exeMatches = $false
    if ($Process.ExecutablePath) {
        try {
            $actual = [IO.Path]::GetFullPath([string]$Process.ExecutablePath)
            $exeMatches = [string]::Equals(
                $actual, $target, [StringComparison]::OrdinalIgnoreCase)
        } catch {}
    }

    $targetPattern = [regex]::Escape($target)
    $commandUsesTarget = $commandLine -match (
        '(?i)^\s*"?' + $targetPattern + '"?(?:\s|$)')
    return ($exeMatches -or $commandUsesTarget)
}

function Get-RunningConnectors {
    param([Parameter(Mandatory=$true)] [string] $VenvPython)

    try {
        $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    } catch {
        return @()
    }
    return @($processes | Where-Object {
        Test-ConnectorProcess -Process $_ -VenvPython $VenvPython
    })
}

function Get-ProcessTreeIds {
    param(
        [Parameter(Mandatory=$true)] [object[]] $Processes,
        [Parameter(Mandatory=$true)] [int[]] $RootProcessIds
    )

    $ids = @{}
    foreach ($rootId in $RootProcessIds) { $ids[[int]$rootId] = $true }
    do {
        $added = $false
        foreach ($item in $Processes) {
            $parentId = [int]$item.ParentProcessId
            $childId = [int]$item.ProcessId
            if ($ids.ContainsKey($parentId) -and -not $ids.ContainsKey($childId)) {
                $ids[$childId] = $true
                $added = $true
            }
        }
    } while ($added)
    return @($ids.Keys | ForEach-Object { [int]$_ })
}

function Stop-RunningConnectors {
    param([Parameter(Mandatory=$true)] [string] $VenvPython)

    if (-not (Test-Path -LiteralPath $VenvPython)) { return }
    $running = @(Get-RunningConnectors -VenvPython $VenvPython)
    if ($running.Count -eq 0) { return }

    try { $snapshot = @(Get-CimInstance Win32_Process -ErrorAction Stop) }
    catch { $snapshot = $running }
    $rootIds = @($running | ForEach-Object { [int]$_.ProcessId })
    $processIds = @(Get-ProcessTreeIds `
        -Processes $snapshot -RootProcessIds $rootIds)

    Write-Step "Stopping the existing connector and its child processes for upgrade ..."
    foreach ($processId in $processIds) {
        # A venv launcher and its base-Python child can both match. Stopping
        # either may make the other disappear, so decide success only after
        # checking that the complete snapshotted process tree has exited.
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(8)
    do {
        $alive = @($processIds | Where-Object {
            Get-Process -Id $_ -ErrorAction SilentlyContinue
        })
        if ($alive.Count -eq 0) { break }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)

    if ($alive.Count -ne 0) {
        throw "The existing agentbridge connector did not stop. Stop it with Ctrl+C, then re-run the installer."
    }
    # Let its parent launcher unwind and release app as its working directory.
    Start-Sleep -Milliseconds 500
    Write-Ok "Existing connector stopped."
}

function Remove-DirectoryWithRetry {
    param(
        [Parameter(Mandatory=$true)] [string] $Path,
        [int] $Attempts = 12,
        [int] $DelayMilliseconds = 500
    )

    if (-not (Test-Path -LiteralPath $Path)) { return }
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            return
        } catch {
            if ($attempt -eq $Attempts) {
                throw "Could not refresh '$Path' because another process is using it. Stop any connector or shell whose working directory is there, then re-run the installer."
            }
            Start-Sleep -Milliseconds $DelayMilliseconds
        }
    }
}

function Get-VenvSitePackages {
    param([Parameter(Mandatory=$true)] [string] $Python)

    # JSON's ASCII escapes survive Windows PowerShell's OEM pipe encoding, even
    # for a Unicode custom root. Never embed that path in Python source code.
    $pathJson = & $Python -I -c "import json, site; print(json.dumps(site.getsitepackages()[0]))"
    if ($LASTEXITCODE -ne 0 -or -not $pathJson) {
        throw 'Could not locate the connector virtualenv site-packages directory.'
    }
    return ($pathJson | ConvertFrom-Json)
}

function Get-PreferredPath {
    param([Parameter(Mandatory=$true)] [string] $Path, [string] $CurrentPath)
    if ($Path -match "[;`r`n]") { throw 'PATH cannot represent a directory containing semicolons or line breaks.' }
    $target = [Environment]::ExpandEnvironmentVariables($Path).TrimEnd('\')
    $entries = @($CurrentPath -split ';' | Where-Object {
        $_ -and [Environment]::ExpandEnvironmentVariables($_.Trim().Trim('"')).TrimEnd('\') -ine $target
    })
    return (@($Path) + $entries) -join ';'
}

function Add-UserPathEntry {
    param([Parameter(Mandatory=$true)] [string] $Path)

    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    try {
        $newUserPath = Get-PreferredPath -Path $Path -CurrentPath $userPath
    } catch {
        Write-Warn2 'The installation directory cannot be represented safely on PATH. Use its command by absolute path.'
        return
    }
    if ($newUserPath -cne $userPath) {
        try {
            [Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
        } catch {
            Write-Warn2 "Could not persist $Path on the user PATH. Add it manually."
        }
    }

    $env:Path = Get-PreferredPath -Path $Path -CurrentPath $env:Path
}

# --- Config ----------------------------------------------------------------
# Canonical public repository (anonymous download; no repo access needed).
# Forks are development copies, never the default install or upgrade source.
# Override AGENTBRIDGE_SOURCE_ZIP (or legacy DEEPBOX_SOURCE_ZIP) to pin a branch.
$SourceZip = Get-ProductEnvironment -Stem 'SOURCE_ZIP' -Default 'https://github.com/yusx-swapp/AgentBridge/archive/refs/heads/main.zip'
if ([string]::IsNullOrWhiteSpace($SourceZip)) { throw 'The selected SOURCE_ZIP setting is empty.' }
$Home2   = if ($env:USERPROFILE) { $env:USERPROFILE } else { [Environment]::GetFolderPath('UserProfile') }
$Root    = Get-InstallRoot -HomeDirectory $Home2
$Src     = Join-Path $Root 'app'          # extracted connector source
$Venv    = Join-Path $Root 'venv'
$VenvPy  = Join-Path $Venv 'Scripts\python.exe'
$Bin     = Join-Path $Root 'bin'
$Command = Join-Path $Bin 'agentbridge.cmd'
$LegacyCommand = Join-Path $Bin 'deepbox.cmd'
$Launcher = Join-Path $Root 'deepbox-connect.cmd'  # legacy compatibility
$InstallScriptUrl = 'https://raw.githubusercontent.com/yusx-swapp/AgentBridge/main/scripts/install.ps1'

Write-Step "Installing into $Root"
New-Item -ItemType Directory -Force -Path $Root | Out-Null

# --- 1. Locate Python 3.10+ -------------------------------------------------
# Returns @(exe, @(prefixArgs...)) for the first interpreter that is >= 3.10,
# or $null. Handles the Windows `py` launcher (needs a `-3` prefix arg) as
# well as plain `python` / `python3`.
function Find-Python {
    $verCheck = 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)'
    $candidates = @(
        @('py',      @('-3')),
        @('python',  @()),
        @('python3', @())
    )
    foreach ($c in $candidates) {
        $exe  = $c[0]
        $pre  = $c[1]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            & $exe @pre '-c' $verCheck 2>$null
            if ($LASTEXITCODE -eq 0) { return ,@($exe, $pre) }
        } catch {}
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Warn2 "Python 3.10+ was not found on PATH."
    Write-Host  "  Install it, then re-run this installer:"
    Write-Host  "    winget install Python.Python.3.12"
    Write-Host  "    (or download from https://www.python.org/downloads/)"
    throw "Python 3.10+ required."
}
$PyExe  = $py[0]
$PyArgs = $py[1]
Write-Ok "Using Python: $PyExe $($PyArgs -join ' ')"

# --- 2. Download + extract connector source --------------------------------
Write-Step "Downloading connector source ..."
$tmpZip = Join-Path $env:TEMP ("agentbridge-" + [guid]::NewGuid().ToString('N') + '.zip')
Invoke-WebRequest -Uri $SourceZip -OutFile $tmpZip -UseBasicParsing

$tmpExtract = Join-Path $env:TEMP ("agentbridge-x-" + [guid]::NewGuid().ToString('N'))
if (Test-Path $tmpExtract) { Remove-Item -Recurse -Force $tmpExtract }
Expand-Archive -Path $tmpZip -DestinationPath $tmpExtract -Force
Remove-Item -Force $tmpZip

# The GitHub zip nests everything under a single <repo>-<branch> folder.
$archiveSource = Get-ArchiveSource -ExtractDirectory $tmpExtract

# Refresh the app folder with only what the connector needs. Legacy launchers
# used app as their cwd, so stop only processes launched by this installation's
# virtualenv and retry while those wrappers unwind.
Stop-RunningConnectors -VenvPython $VenvPy
Remove-DirectoryWithRetry -Path $Src
New-Item -ItemType Directory -Force -Path $Src | Out-Null
foreach ($package in @('agentbridge', 'connector')) {
    Copy-Item -Recurse -Force -LiteralPath (Join-Path $archiveSource $package) -Destination (Join-Path $Src $package)
}
foreach ($f in @('requirements-connector.txt', 'requirements.txt')) {
    $p = Join-Path $archiveSource $f
    if (Test-Path -LiteralPath $p) { Copy-Item -Force -LiteralPath $p -Destination (Join-Path $Src $f) }
}
Remove-Item -Recurse -Force $tmpExtract
Write-Ok "Connector source ready."

# --- 3. Virtual environment + dependencies ---------------------------------
if (-not (Test-Path $VenvPy)) {
    Write-Step "Creating virtual environment ..."
    & $PyExe @PyArgs -m venv $Venv
}
Write-Step "Installing connector dependencies ..."
& $VenvPy -m pip install --quiet --upgrade pip | Out-Null
$req = Join-Path $Src 'requirements-connector.txt'
if (Test-Path $req) {
    & $VenvPy -m pip install --quiet -r $req
} else {
    & $VenvPy -m pip install --quiet 'httpx>=0.27' 'websockets>=12.0' 'PyYAML>=6.0' 'pywinpty>=2.0'
}
$sitePackages = Get-VenvSitePackages -Python $VenvPy
if (-not $sitePackages) { throw "Could not locate the connector virtualenv site-packages directory." }
$pthLine = "import sys; from pathlib import Path; sys.path.insert(0, str(Path(sys.prefix).parent / 'app'))`n"
# Keep this filename: upgrades reuse the same registration, not a second .pth.
[System.IO.File]::WriteAllText((Join-Path $sitePackages 'deepbox-app.pth'), $pthLine, [System.Text.Encoding]::ASCII)
Write-Ok "Dependencies installed."

# --- 4. Install stable command + legacy launcher ----------------------------
# The PATH shim is intentionally stable: `agentbridge upgrade` can refresh app and
# venv without rewriting the batch file that is currently invoking the upgrade.
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
$commandBody = @"
@echo off
rem agentbridge-stable-shim-v1
setlocal DisableDelayedExpansion
rem Pin commands and upgrades to this installation, regardless of the caller's HOME settings.
for %%I in ("%~dp0..") do set "INSTALL_ROOT=%%~fI"
set "AGENTBRIDGE_HOME=%INSTALL_ROOT%"
set "DEEPBOX_HOME=%INSTALL_ROOT%"
if /I "%~1"=="upgrade" goto upgrade
set "VENV_PYTHON=%INSTALL_ROOT%\venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (echo [agentbridge] installation is incomplete; run agentbridge upgrade & exit /b 1)
"%VENV_PYTHON%" -I -u -m agentbridge %*
exit /b %ERRORLEVEL%
:upgrade
set "AGENTBRIDGE_INSTALL_ONLY=1"
set "DEEPBOX_INSTALL_ONLY=1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "irm '$InstallScriptUrl' | iex"
exit /b %ERRORLEVEL%
"@
if (-not (Test-Path -LiteralPath $Command)) {
    Set-Content -LiteralPath $Command -Value $commandBody -Encoding ASCII
} elseif (-not ([IO.File]::ReadAllText($Command).Contains('agentbridge-stable-shim-v1'))) {
    throw "Refusing to replace an unrecognized command at '$Command'. Move it, then re-run the installer."
} elseif (-not ([IO.File]::ReadAllText($Command).Contains($InstallScriptUrl))) {
    Write-Warn2 'The existing stable command keeps its original upgrade source; it was not rewritten.'
    Write-Warn2 "For future upgrades, run the canonical installer directly: irm $InstallScriptUrl | iex"
}

$aliasBody = @"
@echo off
rem agentbridge-legacy-alias-v1
setlocal DisableDelayedExpansion
rem Tail dispatch avoids CALL reparsing percent signs in paths or arguments.
"%~dp0agentbridge.cmd" %*
"@
if (-not (Test-Path -LiteralPath $LegacyCommand)) {
    Set-Content -LiteralPath $LegacyCommand -Value $aliasBody -Encoding ASCII
} else {
    $existing = [IO.File]::ReadAllText($LegacyCommand)
    if (-not ($existing.Contains('agentbridge-legacy-alias-v1') -or $existing.Contains('deepbox-stable-shim-v1'))) {
        throw "Refusing to replace an unrecognized command at '$LegacyCommand'. Move it, then re-run the installer."
    }
    # An old v1 shim may be executing this upgrade. Leave it intact; connector.cli
    # remains supported and uses this same app/venv. Fresh installs use the alias.
}
Add-UserPathEntry -Path $Bin
Write-Ok "Command installed: $Command"

$launcherBody = @"
@echo off
rem Legacy compatibility; prefer: agentbridge connect
setlocal DisableDelayedExpansion
"%~dp0bin\agentbridge.cmd" connect %*
"@
Set-Content -LiteralPath $Launcher -Value $launcherBody -Encoding ASCII

# --- 5. Finish, or honor commands generated by the previous web UI ---------
$server = Get-ProductEnvironment -Stem 'SERVER_URL'
$token  = Get-ProductEnvironment -Stem 'TOKEN'
$installOnly = (Get-ProductEnvironment -Stem 'INSTALL_ONLY') -eq '1'

if (-not $installOnly -and $server -and $token) {
    Write-Ok "Setup complete. Connecting ..."
    Write-Host ""
    Write-Host "  Reconnect without reinstalling:" -ForegroundColor DarkGray
    Write-Host "      agentbridge connect" -ForegroundColor DarkGray
    Write-Host ""
    & $Command doctor
    & $Command connect
} else {
    if (($server -and -not $token) -or ($token -and -not $server)) {
        Write-Warn2 "Both AGENTBRIDGE_SERVER_URL and AGENTBRIDGE_TOKEN are required to connect (legacy DEEPBOX_* names also work)."
    }
    Write-Ok "Setup complete."
    Write-Host "  Open a new terminal if needed, then run:"
    Write-Host "      agentbridge connect"
    Write-Host "  Upgrade later with:"
    Write-Host "      agentbridge upgrade"
}
