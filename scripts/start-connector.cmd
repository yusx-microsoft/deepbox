@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo [deepbox] Missing .venv. Run: py -3 -m venv .venv
  exit /b 1
)
if not defined DEEPBOX_SERVER_URL set /p DEEPBOX_SERVER_URL=Server HTTPS URL:
if not defined DEEPBOX_TOKEN set /p DEEPBOX_TOKEN=Devbox token:
if "%DEEPBOX_SERVER_URL%"=="" exit /b 1
if "%DEEPBOX_TOKEN%"=="" exit /b 1
.venv\Scripts\python -u -m connector
