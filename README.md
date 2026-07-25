# deepbox

deepbox is an **agent switchboard / control plane**. You connect the agent CLIs
already running on your own devbox (Claude Code, Copilot CLI, Codex CLI, and
similar tools) to the server, sign in to the web UI, and interact with them as if
you were sitting at the local terminal.

> The server is a control plane, not an AI product. It never runs models and never
> stores model API keys. Intelligence and credentials stay on your devbox.

See [`docs/design.md`](docs/design.md) for the technical architecture.

## Components

- `server/` — FastAPI + WebSocket + SQLite. Provides identity, workspaces,
  channels/sessions, presence, and opaque frame relay. It also owns the Protocol
  v3 durable recording pipeline (frames, checkpoints, asciicast export, and
  retention). The server never runs models, never holds model keys, and never
  interprets runtime/model strings.
- `connector/` — A user-launched process that bridges local agent CLIs to the
  server. Installed once, then managed with `deepbox connect` / `doctor` /
  `status` / `project` / `skill` / `upgrade`; routine connects never refresh the
  install directory. A connector-only runtime registry builds and validates the
  argv for Claude Code / Copilot CLI / Codex CLI / mock, and reports capabilities
  that stay opaque to the server and web UI.
- `web/` — A single-page **structured-first switchboard**. For runtimes that
  support headless/JSON output it renders a native chat surface with
  capability-driven model and reasoning controls plus **New chat**; it falls back
  to xterm.js only for legacy/TUI runtimes. The left navigation is organized as
  Workspace → Devbox → Agent. Adding an agent refreshes the runtime/project
  inventory and lets you pick a LocalProject; the "add a local project" action
  only produces a copyable `deepbox project add ...` command and never browses the
  host filesystem. The Skills view shows only the path-free metadata the connector
  reports plus local management commands. One-time tokens appear only in
  memory/DOM. Auto-reconnect, structured-event restore, terminal screen restore,
  and durable session history are all preserved. The DOM-free logic lives in the
  UMD module `web/ui.js`, covered by `web/ui.test.js` (`node:test`).

## Collaboration and access control

- **Workspaces** — Every user gets a personal workspace and can create more. The
  left navigation groups resources as Workspace → Devbox → Agent, and the
  `viewer / operator / admin / owner` roles gate every resource under a workspace.
- **Sign-in** — Local password sign-in is available for development and hybrid
  migration. In Azure, App Service Easy Auth can front tenant-scoped Microsoft
  Entra accounts; deepbox additionally checks a tenant allowlist.
- **Invitations** — Workspace owners and admins issue single-use, expiring,
  email-bound join links. The deployment owner separately manages local account
  invitations, disabling, and re-enabling.

## Security baseline

- Argon2id password hashing with transparent upgrade of older hashes.
- Production Origin allowlist, tiered rate limiting, and security headers.
- Redacted JSON audit logging.
- Immediate disconnect on credential revocation.
- Secure erase of durable recordings for workspace admins and owners.

See [`docs/design.md`](docs/design.md) for how the durable recording pipeline
(frames, checkpoints, replay, and retention) works.

## Other documentation

- [`docs/product-design.md`](docs/product-design.md) — Product positioning, users,
  object model, core flows, and design principles.
- [`docs/planning.md`](docs/planning.md) — Current v1 implementation status,
  architectural invariants, remaining risks, and validation commands.
- [`docs/remote-deployment.md`](docs/remote-deployment.md) — Connecting Windows
  machines over Tailscale.
- [`docs/azure-deployment.md`](docs/azure-deployment.md) — Azure App Service
  (Linux) deployment with keyless Entra / Easy Auth sign-in.
- [`docs/install.md`](docs/install.md) — Installing the `deepbox` command once,
  routine connects, explicit upgrades, and safe refresh on Windows.
- [`docs/implementation.md`](docs/implementation.md) — Notes on the current code.
- [`docs/onboarding.md`](docs/onboarding.md) — First owner setup, roles,
  invitations, and the member lifecycle.
- [`docs/operations.md`](docs/operations.md) — Operations handbook: structured
  logging, connection visibility, readiness checks, backup/restore, capacity
  alerts, and version/smoke checks.
- [`docs/persistence.md`](docs/persistence.md) — Session persistence design.

## Connecting a machine: install once, connect anytime

Copy the one-time install command for your platform from the browser or from
[`docs/install.md`](docs/install.md). After installing, set the server URL and
devbox token issued by the browser and run:

```text
deepbox connect
```

Upgrading is explicit: `deepbox upgrade`. Only install/upgrade refreshes
`~/.deepbox/app`; `deepbox connect` never downloads, reinstalls, or touches the
install directory in use.

## LocalProjects and user skills

Register projects on the machine running the connector. Absolute paths are stored
only in the connector-local `state.db`; the server receives only a stable project ID
and display name. The browser's project actions only
generate copyable commands and never browse the host filesystem.

```text
deepbox project add "C:\Code\my-project" --name "My project"
deepbox project list
```

A skill is a directory containing a UTF-8 `SKILL.md` whose directory name equals
the lower-kebab-case `name` in the YAML frontmatter. Skills install to the
personal scope by default; add `--project` to install into a registered project
scope:

```text
deepbox skill install "C:\Skills\review-pr"
deepbox skill install "C:\Skills\review-pr" --project "My project"
deepbox skill list
deepbox skill inspect review-pr
deepbox skill remove review-pr
```

The project-scoped `list` / `inspect` / `remove` commands take the same
`--project "My project"` argument. The connector validates boundaries; rejects
symlink/reparse targets, over-limit trees, and trees that change during reads;
copies content into `<connector-state-root>/skills/store/<digest>/<name>/` and
into the skill roots each adapter family declares. deepbox never executes skill
files, and the server stores only a path-free inventory. See
[`docs/install.md`](docs/install.md#local-projects-and-skills) for the full
schema, limits, scope resolution, and drift/`--force` rules.

## Structured chat controls

When live model discovery is unavailable or returns no model ID, the connector
keeps the adapter's static catalog as a `partial/adapter` fallback. The UI falls
back in order: control choices → surface model facts → family model catalog. It
always offers **Runtime default**, and renders an editable model combobox only
when the adapter allows custom model IDs. **Runtime default** sends no `--model`.

For Claude structured runtimes the model is a per-turn control: before a later
turn the connector sends `set_model` to the same process, so you can still switch
between explicit models after the first turn. The protocol cannot clear an
already-set model, so returning to **Runtime default** requires **New chat**.
True session-scoped controls (such as permission and reasoning) lock once the
session is configured or the first chat item appears. **New chat** terminates the
current runtime session, creates an empty persisted session, and reopens the
controls without deleting prior history. Terminating a session still requires the
operator role and the current keyboard lease.

## Quick start (mock agent, end to end)

```bat
cd C:\Code\deepbox
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
:: On a connector machine, also install (includes Windows-only pywinpty):
::   .venv\Scripts\python -m pip install -r requirements-connector.txt
:: 1) Start the local dev server (defaults to 127.0.0.1:8077)
.venv\Scripts\python -m server
:: 2) Open http://localhost:8077, sign in, create a Devbox, copy its one-time token
:: 3) Start the connector in a new terminal; it probes and reports local runtimes
set DEEPBOX_SERVER_URL=http://localhost:8077
set DEEPBOX_TOKEN=hpc_box_...
.venv\Scripts\python -m connector
:: 4) Back in Fleet, add an Agent, pick "mock" from the dropdown, then open it
::    Agents can be deleted; an online connector syncs adds/removes without a reconnect
```

## Web UI keyboard shortcuts

- `Ctrl/Cmd + K` — Open the command palette (filter to open an agent, open
  history, create a devbox, or go to owner).
- Inside the palette, `↑` / `↓` move the selection, `Enter` runs it, `Esc` closes.
- The Fleet search box filters devboxes and agents live.
- Keyboard lease within a session: `Request` / `Take keyboard` / `Release` /
  `Hand off` (a viewer is always read-only).
