#!/usr/bin/env bash
# Install the agentbridge local command once (macOS / Linux).
#
# The installer downloads the connector, creates an isolated virtualenv, and
# adds agentbridge (plus the deepbox alias) to the user's PATH. Fresh installs use
# ~/.agentbridge; existing ~/.deepbox roots are reused. Daily `agentbridge connect`
# calls never download or replace installed files.
#
# Run once:
#     curl -fsSL https://raw.githubusercontent.com/yusx-swapp/AgentBridge/main/scripts/install.sh | bash
#
# Then set AGENTBRIDGE_SERVER_URL and AGENTBRIDGE_TOKEN and run:
#     agentbridge connect
#
# Upgrade explicitly with `agentbridge upgrade`. The token is never written to disk;
# it is passed to the connector process through the environment only.
#
# Requires Python 3.10+. Claude Code / Copilot CLI / Codex are not installed by
# this script.
set -euo pipefail

say()  { printf '\033[36m[agentbridge]\033[0m %s\n' "$1"; }
ok()   { printf '\033[32m[agentbridge]\033[0m %s\n' "$1"; }
warn() { printf '\033[33m[agentbridge]\033[0m %s\n' "$1"; }

# One compatibility boundary. Do not use :- here: explicit empty values win.
product_env() {
  local canonical="AGENTBRIDGE_$1" legacy="DEEPBOX_$1"
  if [ "${!canonical+x}" = x ]; then printf '%s' "${!canonical}"
  elif [ "${!legacy+x}" = x ]; then printf '%s' "${!legacy}"
  else printf '%s' "${2-}"
  fi
}

select_install_root() {
  local default_root="${1}/.agentbridge"
  if [ ! -e "$default_root" ] && [ -d "${1}/.deepbox" ]; then
    default_root="${1}/.deepbox"
  fi
  product_env HOME "$default_root"
}

# --- Config ----------------------------------------------------------------
# Canonical public repository; forks are never the default install/upgrade source.
SOURCE_ZIP="$(product_env SOURCE_ZIP 'https://github.com/yusx-swapp/AgentBridge/archive/refs/heads/main.zip')"
ROOT="$(select_install_root "$HOME")"
[ -n "$SOURCE_ZIP" ] || { warn 'The selected SOURCE_ZIP setting is empty.'; exit 1; }
[ -n "$ROOT" ] || { warn 'The selected HOME setting is empty; set AGENTBRIDGE_HOME or unset it.'; exit 1; }
say "Installing into ${ROOT}"
mkdir -p "$ROOT"
ROOT="$(CDPATH= cd -- "$ROOT" && pwd -P)"
SRC="${ROOT}/app"
VENV="${ROOT}/venv"
BIN="${ROOT}/bin"
COMMAND="${BIN}/agentbridge"
LEGACY_COMMAND="${BIN}/deepbox"
LAUNCHER="${ROOT}/deepbox-connect.sh"  # legacy compatibility

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
ok "Using Python: $("$PY" --version 2>&1)"

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

# Validate both packages before touching a working installation. The enclosing
# repository/branch name is deliberately not coupled to the product name.
INNER="$("$PY" - "$EXTRACT" <<'PY'
from pathlib import Path
import sys

folders = [p for p in Path(sys.argv[1]).iterdir() if p.is_dir()]
if len(folders) != 1:
    sys.exit('Unexpected archive layout (expected one source folder).')
source = folders[0]
for required in ('agentbridge/__init__.py', 'agentbridge/__main__.py', 'connector/__init__.py'):
    if not (source / required).is_file():
        sys.exit('Source archive is missing a required package or entrypoint; the existing installation was not replaced.')
if not any((source / name).is_file() for name in ('requirements-connector.txt', 'requirements.txt')):
    sys.exit('Source archive is missing requirements; the existing installation was not replaced.')
print(source)
PY
)"

rm -rf "$SRC"
mkdir -p "$SRC"
for package in agentbridge connector; do
  cp -R "${INNER}/${package}" "${SRC}/${package}"
done
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
  "$VENV_PY" -m pip install --quiet 'httpx>=0.27' 'websockets>=12.0' 'PyYAML>=6.0'
fi
SITE_PACKAGES="$("$VENV_PY" -c 'import site; print(site.getsitepackages()[0])')"
if [ -z "$SITE_PACKAGES" ]; then
  warn "Could not locate the connector virtualenv site-packages directory."
  exit 1
fi
# Reuse the old registration filename instead of adding a second .pth on upgrade.
printf '%s\n' "import sys; from pathlib import Path; sys.path.insert(0, str(Path(sys.prefix).parent / 'app'))" > "${SITE_PACKAGES}/deepbox-app.pth"
ok "Dependencies installed."

# --- 4. Install stable command + legacy launcher ----------------------------
# This shim is not rewritten during upgrades, so it can safely invoke the
# installer while the command file itself is running.
mkdir -p "$BIN"
if [ ! -e "$COMMAND" ]; then
  cat > "$COMMAND" <<'EOF'
#!/usr/bin/env bash
# agentbridge-stable-shim-v1
set -euo pipefail
# Pin commands and upgrades to this installation, not the caller's HOME settings.
COMMAND_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(CDPATH= cd -- "${COMMAND_DIR}/.." && pwd -P)"
export AGENTBRIDGE_HOME="$ROOT"
export DEEPBOX_HOME="$ROOT"
if [ "${1:-}" = "upgrade" ]; then
  export AGENTBRIDGE_INSTALL_ONLY=1
  export DEEPBOX_INSTALL_ONLY=1
  URL="https://raw.githubusercontent.com/yusx-swapp/AgentBridge/main/scripts/install.sh"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" | bash
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- "$URL" | bash
  else
    echo "[agentbridge] curl or wget is required for upgrade." >&2
    exit 1
  fi
  exit
fi
PY="${ROOT}/venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[agentbridge] installation is incomplete; run agentbridge upgrade" >&2
  exit 1
fi
exec "$PY" -I -u -m agentbridge "$@"
EOF
elif ! grep -Fq 'agentbridge-stable-shim-v1' "$COMMAND"; then
  warn "Refusing to replace an unrecognized command at ${COMMAND}."
  exit 1
elif ! grep -Fq 'https://raw.githubusercontent.com/yusx-swapp/AgentBridge/main/scripts/install.sh' "$COMMAND"; then
  warn "The existing stable command keeps its original upgrade source; it was not rewritten."
  warn "For future upgrades, run the canonical installer directly: curl -fsSL https://raw.githubusercontent.com/yusx-swapp/AgentBridge/main/scripts/install.sh | bash"
fi
chmod +x "$COMMAND"
ok "Command installed: ${COMMAND}"

if [ -e "$LEGACY_COMMAND" ] && ! grep -Eq 'agentbridge-legacy-alias-v1|deepbox-stable-shim-v1' "$LEGACY_COMMAND"; then
  warn "Refusing to replace an unrecognized command at ${LEGACY_COMMAND}."
  exit 1
fi
# Replace via a same-directory rename, never truncate a running legacy shim.
ALIAS_TMP="$(mktemp "${BIN}/.legacy-command.XXXXXX")"
cat > "$ALIAS_TMP" <<'EOF'
#!/usr/bin/env bash
# agentbridge-legacy-alias-v1
set -euo pipefail
COMMAND_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec "${COMMAND_DIR}/agentbridge" "$@"
EOF
chmod +x "$ALIAS_TMP"
mv -f "$ALIAS_TMP" "$LEGACY_COMMAND"

cat > "$LAUNCHER" <<'EOF'
#!/usr/bin/env bash
# Legacy compatibility; prefer: agentbridge connect
set -euo pipefail
ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec "${ROOT}/bin/agentbridge" connect "$@"
EOF
chmod +x "$LAUNCHER"

# Add the command to the profile used by the current login shell. A marker keeps
# repeated explicit upgrades from changing the profile again.
case "$(basename "${SHELL:-sh}")" in
  zsh) PROFILE="${HOME}/.zprofile" ;;
  bash)
    if [ -f "${HOME}/.bash_profile" ]; then PROFILE="${HOME}/.bash_profile"
    else PROFILE="${HOME}/.profile"
    fi
    ;;
  *) PROFILE="${HOME}/.profile" ;;
esac
# A root-specific marker is idempotent without putting a potentially multiline
# path in a shell comment. The actual assignment below is always shell-quoted.
BIN_LITERAL="$("$PY" -c 'import shlex, sys; print(shlex.quote(sys.argv[1]))' "$BIN")"
PATH_ID="$("$PY" -c 'import hashlib, sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "$BIN")"
PATH_MARKER="# agentbridge command path v1: $PATH_ID"
case "$BIN" in
  *:*|*$'\n'*|*$'\r'*)
    warn 'The installation directory cannot be represented safely on PATH. Use its command by absolute path.'
    ;;
  *)
    if ! grep -Fq "$PATH_MARKER" "$PROFILE" 2>/dev/null; then
      {
        printf '\n%s\n' "$PATH_MARKER"
        printf '_APP_BIN=%s\n' "$BIN_LITERAL"
        cat <<'EOF'
case ":${PATH}:" in
  *":${_APP_BIN}:"*) ;;
  *) export PATH="${_APP_BIN}:${PATH}" ;;
esac
unset _APP_BIN
EOF
      } >> "$PROFILE"
    fi
    ;;
esac

# --- 5. Finish, or honor commands generated by the previous web UI ---------
SERVER="$(product_env SERVER_URL)"
TOKEN="$(product_env TOKEN)"
INSTALL_ONLY="$(product_env INSTALL_ONLY 0)"
if [ "$INSTALL_ONLY" != "1" ] && [ -n "$SERVER" ] && [ -n "$TOKEN" ]; then
  ok "Setup complete. Connecting ..."
  echo
  echo "  Reconnect without reinstalling:"
  echo "      agentbridge connect"
  echo
  "$COMMAND" doctor || true
  rm -rf "$TMP"
  trap - EXIT
  exec "$COMMAND" connect
else
  if { [ -n "$SERVER" ] && [ -z "$TOKEN" ]; } || { [ -n "$TOKEN" ] && [ -z "$SERVER" ]; }; then
    warn "Both AGENTBRIDGE_SERVER_URL and AGENTBRIDGE_TOKEN are required to connect (legacy DEEPBOX_* names also work)."
  fi
  ok "Setup complete."
  echo "  Open a new terminal if needed, then run:"
  echo "      agentbridge connect"
  echo "  Upgrade later with:"
  echo "      agentbridge upgrade"
fi
