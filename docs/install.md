# Install once, connect anytime

Install agentbridge once as a local command. Daily connections use `agentbridge connect`;
they do **not** download code, rebuild the virtualenv, or replace
`<install-root>/app`. The old `deepbox` command remains supported.

For the optional local DeepOrca library runtime, see [DeepOrca integration](deeporca.md).

**Local-review phase:** these are instructions for a later, explicitly authorized
installation, not actions performed by the rename review. No machine needs to be
re-enrolled and no credential or state migration is required. Publishing scripts,
upgrading machines, and deploying Azure are separate approval gates.

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.ps1 | iex
```

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.sh | bash
```

The Windows installer makes `agentbridge` and its `deepbox` alias available in the current PowerShell
session. On macOS/Linux, open a new terminal after installation or run:

```bash
# Fresh default only; for an existing/custom root use the bin path printed by setup.
export PATH="$HOME/.agentbridge/bin:$PATH"
```

## Connect a machine

For a **new** machine, sign in to the browser, create a devbox, and mint its
one-time token. For an existing machine, keep its existing devbox, installation
root and credentials; the rename is not a reason to mint a new token. The token
dialog generates the complete command for each platform. Replace the example
URL with the **existing** server URL (its hostname is not being renamed):

```powershell
# Windows
$env:AGENTBRIDGE_SERVER_URL = 'https://deepbox.example'
$env:AGENTBRIDGE_TOKEN = 'hpc_box_xxxxxxxx'
agentbridge connect
```

```bash
# macOS / Linux
export AGENTBRIDGE_SERVER_URL='https://deepbox.example'
export AGENTBRIDGE_TOKEN='hpc_box_xxxxxxxx'
agentbridge connect
```

`agentbridge connect` runs from the caller's current directory and only starts the
already-installed connector. It never invokes either installer. The token is
passed through the process environment; the installer never writes it to a shim,
profile, log, or file. `DEEPBOX_SERVER_URL` and `DEEPBOX_TOKEN` still work when
their canonical counterparts are absent.

## What the one-time installer does

1. Finds a Python 3.10+ interpreter (and prints install guidance if missing).
2. Downloads the source ZIP from the public `deeporc-ai/deepbox` mirror,
   anonymously and without Git credentials. It validates and copies **both**
   `agentbridge/` (the entrypoint/configuration boundary) and `connector/`.
   An archive without either package or the entrypoint is rejected before the
   existing app is replaced. The enclosing ZIP folder name is not product-coupled.
3. During a Windows install or explicit upgrade, finds connector processes from
   this installation's virtualenv and stops their connector-owned process trees
   before replacing the source directory.
4. Refreshes `<install-root>/app`, creates or reuses `<install-root>/venv`, installs
   the connector dependencies (`httpx`, `websockets`, and `pywinpty` on
   Windows), and records the app location in the venv's `deepbox-app.pth`.
5. Installs a stable command at `<install-root>/bin/agentbridge.cmd` on Windows or
   `<install-root>/bin/agentbridge` on macOS/Linux. The shim starts `python -I -u -m agentbridge`,
   so the caller's working directory and `PYTHONPATH` cannot replace the
   installed connector package; the installer then adds the bin directory to
   the user's PATH.
6. Keeps `deepbox` and `<install-root>/deepbox-connect.cmd` or `.sh` as
   compatibility commands using the **same app and virtualenv**, not a second
   installation. Existing Windows v1 `deepbox.cmd` shims are left intact because
   they may be running the upgrade; their supported `connector.cli` target still
   uses that installation. Fresh legacy shims simply delegate to `agentbridge`.

If both selected `SERVER_URL` and `TOKEN` settings are already set, the installer
runs diagnostics and connects after setup. Otherwise it installs only and tells
the user to run `agentbridge connect`. Set `AGENTBRIDGE_INSTALL_ONLY=1` to prevent
automatic diagnostics/connection; explicit `agentbridge upgrade` always sets it.

### Installation roots and configuration compatibility

The installer chooses one root, in this order:

1. Explicit `AGENTBRIDGE_HOME`.
2. Explicit legacy `DEEPBOX_HOME` if the canonical key is absent.
3. Existing `~/.deepbox` if `~/.agentbridge` does not exist.
4. `~/.agentbridge` for a fresh install (or an already-existing canonical root).

An explicit empty HOME is an error, not permission to fall back to another
installation or the current directory. For a custom-root upgrade, use that
installation's installed command or set its HOME explicitly when invoking an
installer. Arbitrary custom roots cannot be discovered automatically. If both
default roots exist, select the intended old root explicitly rather than guessing.

Newly installed shims pin both HOME keys to their own `bin/..` location; changing
the caller's HOME settings does not redirect an existing shim to another venv.
The Unix installer records the actual, shell-quoted bin path in the login profile.
PATH updates preserve unrelated entries and prefer the selected installation;
Windows shims also handle spaces, Unicode and CMD metacharacters without baking
the root or credentials into script text. If a custom directory contains a PATH
separator (Windows `;`, Unix `:`) or line breaks, setup leaves PATH alone and
prints guidance to invoke the installed command by absolute path.
Nothing copies, moves or deletes credentials, machine identity, local databases,
project registrations or spools. Independent legacy state paths below remain intact.

All product configuration uses one presence-based compatibility boundary:
`AGENTBRIDGE_<name>` wins over `DEEPBOX_<name>`, including an **explicitly empty**
canonical value. An empty canonical token must never revive a legacy token.
`AGENTBRIDGE_SOURCE_ZIP` (legacy `DEEPBOX_SOURCE_ZIP`) selects a fork, pinned
branch or commit; the selected archive must contain both packages. Neither
environment prefix implies a GitHub repository or Azure resource rename.

## Reconnect and local commands

Once installed, use the same command from any working directory:

```text
agentbridge connect
agentbridge doctor
agentbridge status
agentbridge project add <path> [--name <display-name>]
agentbridge project list
agentbridge project remove <project-id>
agentbridge project sync
agentbridge skill install <folder> [--project [ID|NAME|PATH]] [--force]
agentbridge skill list [--project [ID|NAME|PATH]]
agentbridge skill inspect <name> [--project [ID|NAME|PATH]]
agentbridge skill remove <name> [--project [ID|NAME|PATH]] [--force]
```

The legacy `<install-root>/deepbox-connect.cmd` / `.sh` launcher delegates to
`agentbridge connect`, so existing shortcuts continue to work. `python -m agentbridge`
is also available in a source checkout; the older `python -m connector` and
`python -m connector.cli` forms remain supported. The local Windows launcher
`scripts/start-connector.cmd` now delegates to the canonical entrypoint and reads
the same environment settings; it does not prompt for or expand tokens in CMD.

## Local projects and skills

### Managing local projects

Project paths live only in connector-local `state.db` under
`%LOCALAPPDATA%\deepbox` on Windows or
`${XDG_STATE_HOME:-~/.local/state}/deepbox` on macOS/Linux. The server and browser
receive only a stable project ID and display name, never the path or local runtime
configuration. Register a project before selecting it while creating an agent:

```powershell
agentbridge project add "C:\src\my-repo" --name "my-repo"
agentbridge project list
agentbridge project remove <project-id>
agentbridge project sync
```

```bash
agentbridge project add "$HOME/src/my-repo" --name "my-repo"
agentbridge project list
agentbridge project remove <project-id>
agentbridge project sync
```

`add` requires an existing directory and stores its canonical absolute path;
adding the same path again reuses its ID. `remove` deletes only the registration,
not the directory. With a token, `add` and `remove` immediately report the path-free
inventory; otherwise run `sync` later. A project with a managed project-scoped
skill cannot be removed until that skill is removed.

The browser's **Add agent** form refreshes projects at the point of use. Its
**Add a local project** action only builds and copies a command such as
`agentbridge project add "C:\src\my-repo" --name "my-repo"`; it never browses or
mutates the workstation. Run the command locally and then choose **Refresh projects**.

### `SKILL.md` package schema

A skill is a UTF-8 directory tree with a `SKILL.md` file at its root. The file
starts with YAML frontmatter containing string `name` and `description` fields,
followed by normal Markdown instructions:

```markdown
---
name: review-pr
description: Review a pull request for correctness, tests, and operational risk.
---
# Review a pull request

Read the changed files before reporting findings.
```

The directory basename must exactly equal `name`. Names use lower-kebab-case,
are at most 64 characters, and descriptions are at most 1,024 characters.
agentbridge decodes `SKILL.md` strictly as UTF-8, parses frontmatter with
`yaml.safe_load`, and requires a mapping with string keys. A package may contain
at most 256 regular files and 10 MiB total. Symlinks, junctions, other reparse
points, traversal, and files that change during hashing are rejected.
Script-looking files and a `scripts/` directory are allowed but set
`contains_scripts`; agentbridge itself never executes any skill content.

### Install and manage skills

Personal scope is the default. For project scope, provide `--project` with a
registered ID, unique case-insensitive name, or exact normalized path. Supplying
`--project` without a value is equivalent to `--project .` and selects the
longest registered project containing the current working directory.

```powershell
# Personal scope
agentbridge skill install "C:\Skills\review-pr"
agentbridge skill list
agentbridge skill inspect review-pr
agentbridge skill remove review-pr

# Project scope
agentbridge skill install "C:\Skills\review-pr" --project "my-repo"
agentbridge skill list --project "my-repo"
agentbridge skill inspect review-pr --project "my-repo"
agentbridge skill remove review-pr --project "my-repo"
```

Install validates and hashes the source twice, copies it to
`<connector-state-root>/skills/store/<digest>/<name>/`, then stages replacements
in every personal or project skill root declared by the registered runtime adapter
families. The local database records scope, project, targets, and binding paths.
Repeated root discovery merges with earlier bindings instead of orphaning them.
`list` and `inspect` verify both the store and every binding and return
`installed`, `drifted`, or `missing`. Install and remove refuse drifted
destinations unless `--force` is explicit. Removing the final reference also
garbage-collects the content-addressed store directory.

While connected, the connector reports inventory changes automatically. The
server stores only `id`, `name`, `description`, `digest`, `scope`, `project_id`,
`targets`, `contains_scripts`, and `status`; it receives no source, store,
project, or binding path and never reads model credentials.

### Structured chat controls

Opening **Add agent** refreshes runtime capabilities and projects before rendering
its selectors. When live model discovery is unavailable or empty, the connector
retains the adapter family's static models with `models.status=partial` and
`models.source=adapter`. Model choices fall back from control choices to surface
model facts and then the family catalogue; structured chat always includes
**Runtime default**. Adapters that allow custom IDs receive an editable model
combobox; other model and reasoning controls remain selects.

Claude structured model selection is turn-scoped: after the first turn the connector
applies a changed model through a native `set_model` request before sending the next
prompt. Explicit models can therefore be switched live; because the protocol cannot
clear an applied model, returning to **Runtime default** requires **New chat**. True
session-scoped controls remain editable until the session is configured
or contains its first chat item. They then lock with a prompt to start **New chat**.
That action creates a blank persisted session and re-enables those controls without
stopping another collaborator's session or deleting saved history. **End session**
is a separate confirmed action for the keyboard holder or a workspace Admin/Owner.

### Terminal and shared-chat checks

- Explicit **Terminal** never resumes a structured Chat. **New session** retains
  that surface. If xterm assets could not load, the page shows an actionable error
  before creating a session; rendering a Chat does not initialize xterm.
- On Windows, use the installed CLI's direct argv or a correctly quoted explicit
  command. PTY startup failures are surfaced instead of falling back to a fake
  runtime. The connector also cleans up failed starts and exited PTYs.
- Shared users need **Operator** or above to send chat messages. **Viewer** means
  read-only, including existing invitations and memberships. An owner/admin must
  explicitly change that role in **Members & invitations → role → Save**;
  connecting again does not promote a Viewer. A failed Save leaves the old grant
  unchanged and keeps the selected role available for retry.
- Structured chat does not need a keyboard lease. Interactive terminals still
  have a single keyboard holder, so another operator must request control first.
- Connector server URLs require HTTPS outside loopback and cannot contain
  credentials, a query string or a fragment. Diagnostics report safe error classes
  rather than echoing raw exception text or credential-bearing URLs.

## Upgrade explicitly

Upgrade only when requested:

```text
agentbridge upgrade
```

The stable command downloads the current installer with
`AGENTBRIDGE_INSTALL_ONLY=1` (and its legacy alias). The installer may stop a
running Windows connector and refresh `<install-root>/app`; normal
`agentbridge connect` calls never do this. Stop the connector before a Unix
upgrade. After an upgrade, run `agentbridge connect` again with the same existing
credentials if the previous connector was stopped. Never delete the old root or
mint a new machine token merely to adopt the new command name.

### Windows process safety during install or upgrade

`install.ps1` matches only this installation's virtualenv Python running
`-m agentbridge`, `-m connector` or `-m connector.cli`. Matching is anchored at
the actual interpreter/module invocation, not a string inside `-c` or another
program's arguments. It snapshots and stops that process's
connector-owned child tree, waits for handles to be released, and retries the
source-directory replacement. It does not stop unrelated Python processes and
never logs inspected command lines.

If a separate shell has manually changed its working directory to
`<install-root>\app`, leave that directory or close the shell before upgrading.

## Hosting the installer scripts

The browser and examples use these anonymous GitHub Raw endpoints on `main`:

```text
https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.ps1
https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.sh
```

**Deliberate external boundary:** the source repository is still
`yusx-microsoft/deepbox`, with the `deeporc-ai/deepbox` public mirror. There is no
assumed `agentbridge` repository or new domain. These URLs, Azure resources,
existing server hostnames and authentication callbacks stay unchanged until a
separately approved migration.

Publishing is **not** part of the local rename/review phase. After explicit
approval, publish the same reviewed packages and both installer scripts to the
public mirror; there is no separate Blob upload step. Keep the UI command,
downloaded source and entrypoint in sync. Do not stage, commit, push or deploy as
part of merely reviewing the rename.

Only after publishing is approved, verify both endpoints anonymously (network
checks are not part of the local review):

```powershell
Invoke-WebRequest -UseBasicParsing https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.ps1 | Select-Object StatusCode
```

```bash
curl -I https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.sh
```

Both should return HTTP `200`. GitHub Raw may cache briefly after a push; pin a
commit SHA in the URL when an immutable installer is required.
