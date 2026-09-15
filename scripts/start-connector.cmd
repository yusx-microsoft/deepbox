@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo [agentbridge] Missing .venv. Run: py -3 -m venv .venv
  exit /b 1
)
rem Resolve AGENTBRIDGE_* and legacy DEEPBOX_* once in agentbridge.product.env.
rem Never expand credentials in CMD; an explicit empty canonical value wins.
".venv\Scripts\python.exe" -u -m agentbridge connect %*
exit /b %ERRORLEVEL%
