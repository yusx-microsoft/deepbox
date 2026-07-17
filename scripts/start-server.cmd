@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo [deepbox] Missing .venv. Run: py -3 -m venv .venv
  exit /b 1
)
if not exist ".env" (
  echo [deepbox] Missing .env. Copy .env.example to .env and configure it first.
  exit /b 1
)
.venv\Scripts\python -m server
