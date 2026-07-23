#!/usr/bin/env bash
# deepbox one-line connector installer (macOS / Linux).
#
# Downloads the deepbox connector, creates an isolated virtualenv, installs the
# connector dependencies, writes a launcher, and connects this machine to your
# deepbox server. No git clone and no manual dependency wrangling.
#
# Interactive (prompts for server URL + token):
#     curl -fsSL https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.sh | bash
#
# Non-interactive (pre-set the two values):
#     export DEEPBOX_SERVER_URL='https://deepbox-sixingyu-pa.azurewebsites.net'
#     export DEEPBOX_TOKEN='hpc_box_xxxxxxxx'
#     curl -fsSL https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.sh | bash
#
# Everything lands under ~/.deepbox and can be removed by deleting that folder.
# The token is never written to disk; it is passed to the connector process via
# an environment variable only.
#
# Requires Python 3.10+. The connector runs your local Claude Code / Copilot CLI
# / Codex agents; those tools are NOT installed by this script.
set -euo pipefail

say()  { printf '\033[36m[deepbox]\033[0m %s\n' "$1"; }
ok()   { printf '\033[32m[deepbox]\033[0m %s\n' "$1"; }
warn() { printf '\033[33m[deepbox]\033[0m %s\n' "$1"; }

SOURCE_ZIP="${DEEPBOX_SOURCE_ZIP:-https://github.com/yusx-swapp/deepbox/archive/refs/heads/main.zip}"
ROOT="${HOME}/.deepbox"
SRC="${ROOT}/app"
VENV="${ROOT}/venv"
LAUNCHER="${ROOT}/deepbox-connect.sh"

say "Installing into ${ROOT}"
mkdir -p "${ROOT}"

# --- 1. Locate Python 3.10+ -------------------------------------------------
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)' 2>/dev/null; then
      PY="$cand"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  warn "Python 3.10+ was not found on PATH."
  echo  "  Install it, then re-run this installer, e.g.:"
  echo  "    macOS:  brew install python@3.12"
  echo  "    Debian: sudo apt-get install -y python3 python3-venv python3-pip"
  exit 1
fi
ok "Using Python: $($PY --version 2>&1)"

# --- 2. Download + extract connector source --------------------------------
say "Downloading connector source ..."
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ZIP="${TMP}/src.zip"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$SOURCE_ZIP" -o "$ZIP"
else
  wget -qO "$ZIP" "$SOURCE_ZIP"
fi

EXTRACT="${TMP}/x"
mkdir -p "$EXTRACT"
"$PY" -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$ZIP" "$EXTRACT"

# GitHub zips nest everything under a single <repo>-<branch> directory.
INNER="$(find "$EXTRACT" -mindepth 1 -maxdepth 1 -type d | head -n1)"
[ -n "$INNER" ] || { warn "Unexpected archive layout."; exit 1; }

rm -rf "$SRC"
mkdir -p "$SRC"
cp -R "${INNER}/connector" "${SRC}/connector"
for f in requirements-connector.txt requirements.txt; do
  [ -f "${INNER}/${f}" ] && cp "${INNER}/${f}" "${SRC}/${f}"
done
ok "Connector source ready."

# --- 3. Virtualenv + dependencies ------------------------------------------
VENV_PY="${VENV}/bin/python"
if [ ! -x "$VENV_PY" ]; then
  say "Creating virtual environment ..."
  "$PY" -m venv "$VENV"
fi
say "Installing connector dependencies ..."
"$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null
if [ -f "${SRC}/requirements-connector.txt" ]; then
  "$VENV_PY" -m pip install --quiet -r "${SRC}/requirements-connector.txt"
else
  "$VENV_PY" -m pip install --quiet 'httpx>=0.27' 'websockets>=12.0'
fi
ok "Dependencies installed."

# --- 4. Write launcher ------------------------------------------------------
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "\$(dirname "\$0")/app"
: "\${DEEPBOX_SERVER_URL:?set DEEPBOX_SERVER_URL to your deepbox server HTTPS URL}"
: "\${DEEPBOX_TOKEN:?set DEEPBOX_TOKEN to your devbox token}"
exec "${VENV_PY}" -u -m connector "\$@"
EOF
chmod +x "$LAUNCHER"
ok "Launcher written: ${LAUNCHER}"

# --- 5. Connect now ---------------------------------------------------------
SERVER="${DEEPBOX_SERVER_URL:-}"
TOKEN="${DEEPBOX_TOKEN:-}"
if [ -z "$SERVER" ] && [ -t 0 ]; then read -r -p "deepbox server HTTPS URL: " SERVER; fi
if [ -z "$TOKEN" ]  && [ -t 0 ]; then read -r -p "deepbox devbox token: " TOKEN; fi

if [ -n "$SERVER" ] && [ -n "$TOKEN" ]; then
  ok "Setup complete. Connecting ..."
  echo
  echo "  Reconnect any time with:"
  echo "      DEEPBOX_SERVER_URL=... DEEPBOX_TOKEN=... ${LAUNCHER}"
  echo
  export DEEPBOX_SERVER_URL="$SERVER"
  export DEEPBOX_TOKEN="$TOKEN"
  cd "$SRC"
  "$VENV_PY" -m connector --doctor || true
  exec "$VENV_PY" -u -m connector
else
  ok "Setup complete. Connect with:"
  echo "      DEEPBOX_SERVER_URL=... DEEPBOX_TOKEN=... ${LAUNCHER}"
fi
