@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo [agentbridge] Missing .venv. Run: py -3 -m venv .venv
  exit /b 1
)
if not exist ".env" (
  echo [agentbridge] Missing .env. Copy .env.example to .env and configure it first.
  exit /b 1
)
".venv\Scripts\python.exe" -m server
exit /b %ERRORLEVEL%
