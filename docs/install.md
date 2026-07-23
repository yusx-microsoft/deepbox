# One-Line Installer

deepbox ships two one-line installers so a user can connect a machine **without
cloning the repo or installing dependencies by hand**:

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.ps1 | iex
```

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.sh | bash
```

## What the installer does

1. Finds a Python 3.10+ interpreter (and prints install guidance if missing).
2. Downloads the connector source as a zip from the **public** mirror
   (`yusx-swapp/deepbox`) — anonymous, no GitHub access needed.
3. On a Windows reinstall, finds a running `-m connector` process from this
   installation's virtualenv and stops its connector-owned process tree before
   replacing the source directory.
4. Creates an isolated virtualenv under `~/.deepbox` and installs the
   connector dependencies (`httpx`, `websockets`, and `pywinpty` on Windows).
5. Writes a reusable launcher (`~/.deepbox/deepbox-connect.cmd` / `.sh`).
6. Connects immediately using the server URL + devbox token.

Everything lives under `~/.deepbox`; uninstall by deleting that folder. The
**token is never written to disk** — it is passed to the connector process via
an environment variable only.

### Reinstalling while a Windows connector is running

Re-running `install.ps1` detects only connector processes launched as
`~\.deepbox\venv\Scripts\python.exe -m connector`, snapshots and stops their
connector-owned child process tree, waits for the launcher to release
`~\.deepbox\app`, and retries the directory replacement. This intentionally
ends that connector's active local sessions before the new copy connects. It
does not stop unrelated Python processes and never logs their command lines. If
a separate shell itself has `~\.deepbox\app` as its working directory, leave
that directory or close the shell and run the installer again.

### Non-interactive use

Pre-set the two values so the piped installer doesn't prompt:

```powershell
$env:DEEPBOX_SERVER_URL = 'https://deepbox-sixingyu-pa.azurewebsites.net'
$env:DEEPBOX_TOKEN      = 'hpc_box_xxxxxxxx'
irm https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.ps1 | iex
```

```bash
export DEEPBOX_SERVER_URL='https://deepbox-sixingyu-pa.azurewebsites.net'
export DEEPBOX_TOKEN='hpc_box_xxxxxxxx'
curl -fsSL https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.sh | bash
```

Set `DEEPBOX_SOURCE_ZIP` to install from a fork or a specific branch.

### Reconnecting later

The generated launcher re-runs the connector without re-downloading anything:

```powershell
# Windows — prompts for URL/token if not already in the environment
%USERPROFILE%\.deepbox\deepbox-connect.cmd
```

```bash
# macOS / Linux
DEEPBOX_SERVER_URL=... DEEPBOX_TOKEN=... ~/.deepbox/deepbox-connect.sh
```

### Managing local projects

Project paths live only in the connector-local `~/.deepbox/state.db`. The
server and browser receive a stable project ID and display name, never the path.
Register a project before selecting it while creating an agent in the browser:

```powershell
& "$HOME\.deepbox\deepbox-connect.cmd" project add "C:\src\my-repo" --name "my-repo"
& "$HOME\.deepbox\deepbox-connect.cmd" project list
& "$HOME\.deepbox\deepbox-connect.cmd" project remove <project-id>
& "$HOME\.deepbox\deepbox-connect.cmd" project sync
```

```bash
~/.deepbox/deepbox-connect.sh project add "$HOME/src/my-repo" --name "my-repo"
~/.deepbox/deepbox-connect.sh project list
~/.deepbox/deepbox-connect.sh project remove <project-id>
~/.deepbox/deepbox-connect.sh project sync
```

`add` requires an existing directory and stores its canonical absolute path;
adding the same path again reuses its ID. `remove` deletes only the local
registration, not the directory. After synchronization, server-side agents that
referenced the removed project lose that binding. `sync` sends the path-free
project list without starting the long-running connector.

---

# Hosting the scripts for one-line use

The browser and examples use the anonymous GitHub Raw endpoints on the `main`
branch:

```text
https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.ps1
https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.sh
```

The scripts are published by pushing the same reviewed commit to the public
GitHub mirror; there is no separate Blob upload step. Keep `scripts/install.ps1`
and `scripts/install.sh` in that commit so the UI command and downloaded source
stay in sync.

Verify both endpoints anonymously after publishing:

```powershell
Invoke-WebRequest -UseBasicParsing https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.ps1 | Select-Object StatusCode
```

```bash
curl -I https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.sh
```

Both should return HTTP `200`. GitHub Raw may cache briefly after a push; pin a
commit SHA in the URL when an immutable installer is required.
