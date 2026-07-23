<#
.SYNOPSIS
  deepbox one-line connector installer (Windows / PowerShell).

.DESCRIPTION
  Downloads the deepbox connector, creates an isolated virtual environment,
  installs the connector dependencies, writes a launcher, and connects the
  machine to your deepbox server. No git clone and no manual dependency
  wrangling required.

  Run it straight from the web:

      # Interactive (prompts for server URL + token):
      irm https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.ps1 | iex

      # Non-interactive (pre-set the two values, then pipe):
      $env:DEEPBOX_SERVER_URL = 'https://deepbox-sixingyu-pa.azurewebsites.net'
      $env:DEEPBOX_TOKEN      = 'hpc_box_xxxxxxxx'
      irm https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.ps1 | iex

  Everything is written under %USERPROFILE%\.deepbox and can be removed by
  deleting that folder. The installer never stores your token on disk: it is
  passed to the connector process via an environment variable only.

.NOTES
  Requires Python 3.10+ (https://www.python.org/downloads/ or `winget install
  Python.Python.3.12`). The connector runs your local Claude Code / Copilot CLI
  / Codex agents; those tools are NOT installed by this script.
#>

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "[deepbox] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[deepbox] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "[deepbox] $msg" -ForegroundColor Yellow }

# Match only connector processes launched by this installation's virtualenv.
# Command lines are inspected but never printed because they may contain tokens.
function Test-DeepboxConnectorProcess {
    param(
        [Parameter(Mandatory=$true)] $Process,
        [Parameter(Mandatory=$true)] [string] $VenvPython
    )

    $commandLine = [string]$Process.CommandLine
    if (-not $commandLine -or
        $commandLine -notmatch '(?i)(?:^|\s)-m\s+connector(?:\s|$)') {
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

function Get-RunningDeepboxConnectors {
    param([Parameter(Mandatory=$true)] [string] $VenvPython)

    try {
        $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    } catch {
        return @()
    }
    return @($processes | Where-Object {
        Test-DeepboxConnectorProcess -Process $_ -VenvPython $VenvPython
    })
}

function Get-DeepboxProcessTreeIds {
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

function Stop-RunningDeepboxConnectors {
    param([Parameter(Mandatory=$true)] [string] $VenvPython)

    if (-not (Test-Path -LiteralPath $VenvPython)) { return }
    $running = @(Get-RunningDeepboxConnectors -VenvPython $VenvPython)
    if ($running.Count -eq 0) { return }

    try { $snapshot = @(Get-CimInstance Win32_Process -ErrorAction Stop) }
    catch { $snapshot = $running }
    $rootIds = @($running | ForEach-Object { [int]$_.ProcessId })
    $processIds = @(Get-DeepboxProcessTreeIds `
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
        throw "The existing deepbox connector did not stop. Stop it with Ctrl+C, then re-run the installer."
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

# --- Config ----------------------------------------------------------------
# Public source of the connector code (anonymous download; no repo access
# needed). Override with $env:DEEPBOX_SOURCE_ZIP to pin a fork/branch.
$SourceZip = if ($env:DEEPBOX_SOURCE_ZIP) { $env:DEEPBOX_SOURCE_ZIP }
            else { 'https://github.com/yusx-swapp/deepbox/archive/refs/heads/main.zip' }
$Home2   = if ($env:USERPROFILE) { $env:USERPROFILE } else { [Environment]::GetFolderPath('UserProfile') }
$Root    = Join-Path $Home2 '.deepbox'
$Src     = Join-Path $Root 'app'          # extracted connector source
$Venv    = Join-Path $Root 'venv'
$VenvPy  = Join-Path $Venv 'Scripts\python.exe'
$Launcher = Join-Path $Root 'deepbox-connect.cmd'

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
$tmpZip = Join-Path $env:TEMP ("deepbox-" + [guid]::NewGuid().ToString('N') + '.zip')
Invoke-WebRequest -Uri $SourceZip -OutFile $tmpZip -UseBasicParsing

$tmpExtract = Join-Path $env:TEMP ("deepbox-x-" + [guid]::NewGuid().ToString('N'))
if (Test-Path $tmpExtract) { Remove-Item -Recurse -Force $tmpExtract }
Expand-Archive -Path $tmpZip -DestinationPath $tmpExtract -Force
Remove-Item -Force $tmpZip

# The GitHub zip nests everything under a single <repo>-<branch> folder.
$inner = Get-ChildItem -Path $tmpExtract -Directory | Select-Object -First 1
if (-not $inner) { throw "Unexpected archive layout (no inner folder)." }

# Refresh the app folder with only what the connector needs. A previous
# connector keeps this directory as its cwd on Windows, so stop only processes
# launched by this installation's virtualenv and retry while wrappers unwind.
Stop-RunningDeepboxConnectors -VenvPython $VenvPy
Remove-DirectoryWithRetry -Path $Src
New-Item -ItemType Directory -Force -Path $Src | Out-Null
Copy-Item -Recurse -Force (Join-Path $inner.FullName 'connector') (Join-Path $Src 'connector')
foreach ($f in @('requirements-connector.txt', 'requirements.txt')) {
    $p = Join-Path $inner.FullName $f
    if (Test-Path $p) { Copy-Item -Force $p (Join-Path $Src $f) }
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
    & $VenvPy -m pip install --quiet 'httpx>=0.27' 'websockets>=12.0' 'pywinpty>=2.0'
}
Write-Ok "Dependencies installed."

# --- 4. Write launcher ------------------------------------------------------
# The launcher re-runs the connector any time you want to reconnect, without
# re-downloading anything. Token is read from the environment at run time.
$launcherBody = @"
@echo off
setlocal
cd /d "%~dp0app"
if not defined DEEPBOX_SERVER_URL set /p DEEPBOX_SERVER_URL=deepbox server HTTPS URL:
if not defined DEEPBOX_TOKEN set /p DEEPBOX_TOKEN=deepbox devbox token:
if "%DEEPBOX_SERVER_URL%"=="" (echo [deepbox] server URL required & exit /b 1)
if "%DEEPBOX_TOKEN%"=="" (echo [deepbox] token required & exit /b 1)
"$VenvPy" -u -m connector %*
"@
Set-Content -Path $Launcher -Value $launcherBody -Encoding ASCII
Write-Ok "Launcher written: $Launcher"

# --- 5. Connect now ---------------------------------------------------------
$server = $env:DEEPBOX_SERVER_URL
$token  = $env:DEEPBOX_TOKEN
if (-not $server) { $server = Read-Host 'deepbox server HTTPS URL' }
if (-not $token)  { $token  = Read-Host 'deepbox devbox token' }

if ($server -and $token) {
    Write-Ok "Setup complete. Connecting ..."
    Write-Host ""
    Write-Host "  Reconnect any time with:" -ForegroundColor DarkGray
    Write-Host "      $Launcher" -ForegroundColor DarkGray
    Write-Host ""
    $env:DEEPBOX_SERVER_URL = $server
    $env:DEEPBOX_TOKEN      = $token
    Push-Location $Src
    try {
        & $VenvPy -m connector --doctor
        & $VenvPy -u -m connector
    } finally {
        Pop-Location
    }
} else {
    Write-Ok "Setup complete. Connect with:"
    Write-Host "      $Launcher"
}
