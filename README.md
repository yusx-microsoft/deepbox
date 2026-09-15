# AgentBridge

AgentBridge is a **central bridge for people and teams to manage agents across
many machines**. Agents can run on local computers, remote hosts, or devboxes;
users should not have to visit each device separately to find, inspect, or operate
them. A Workspace groups those devices and agents and can be shared with teammates
under the same access rules.

The primary relationship is **human/team → distributed agents**, not autonomous
agent-to-agent collaboration. An inventory of 100 agents across devices and the
number of simultaneously visible panes are different concerns. The current four-pane
cap is a view limit, not the intended size of the managed agent inventory; a real
100-agent/device stress test is separate from the existing UI regression tests.

> The server is a control plane, not an AI product. It never runs models and never
> stores model API keys. Intelligence and credentials stay on your devbox.

**The user has authorized release of this current workbench.** The separate static
design candidates are not part of the application release or the product roadmap.
The existing Azure app remains `deepbox-webdata-du`; source repository names,
Azure identities and installed data are not being migrated. Verify rollout through
the actual deployment status and `/api/version`, not a Git push alone. See the
[rename contract](docs/agentbridge.md) and [review record](docs/review.md).
External repository, Azure, and domain migration remains a separate pending step.

**Naming:** **AgentBridge** is the display name. `agentbridge` remains the lowercase
CLI/package identifier and the basis for the `AGENTBRIDGE_*` environment prefix;
`deepbox` compatibility identities are unchanged. This is not an auth or data migration.

## Components

- `agentbridge/product.py` — separate display and machine names, canonical `AGENTBRIDGE_*`
  environment lookup with `DEEPBOX_*` compatibility, and install-home resolution.
- `server/` — FastAPI + WebSocket + SQLite. Provides identity, workspaces,
  channels/sessions, presence, and opaque frame relay. It also owns the Protocol
  v3 durable recording pipeline (frames, checkpoints, asciicast export, and
  retention). The server never runs models, never holds model keys, and never
  interprets runtime/model strings.
- `connector/` — A user-launched process that bridges local agent CLIs to the
  server. Installed once, then managed with `agentbridge connect` / `doctor` /
  `status` / `project` / `skill` / `upgrade`; routine connects never refresh the
  install directory. `deepbox` remains a compatibility alias. A connector-only runtime registry builds and validates the
  argv for Claude Code / Copilot CLI / Codex CLI / mock, and reports capabilities
  that stay opaque to the server and web UI.
- `web/` — a refined, structured-first split workbench, not a dashboard or a new web
  framework. Top navigation and a compact collapsible sidebar frame independent
  panes. Model/reasoning controls remain
  capability-driven; explicit **Terminal** never reuses Chat or an unknown surface.
  See the [current module map](docs/implementation.md#5-web-web).

## Conversation workbench

- The local draft returns to the pre-tmux workbench: refined top navigation,
  a compact collapsible sidebar, flexible split panes, and uncluttered transcripts.
  Restrained sans-serif UI type, monospace code/data, subtle borders and spacing,
  and light/dark themes keep the shell quiet. No green tmux status bar, forced
  full-screen TUI, or remote fonts.
- **Optional tmux-style shortcuts are opt-in, default off.** Control+B is not
  intercepted unless the user enables them. They supplement visible navigation
  and pane controls rather than define the visual shell. The allowlisted command
  prompt performs UI actions only, never OS commands or agent prompts; see the
  [optional key contract](docs/agentbridge.md#optional-tmux-style-interaction).
- Up to **four visible panes** (not four managed agents), split right or below in a binary tree. Drag a separator or
  focus it and use the axis arrow keys; `Home` resets its ratio. Select, maximize,
  restore, or close a pane without replacing the other panes' sessions.
- Each pane owns its socket, chat/terminal/replay state, and teardown. **Close
  pane** only detaches; **New chat** preserves the old shared session. Ending a
  session is a separate, confirmed, permission-checked action.
- Unsent drafts stay pane-local; focusing, resizing, or splitting another pane
  must not replace them. Layout preferences do not persist draft text.
- Layout preferences are scoped to the signed-in user and workspace. They contain
  geometry and target IDs/surface/kind, not messages, files, tokens, or roles.
  Restoring missing or ended targets never auto-creates sessions.
- Local helpers load in deterministic order. Chat and app boot do not wait for
  a CDN. `terminal-assets.js` loads the **existing pinned jsDelivr xterm dependency**
  only for terminal use; failure is visible before a session is created. No vendor
  assets were downloaded or newly vendored during this implementation.

## Collaboration and access control

- **Workspaces** — Every user gets a personal workspace and can create more. The
  sidebar groups resources as Workspace → Devbox → Agent, and the
  `viewer / operator / admin / owner` roles gate every resource under a workspace.
- **Shared chat** — Operators, admins, and owners can send messages without
  taking a keyboard lease. Viewers remain read-only. Interactive terminals retain
  a single keyboard holder; this does not restrict structured conversations.
- **Sign-in** — Local password sign-in is available for development and hybrid
  migration. In Azure, App Service Easy Auth can front tenant-scoped Microsoft
  Entra accounts; AgentBridge additionally checks a tenant allowlist.
- **Invitations** — Workspace owners and admins issue single-use, expiring,
  email-bound join links. The UI explicitly selects **Operator** (can send messages)
  for new invitations; the API still defaults to Viewer. Existing Viewer members
  are never promoted automatically—an owner/admin can change a colleague's role
  in **Members & invitations**, then explicitly click **Save**.
  The deployment owner separately manages local account
  invitations, disabling, and re-enabling.
- **Termination** — Terminal input, resize, and termination require the keyboard
  holder. Structured termination requires the holder or workspace Admin/Owner;
  permission to chat alone is not permission to end everyone's session.

## Security baseline

- Argon2id password hashing with transparent upgrade of older hashes.
- Production Origin allowlist, tiered rate limiting, and security headers.
- Redacted JSON audit logging.
- Immediate disconnect on credential revocation.
- Secure erase of durable recordings for workspace admins and owners.

See [`docs/design.md`](docs/design.md) for how the durable recording pipeline
(frames, checkpoints, replay, and retention) works.

## Other documentation

- [`docs/review.md`](docs/review.md) — Local draft review and acceptance checklist;
  previous release approval does not apply to this work.
- [`docs/agentbridge.md`](docs/agentbridge.md) — Phased rename, environment/home
  compatibility, and the separately approved final external rename step.
- [`docs/design.md`](docs/design.md) — Technical architecture and protocol.
- [`docs/product-design.md`](docs/product-design.md) — Product positioning, users,
  object model, core flows, and design principles.
- [`docs/planning.md`](docs/planning.md) — Current v1 implementation status,
  architectural invariants, remaining risks, and validation commands.
- [`docs/remote-deployment.md`](docs/remote-deployment.md) — Connecting Windows
  machines over Tailscale.
- [`docs/azure-deployment.md`](docs/azure-deployment.md) — Azure App Service
  (Linux) deployment with keyless Entra / Easy Auth sign-in.
- [`docs/install.md`](docs/install.md) — Installing the `agentbridge` command once,
  routine connects, explicit upgrades, and safe refresh on Windows.
- [`docs/implementation.md`](docs/implementation.md) — Notes on the current code.
- [`docs/onboarding.md`](docs/onboarding.md) — First owner setup, roles,
  invitations, and the member lifecycle.
- [`docs/operations.md`](docs/operations.md) — Operations handbook: structured
  logging, connection visibility, readiness checks, backup/restore, capacity
  alerts, and version/smoke checks.
- [`docs/persistence.md`](docs/persistence.md) — Session persistence design.

## Connecting a machine: install once, connect anytime

Installation and connection are explicit user actions, not part of reviewing this
draft. When you choose to connect a machine, follow [`docs/install.md`](docs/install.md),
set `AGENTBRIDGE_SERVER_URL` and `AGENTBRIDGE_TOKEN` for your chosen server, then run:

```text
agentbridge connect
```

Upgrading is explicit: `agentbridge upgrade`. Routine connects do not install or
download anything. Legacy `deepbox` commands and existing `.deepbox` or custom
install roots remain supported; nothing is moved automatically. Canonical
environment variables win by **presence**, even if empty; see the
[exact precedence rules](docs/agentbridge.md#environment-and-home-compatibility).

## LocalProjects and user skills

Register projects on the machine running the connector. Absolute paths are stored
only in the connector-local `state.db`; the server receives only a stable project ID
and display name. The browser's project actions only
generate copyable commands and never browse the host filesystem.

```text
agentbridge project add "C:\Code\my-project" --name "My project"
agentbridge project list
```

A skill is a directory containing a UTF-8 `SKILL.md` whose directory name equals
the lower-kebab-case `name` in the YAML frontmatter. Skills install to the
personal scope by default; add `--project` to install into a registered project
scope:

```text
agentbridge skill install "C:\Skills\review-pr"
agentbridge skill install "C:\Skills\review-pr" --project "My project"
agentbridge skill list
agentbridge skill inspect review-pr
agentbridge skill remove review-pr
```

The project-scoped `list` / `inspect` / `remove` commands take the same
`--project "My project"` argument. The connector validates boundaries; rejects
symlink/reparse targets, over-limit trees, and trees that change during reads;
copies content into `<connector-state-root>/skills/store/<digest>/<name>/` and
into the skill roots each adapter family declares. AgentBridge never executes skill
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
session is configured or the first chat item appears. **New chat** creates an
empty persisted session and reopens the controls without stopping other viewers'
session or deleting prior history. **End session** is a separate confirmed action
for the keyboard holder or a workspace Admin/Owner.

## Local development and review

Use the [implementation guide](docs/implementation.md#7-testing) for Python and
all browser test suites. The [review guide](docs/review.md) describes isolated
mock/fake-connector acceptance checks. It does not authorize installation,
connection to a live workspace, real model CLI execution, or deployment.
The existing checkout path remains `C:\Code\deepbox`.

## Web UI keyboard shortcuts

- `Ctrl/Cmd + K` — Open the command palette (filter to open an agent, open
  history, create a devbox, or go to owner).
- Inside the palette, `↑` / `↓` move the selection, `Enter` runs it, `Esc` closes.
- The Fleet search box filters devboxes and agents live.
- Keyboard lease within a session: `Request` / `Take keyboard` / `Release` /
  `Hand off` (a viewer is always read-only).
