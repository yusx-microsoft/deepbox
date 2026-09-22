# AgentBridge: repository, installation and compatibility contract

**Scope: repository and installer naming; no cloud deployment is implied.**
Release verification is separate from ongoing visual exploration. Any authorized
deployment targets the existing `deepbox-webdata-du` app. Repository and installer branding use AgentBridge;
Azure resources/domains and installed data are not migrated. See the deployment
status and `/api/version` for what is live; neither a repository rename nor an
unmerged PR establishes publication or deployment.

This guide owns the rename contract. See [implementation](implementation.md#5-web-web)
for the current module map, [product design](product-design.md) for interaction
intent, and [review](review.md) for checks and release evidence.

## Canonical repository and installer source

- **Only upstream / production source:**
  [yusx-swapp/AgentBridge](https://github.com/yusx-swapp/AgentBridge).
  GitHub display/case is `AgentBridge`; CLI and package names remain `agentbridge`.
- **Fork only:** `yusx-microsoft/AgentBridge` is not the main repository, an
  installer mirror, or a production fallback.
- **Windows installer:**
  `https://raw.githubusercontent.com/yusx-swapp/AgentBridge/main/scripts/install.ps1`.
- **macOS/Linux installer:**
  `https://raw.githubusercontent.com/yusx-swapp/AgentBridge/main/scripts/install.sh`.
- **Default payload:**
  `https://github.com/yusx-swapp/AgentBridge/archive/refs/heads/main.zip`.

Point `upstream` at `https://github.com/yusx-swapp/AgentBridge.git`. Develop on a
feature branch created from canonical `upstream/main`, and submit its PR to
**yusx-swapp/AgentBridge:main**; do not develop directly on `main`. A fork can remain
`origin`, but it is not the production source. Keep the currently running worktree
at `C:\Code\deepbox`; a remote rename does not require moving local files.

The canonical URLs describe the requested repository/installation contract, not
proof that the remote rename, PR merge or new scripts are already published.
Verify the repository route, raw script contents and source archive on `main`
before recommending fresh installs. See [publication checks](install.md#hosting-the-installer-scripts)
and [`SOURCE_ZIP` 404 troubleshooting](install.md#source_zip-http-404), including an
explicit reviewed archive override. No cloud or authentication rename is implied.

## Product boundary

AgentBridge centralizes **human and teammate access to agents running on many
devices**. A user managing, for example, 100 agents should not need to operate
100 separate host consoles. Device/agent inventory, find/filter/status, sessions,
and Workspace membership are the primary workflows. Workspaces are shared access
boundaries; agent-to-agent conversation or a Builder/Reviewer team is not the
default product model. Such names in fixtures or static design candidates are
mock content, not built-in agent roles or automatic orchestration.

The four-pane limit concerns concurrent viewing, not fleet size. A production
100-agent/device-scale validation is still distinct from the local pane tests.

## A smaller boundary, not a new framework

**AgentBridge** is the display name (`DISPLAY_NAME`), including the UI and FastAPI
title. **`agentbridge`** remains the lowercase machine name (`NAME`): Python package,
CLI command, service/log namespace, and the basis for `AGENTBRIDGE_*` environment
keys. `LEGACY_NAME = "deepbox"` and the `deepbox` CLI alias remain compatible.
`agentbridge/product.py` centralizes this boundary and configuration/home lookup;
capitalizing the display name is not a crypto, auth, or identity migration.
The server remains FastAPI + SQLite with its existing role checks, keyboard
leases, Hub, recording and durable spool protocol. The connector's provider
registry remains the authority for runtime capabilities; there is no server
micro-framework rewrite or new runtime-specific browser dispatch.

The browser returns to the pre-tmux split workbench: refined top navigation,
a compact collapsible sidebar, flexible panes, and restrained sans-serif UI type
with monospace code/data. Subtle borders and spacing support light/dark themes;
no green tmux status bar or forced full-screen TUI. Small local helpers separate
shell/management from the split tree and each pane's connection and chat/terminal/
history lifecycle. Saved history displays its final content immediately, read-only;
the recording player and recording-management toolbar are removed, not the stored
recordings, recovery pipeline, or explicit Resume. The four-pane cap, independent
in-memory drafts, safe restore, and existing roles remain. Closing only detaches; layout preferences are not
conversation, draft, or permission storage. See the [pane acceptance checklist](review.md#acceptance-checklist-pending).

The existing pinned jsDelivr xterm dependency remains terminal-only and on demand;
chat/app boot does not wait for it. No remote fonts or new vendor downloads were
introduced. This is not a vendored or fully offline terminal distribution.

## Optional tmux-style interaction

Tmux-style keyboard/navigation functions are **opt-in, default off**. They are a
convenience layer, not the primary navigation or default visual shell. Visible top
navigation, sidebar, and pane controls work without them. **Control+B must not be
intercepted unless the user enables this mode**; disabling it clears pending prefix
state. The optional tree/command picker does not replace the workbench chrome.

Only after enabling the optional mode, press **Ctrl+B**, release it, then:

| Key | UI action |
|---|---|
| `%` / `"` | Split right / below |
| Arrow keys | Focus the geometrically adjacent pane, also while zoomed |
| `o` / `O`, `0…3` | Next / previous pane, or select its displayed index |
| `z` / `x` | Zoom/restore / close the view without ending its agent |
| `w` / `s` | Agent/machine/pane tree / workspace picker |
| `c`, `h`, `r` | New session, history, reconnect |
| `m`, `?` | Menu, key help |
| `:` | UI command prompt |
| Ctrl+B again | Forward one literal `\u0002` to an owned live terminal only |
| Esc | Cancel prefix or command prompt |

The command prompt is deliberately **not a shell**. It accepts a small allowlist
such as `split-window -h`, `split-window -v`, `select-pane -t 2`, `resize-pane -Z`,
`choose-tree`, `close-pane`, `request-keyboard`, `interrupt`, `end-session`, and
`theme dark|light`; `help` lists the supported forms. Unknown/OS commands are rejected
and never sent to an agent. `end-session` retains permission checks and confirmation.

Even when opted in, prefix handling is disabled in login/management dialogs and
during IME composition.
Outside prefix mode native editing/terminal keys are not intercepted. There is no
saved command history, message content, or credential in layout storage. This is a
browser interaction model inspired by tmux, not an embedded tmux server.

## Environment and home compatibility

`agentbridge.product.env(stem, default=None)` looks up values by **presence**, not
truthiness. New callers use an unprefixed uppercase stem such as `SERVER_URL`;
full `AGENTBRIDGE_*` and `DEEPBOX_*` keys are also accepted by this helper.

| Canonical key | Legacy key | Helper result |
|---|---|---|
| Present, nonempty | Any | Canonical value |
| Present, empty string | Any | Empty string; never reveals the legacy value |
| Absent | Present, including empty | Legacy value |
| Absent | Absent | Supplied default (otherwise `None`) |

Use `AGENTBRIDGE_SERVER_URL`, `AGENTBRIDGE_TOKEN`, `AGENTBRIDGE_HOME`, and other
`AGENTBRIDGE_*` settings for new configuration. Existing `DEEPBOX_*` configuration
still works when the matching canonical key is absent. Clear/remove a key
intentionally: an empty canonical token is **not** a request to reuse a legacy
token. Individual settings still apply their own parsing, defaults, and validation;
the helper does not promise that an empty value is valid for every setting.

`agentbridge.product.local_home()` resolves the install home without moving or
copying anything:

1. Resolve `HOME` using the same canonical-then-legacy lookup. A nonempty explicit
   override wins and expands `~`; an empty or whitespace-only override raises an
   error rather than falling back to an old root or the current working directory.
2. With no override, use `~/.agentbridge` if it exists.
3. Otherwise, reuse an existing `~/.deepbox`.
4. If neither exists, select `~/.agentbridge`. If both exist, the canonical root
   wins; use an explicit HOME override to choose the other.

Existing legacy/custom installs remain supported; no automatic directory move,
copy, or re-enrollment is implied. A `.deepbox` path in existing-install output
indicates compatibility/reuse, not the fresh-install name: fresh installs use
`~/.agentbridge`. Persistent state/spool and IPC defaults keep
their independent legacy roots for identity continuity; changing install HOME is
not a data migration. See [install](install.md) for explicit, user-controlled
installation/upgrade instructions, not an instruction to execute live setup now.

## Deliberately unchanged identities

- Checkout path `C:\Code\deepbox`, Azure resources/domains (including
  `deepbox-webdata-du`), and Entra callback configuration. GitHub repository and
  installer URLs **do change** to the canonical AgentBridge paths above.
- Existing `deepbox.db` default and database schema: the table is **`session`**,
  not `sessions`. `/api/devboxes` and the Devbox domain model are not brand strings.
- Installed registrations/device IDs, local state/spool roots, recording offsets,
  URL/token-hash spool namespaces, and protocol v3.
- `deepbox_session` / `deepbox-session` cookies and signing identity,
  `deepbox_inv_` invitations, `hpc_box_` tokens/hashes, existing trusted identity
  headers, agent-control IDs, and attachment delimiters.
- `deepbox-sessiond-*` IPC, legacy runtime/socket directories, and existing
  handshake/HMAC domains. A display rename must not strand a running supervisor.
- Lowercase Python/CLI/storage identifiers and public service/log namespaces:
  `DISPLAY_NAME` is never substituted into compatibility-sensitive machine strings.

Do not mechanically replace every `deepbox` string. These identities require an
explicit migration plan if they ever change.

## Migration gates

- [ ] **Local review:** inspect the composed UI and code, run isolated automated
  suites, record integrated evidence, and resolve the [acceptance checklist](review.md).
  No earlier release sign-off carries forward.
- [ ] **Compatibility acceptance:** exercise canonical/legacy/mixed/empty settings,
  existing `.deepbox` and custom roots, and CLI aliases in isolated fixtures.
  Verify no registration, cookie, token, session, or IPC identity is rewritten.
- [ ] **Separate release decision:** only after user approval decide on a commit,
  publication, staged installation, or deployment. Nothing here authorizes those
  actions or a live connector/model run.
- [ ] **Repository and installer publication:** use the approved canonical
  `yusx-swapp/AgentBridge` naming; verify the actual remote, reviewed PR merge to
  upstream `main`, both raw scripts and their matching archive. A fork or local
  edit is not publication evidence. Do not infer completion from this checklist.
- [ ] **Cloud/domain or identity migration:** Azure resource identities, domains,
  authentication, data and IPC remain unchanged. Any future change needs its own
  explicit migration plan and approval; it is not part of the repository rename.
