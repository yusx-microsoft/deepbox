# DeepBox Implementation Guide

This is a current, practical map of the DeepBox codebase: how the parts fit
together, what each module does, the protocol surfaces, and how to run the
tests. For product intent see [`design.md`](design.md) and
[`product-design.md`](product-design.md); for persistence internals see
[`persistence.md`](persistence.md); for operations see
[`operations.md`](operations.md) and [`remote-deployment.md`](remote-deployment.md).

## 1. Architecture at a glance

Three cooperating parts:

- **Server** (`server/`) — FastAPI + SQLite. Handles identity, workspaces,
  devboxes, agents, sessions, keyboard leases, durable recording, and relaying
  frames between browsers and connectors. It never runs models or holds model
  credentials.
- **Connector** (`connector/`) — a Python client the user runs on their own
  machine. It launches the actual CLI agents (Claude Code, Copilot CLI, Codex
  CLI, or a mock), owns the PTYs / structured sessions, and streams output back
  over a durable spool. Model keys and LocalProject source paths stay local;
  terminal bytes or canonical events are relayed through the server.
- **Web** (`web/`) — a static single-page app. Native structured chat for
  runtimes that support JSON output, with an xterm.js terminal fallback for
  everything else. The browser holds no connector token and no model key.

Wire protocol version is `3` (`PROTOCOL_VERSION` in `server/app/models.py` and
`connector/transport.py`); the server validates it during the WebSocket hello,
and connector diagnostics compare it with `/api/health`.

### Data flow

```
Browser  <--WSS /ws/term-->  Server  <--WSS /ws/devbox-->  Connector  -->  CLI agent (PTY / structured)
   |            (relay + durable recording + leases)             |
   +-- REST /api/* (identity, fleet, sessions, replay) ----------+
```

1. The connector authenticates to `/ws/devbox` with a bearer devbox token,
   reports its runtimes/projects/skills (paths stripped), and holds the agent
   processes.
2. A browser opens `/ws/term` for a session; the server relays input frames to
   the owning connector and output/event frames back to the browser.
3. Every output/event frame is made durable on the connector (spool) and on the
   server (recording) with strict per-stream sequencing, so re-attach and replay
   are lossless. See [`persistence.md`](persistence.md).

## 2. Server (`server/app/`)

- **`main.py`** — FastAPI app: all REST routes, both WebSocket endpoints, static
  hosting of the SPA, and health/readiness. Registers an explicit
  `application/javascript` MIME type so Windows MIME-registry quirks plus
  `nosniff` cannot block the SPA.
- **`models.py`** — SQLAlchemy ORM schema and `init_db()` (per-connection SQLite
  PRAGMAs: WAL, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout`,
  `wal_autocheckpoint`). Holds `PROTOCOL_VERSION`, retention constants, and the
  additive `_migrate()` / workspace backfill. `Session.surface` stores `terminal`
  or `structured`; migrated sessions remain unknown until a validated ready or
  snapshot establishes their surface. No runtime-name inference is needed.
- **`hub.py`** — in-memory `Hub`: routes frames between connected browsers and
  connectors, per-devbox bounded send queues, hello ordering, duplicate-connection
  retirement, and presence.
- **`live.py`** — `LiveRegistry`: current terminal screen (pyte) per session and
  the bounded structured `event_restore()` tail used on re-attach.
- **`recording.py`** — `RecordingStore`: the durable frame/checkpoint ledger,
  Protocol v3 classification (NEW/DUPLICATE/GAP/CONFLICT/INVALID), asciicast v2
  and replay export, retention enforcement, and secure erase.
- **`security.py`, `identity.py`, `util.py`** — request authentication, session
  cookies, Argon2id password hashing with legacy salted-SHA-256 upgrade, and
  Microsoft Easy Auth mapping (`local | hybrid | microsoft` modes, tenant
  allowlist in production).
- **`config.py`** — environment/`.env` config; production mode refuses dev
  secrets, an empty Origin allowlist, and non-Secure cookies.
- **`version.py`, `capacity.py`, `logging.py`** — build provenance, capacity
  thresholds, and structured JSON logging.
- **`server/ops/backup.py`, `server/ops/smoke.py`** — validated online SQLite
  backup/restore and a post-restart smoke check.

## 3. REST and WebSocket surfaces

All REST routes are under `/api`. Authorization is by workspace membership and a
four-level role ladder `viewer < operator < admin < owner`; access is aggregated
across all of the caller's memberships. Highlights:

- **Health/ops:** `GET /api/health`, `GET /api/ready`, `GET /api/version`,
  `GET /api/admin/version`, `GET /api/admin/capacity`.
- **Auth/identity:** `GET /api/auth/config`, `POST /api/auth/register`,
  `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/me/user`,
  `GET /api/me`, Microsoft `start`/`callback`/`logout`, and
  bootstrap-status/bootstrap.
- **Workspaces & members:** `GET|POST /api/workspaces`,
  `GET|POST /api/workspaces/{id}/members`,
  `PATCH|DELETE /api/workspaces/{id}/members/{user_id}` (last owner protected).
- **Invitations:** workspace invitations
  (`GET|POST /api/workspaces/{id}/invitations`, `DELETE ...`), plus email-bound,
  single-use, expiring `POST /api/workspace-invitations/preview` and `/accept`.
- **Fleet:** `GET|POST /api/devboxes`, `DELETE /api/devboxes/{id}`, devbox tokens
  (`GET|POST|DELETE`), agents (`POST /api/devboxes/{id}/agents`,
  `DELETE /api/agents/{id}`), and connector inventory intake
  (`POST /api/devboxes/{id}/runtimes|projects|skills`, bearer authenticated).
- **Sessions & replay:** `GET|POST /api/agents/{id}/sessions`,
  `GET /api/sessions/{id}/messages`, `GET /api/sessions/{id}/recording`
  (asciicast v2), `GET /api/sessions/{id}/replay` (header/events/checkpoints/
  duration/metadata), `DELETE /api/sessions/{id}/recording` (workspace admin/owner
  secure erase), `PATCH /api/sessions/{id}/retention`
  (`none|7d|30d|permanent`, workspace admin/owner). All enforce session ownership
  for legacy sessions or workspace RBAC for workspace sessions.

WebSockets:

- **`/ws/devbox`** — connector transport. Bearer token via the `Authorization`
  header only (never query string). Carries the `hello`, inventory, control, and
  durable output/event frames.
- **`/ws/term`** — browser session channel. Origin-checked; cookie-authenticated.
  Carries input, resize, keyboard-lease, and the relayed output/event stream.

Session create/list and ready/snapshot frames carry the generic `surface` value.
An attach cannot silently change an existing session's surface. Structured
`stdin`/`input`, permission replies, and interrupts require Operator or above,
but not the terminal keyboard lease. Terminal input and resize retain exclusive
keyboard ownership. Structured keyboard REST requests return 400; browser keyboard
frames return `keyboard_not_required`. Closing a process is separate: the keyboard
holder or workspace Admin/Owner can terminate it (an Operator cannot terminate a
structured process merely by being able to chat).

Every input checks current identity, membership, and attachment. Every connector
session frame checks the current devbox connection, session's owning agent, and
instance fence—including legacy output, ready, exit and process snapshots, not
just durable v3 frames. Malformed frames return an error instead of tearing down
the connection. Valid legacy wire forms remain supported at this boundary.

## 4. Connector (`connector/`)

- **`client.py` / `Connector.run()`** — top-level loop: `GET /api/me`, report
  projects/skills/runtimes, run a ~2s inventory watcher, open `/ws/devbox`,
  handle handshake and heartbeats (every 20s), and reconnect with backoff after
  abnormal closes, resuming un-ACKed frames from the spool.
- **`supervisor.py`** — `SessionSupervisor` owns the sessions and PTYs
  (session-authoritative). `attach()`/`detach()` connect and disconnect a
  transport without killing the PTY; output is emitted into the durable spool and
  drained to the transport by strict `seq`.
- **`transport.py`** — the WebSocket-facing side. Split from the supervisor so a
  transport restart never kills a running agent. Runs all-in-one over an
  in-process `LoopbackChannel` by default (`python -m connector`), or as two
  processes via `--mode supervisor` / `--mode transport` over a local named pipe
  (Windows) or Unix socket (`0600`).
  There is one sender/delivery path: the unused `Connector._sender` and its
  supervisor-property facades have been removed. Diagnostics and both transports
  share URL validation: HTTPS is required off loopback, and credential-bearing,
  query-bearing or fragment-bearing base URLs are rejected before using a token.
- **`spool.py`** — durable output spool (SQLite, WAL, `synchronous=FULL`); see
  [`persistence.md`](persistence.md).
- **`runtimes.py`** — single source of truth for runtime adapters. `RuntimeAdapter`
  declares stable id/label, argv, model/permission allowlists, non-secret env,
  probe hints, terminal/structured surfaces, and personal/project skill roots.
  Built-in: `mock`, `claude-code`, `copilot-cli`, `codex-cli`,
  `claude-code-structured`, and `copilot-cli-structured`.
  `resolve_cmd()` builds argv (platform-appropriate parsing of `launch_cmd`, else the
  shared `build_command()`), rejects empty/control/shell-metacharacter tokens,
  and spawns argv directly (no shell). Unknown runtime IDs without an explicit
  command are rejected instead of being replaced with `mock`.
- **`runtime_probe.py`** — runs local subprocess probes and emits capability
  schema v2 (installation, compatibility, authentication, models, surfaces, and a
  content-hash `revision`). Executable paths, raw probe output, and credentials
  are never uploaded. Version metadata is a parsed version number, not an arbitrary
  first output line. Probe output is captured in a temporary file and only a 64 KiB
  prefix is read into memory; timeouts and failed probes keep their fixed statuses.
- **`pty_session.py`** — cross-platform pseudo-terminal for interactive TUIs
  (Windows ConPTY via `pywinpty`; POSIX `pty.fork` + `os.execvp`). Default size
  120x30, re-sized by the first browser `resize` frame. Windows passes argv directly
  to `PtyProcess.spawn`; explicit command parsing preserves Windows paths and
  quoted arguments. Normal exit and kill release the child and PTY reader handles;
  a failed working-directory change never falls through to a different directory.
- **`agent_session.py`** — structured (`kind="event"`) sessions. Translates a
  runtime's JSON stream into display-safe canonical events (`status`,
  `session.config`, `user.echo`, `message.delta`, `message`, `tool.call`,
  `tool.result`, `permission.ask`, `turn.end`, `error`). `write_turn(text,
  options)` applies per-turn/session controls under the adapter allowlist.
  All writes use the same bounded turn queue; per-turn processes finish before
  the next queued turn starts. Full/closed queues reject input explicitly.
  Kill cancels queued work, so a scheduled turn cannot launch a new child later.
  Supervisor starts are serialized per session and invalidated by retirement or
  shutdown; partially-started children are cleaned and failures have safe,
  actionable `runtime.unavailable` messages.
- **`local_store.py`** — local project + skill state (SQLite); project paths are
  retained locally and stripped from cloud inventory; see
  [`persistence.md`](persistence.md).
- **`skills.py`** — parses `SKILL.md` frontmatter, validates the tree
  (regular files only; no traversal/symlink; 256 files / 10 MiB caps), and does
  atomic staged install/rollback/drift/GC. Scripts are surfaced
  (`contains_scripts=true`) but never executed by DeepBox.
- **`diagnostics.py`** — shared server URL validation and `run_doctor()`
  URL/TLS/DNS/protocol checks. Unknown connection failures expose the exception
  class, not raw exception text that may contain URLs or credentials.
- **`mockcli.py`** — a fake CLI (echoes `you said: ...`) so the full chain can be
  exercised without a real agent.

### Runtime / surface behavior

- One runtime family may expose several adapter surfaces. The browser picks the
  family's default surface (Claude/Copilot default to `structured`) and sends it
  in the attach frame; the connector confirms via `session.ready.surface`. If the
  runtime is missing or cannot start, it returns `runtime.unavailable` (with
  installation/compatibility/authentication and available surfaces) rather than
  silently falling back to a terminal.
- Model/permission options are validated per turn against the adapter allowlist.
  Claude switches model live in one process via a `set_model` control request;
  runtimes without a live mapping apply the option through per-turn argv.
- File input is base64 over the wire; the connector re-validates count/size, and
  only file name/type/size (never bytes or temp paths) enter echoes and durable
  history.
- Adding a new runtime is one registry entry plus an adapter — no server or
  browser changes.

## 5. Web (`web/`)

- **`index.html`** — mounts xterm.js (CDN) and declares the external stylesheet
  once; `styles.css` is the single source of truth for themes.
- **`app.js`** — UI controller: auth config, fleet rendering
  (`Workspace → Devbox → Agent`), workspace management affordances, one-time
  token display, session content area, DOM/WebSocket/FileReader wiring, and
  replay UI. Role checks are always re-enforced on the server.
- **`ui.js`, `chat.js`, `replay.js`, `collaboration.js`** — shared helpers:
  fleet aggregation, filtering, command building, runtime label/option handling,
  the canonical event reducer, JSONL parsing, replay seek/checkpoint logic, and
  collaboration view state, plus the transcript renderer. Loaded in fixed deferred
  script order before `app.js`; there are no lazy-loader promises or single-flight
  chat-mount gates. Explicit Terminal selects only a matching live terminal;
  New session retains the selected surface. Chat and structured replay never
  initialize xterm. Terminal replay mounts xterm inside its host without replacing
  the replay toolbar. Route/socket guards reject late responses from old views;
  replacing an overlay resolves cancellation and removes its Escape handler.

The reducer merges `session.config`, `user.echo`, assistant messages, tool cards,
permissions, turn and error state; optimistic user turns are de-duplicated against
the canonical `user.echo`, and streaming deltas are not re-rendered as a duplicate
final result. Live and restored events go through the same reducer.

## 6. Configuration and deployment

`config.py` loads from environment/`.env`; `python -m server` starts Uvicorn.
The recommended small deployment keeps Uvicorn on `127.0.0.1:8077` behind
Tailscale Serve for Tailnet HTTPS/WSS; the app does not terminate TLS itself, and
Funnel / direct public exposure is out of scope. `/ws/term` validates Origin;
`/ws/devbox` accepts a bearer token only via the `Authorization` header. Health
endpoints: `GET /api/health` (liveness + protocol) and `GET /api/ready` (also
checks the database and recording data directory). See
[`remote-deployment.md`](remote-deployment.md) and [`operations.md`](operations.md).

## 7. Testing

Server, connector, security, and persistence suites live in `tests/` and run with
pytest; pure helpers and actual app orchestration run with `node --test`.

```bat
:: Python suites
.venv\Scripts\python -m pytest -q
:: A single suite
.venv\Scripts\python -m pytest tests\test_server_recording.py -q

:: Browser (node:test) suites
node --test web\ui.test.js web\chat.test.js web\replay.test.js web\collaboration.test.js web\app.test.js
```

Representative coverage:

| Area | Tests |
|---|---|
| End-to-end lifecycle | `test_agent_lifecycle.py`, `test_hub.py`, `test_onboarding.py` |
| Surface selection and shared-session security | `test_session_surfaces.py`, `web/app.test.js` |
| PTY lifecycle (including isolated Windows children) | `test_pty_session.py` |
| Recording / replay / retention | `test_server_recording.py`, `test_persistence.py` |
| Connector transport split | `test_connector_supervisor.py`, `test_connector_transport.py`, `test_connector_ipc.py` |
| Durable spool | `test_connector_spool.py` |
| Runtimes / probe / structured chat | `test_connector_runtimes.py`, `test_runtime_probe.py`, `test_copilot_session.py`, `test_agent_session.py` |
| Projects & skills | `test_devbox_projects.py`, `test_devbox_skills.py`, `test_skills.py`, `test_local_store.py`, `test_project_watcher.py` |
| Identity / workspaces | `test_identity.py`, `test_collaboration.py`, `test_collaboration_routes.py`, `test_workspace_invitations.py`, `test_microsoft_auth_routes.py` |
| DB / migrations / pragmas | `test_models_migration.py`, `test_db_pragmas.py` |
| Ops | `test_backup.py`, `test_capacity.py`, `test_smoke.py`, `test_version.py`, `test_logging.py` |
| Security / config | `test_security.py`, `test_security_integration.py`, `test_config.py`, `test_password_hashing.py` |
| Browser logic | `web/ui.test.js`, `web/chat.test.js`, `web/replay.test.js`, `web/collaboration.test.js` |

## 8. Current boundaries

- The in-memory `Hub` / `LiveRegistry` have single-server-instance semantics;
  horizontal scale-out needs shared active-connection and live-screen state.
- The app itself does not terminate TLS; a deployment front end such as Azure App
  Service or Tailscale Serve must provide HTTPS/WSS.
- Two-process supervisor/transport (real ConPTY / Windows service durability)
  passes automated simulations but still needs manual on-hardware verification
  before being treated as production-proven.
