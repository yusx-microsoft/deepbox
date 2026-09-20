# AgentBridge implementation review and release boundary

## Status and release boundary

The user has explicitly authorized deploying this current AgentBridge workbench
to the existing Azure app `deepbox-webdata-du`. This authorization is for the
implemented workbench, not for any of the 100 separate static design candidates
and not for external repository, resource or domain migration. Actual rollout
must be verified through deployment status, readiness, version and source bytes.

The completed `1fab322` maintenance release is historical context. Its
[review is preserved at that revision](https://github.com/yusx-microsoft/deepbox/blob/1fab322/docs/review.md);
do not apply its pass counts to this redesign. [Planning](planning.md#3-validation-status)
separates the baseline from current work. Deployment does not authorize setting up,
connecting or upgrading real user machines or agents during validation.

## Review scope

- AgentBridge is a bridge for humans/teams to centrally manage agents across many
  devices and share them through Workspaces. Fixture names such as Builder and
  Reviewer are mock data, not built-in agent-to-agent roles or an orchestration goal.
  Fleet scale and simultaneous visible-pane count must not be conflated.

- Display name is **AgentBridge** (`DISPLAY_NAME`), including the FastAPI title.
  `agentbridge` (`NAME`) stays the lowercase CLI/package/service/log identifier,
  with `AGENTBRIDGE_*` environment keys. The `deepbox` alias, legacy configuration,
  roots, cookies, hashes, and IPC identities remain compatible; no identity migration.
- Small browser modules replace the singleton controller: shell/API/dialogs and
  management compose a user-controlled split workbench with independent panes.
- Return to the pre-tmux workbench: refined top navigation, compact collapsible
  sidebar, flexible split panes, sans-serif UI and monospace code/data, subtle
  borders/spacing, and light/dark themes. No green status bar or forced full-screen
  TUI. Optional tmux shortcuts are opt-in, default off; xterm stays terminal-only.
- Existing FastAPI authorization, keyboard lease, durability, recording, and the
  connector's provider registry remain; no server micro-framework rewrite.

Read [implementation](implementation.md#5-web-web) for actual module ownership and
[AgentBridge](agentbridge.md) for environment/home precedence and migration gates.
Technical proposals elsewhere are not evidence that this draft has been accepted.

## Acceptance checklist (pending)

### Visual and interaction review

- [ ] Inspect both themes and narrow/wide viewports: refined top navigation,
  compact collapsible sidebar, quiet pane controls, uncluttered transcripts,
  restrained sans-serif UI and monospace data, subtle borders and spacing.
  No green tmux status bar, forced full-screen TUI, bubbles, or remote fonts.
- [ ] Confirm optional tmux navigation is off by default and Control+B is untouched.
  With explicit opt-in, check Ctrl+B `%`, `"`, arrows, pane digits, `z`, `x`, `w`,
  `?`, and `:`; disabling clears prefix state. Double Ctrl+B sends exactly one
  literal byte only to an owned live terminal; native Ctrl+C still works. UI command
  text is never evaluated as shell/model input. Visible controls work with mode off.
- [ ] Check **AgentBridge** in display/title surfaces and lowercase `agentbridge`
  in CLI/package/service/log identifiers. Do not rename URLs, env keys, auth, or storage.
- [ ] Verify local helper load order and chat boot with CDN requests blocked.
  Exercise terminal asset failure/retry with a stub: visible failure, no premature
  session create, and no xterm initialization for chat/structured replay. Existing
  pinned xterm is still a runtime CDN dependency, not newly vendored assets.
- [ ] Split right/below up to four panes; reject a fifth. Drag row/column dividers,
  use axis arrow keys and Home, select/maximize/restore, and verify keyboard focus,
  accessible separator values, ratio persistence, and terminal resize notification.
- [ ] Close one pane while other panes are active. Only its socket/listeners/timers
  detach; the backend session and other pane sockets/chat/replay continue.

### State, permission, and lifecycle review

- [ ] Reload saved layouts under the same user/workspace; then switch workspace,
  sign out/in as another user, and check isolation. Inspect saved JSON: geometry,
  selection, and target IDs/surface/kind only; no messages/files/tokens/roles or
  `forceNew`. Missing/deleted/ended saved live targets must not create sessions.
- [ ] Exercise two structured chats, terminal + chat, and live + replay concurrently.
  Reconnect, history loads, file reads, or responses from closed/replaced panes
  must not render into or mutate the current pane/workspace.
- [ ] Keep distinct unsent drafts while focusing/resizing/splitting panes; no draft
  crosses panes or enters saved layout JSON. New chat must not replace another pane's
  draft or end the previous shared session.
- [ ] Explicit Terminal reuses only a known live terminal, never Chat/unknown
  surface. New chat and New session preserve old shared sessions; replay never
  becomes a live create. End session remains separate and confirmed.
- [ ] Operator/Admin/Owner can send structured input without keyboard ownership;
  Viewer cannot. Terminal input/resize/termination remain holder-only, including
  for an Admin/Owner. Structured termination requires current Operator/Admin/Owner
  access without a lease. Check backend denial as well as UI affordances.
- [ ] Replace/close dialogs and switch user/workspace during asynchronous management
  requests. Stale results must not mutate the new context. Role changes require
  explicit Save; there are no automatic grants or Viewer promotions. One-time
  tokens stay in modal memory/DOM, not URLs/logs/storage.
- [ ] Check canonical/legacy/mixed/explicit-empty environments, `.deepbox`
  continuity, custom HOME overrides, and both CLI names using isolated fixtures.
  Preserve cookies, tokens, registrations, IPC identities, `/api/devboxes`, and
  the actual `session` table. No automatic migration or live setup.

## Local verification commands

Run against isolated test fixtures from the existing checkout. The current local
results are recorded below; user visual/code acceptance is still pending:

```bat
cd /d C:\Code\deepbox && .venv\Scripts\python -m pytest -q
cd /d C:\Code\deepbox && node --test web/*.test.js
cd /d C:\Code\deepbox && for %F in (web\*.js) do @node --check "%F"
cd /d C:\Code\deepbox && git -c core.whitespace=blank-at-eol,blank-at-eof,space-before-tab,cr-at-eol diff --check
```

The installed Node supports the `web/*.test.js` glob in CMD, including the new
tmux, pane, dialog and workbench suites. Re-enumerate the files explicitly when
using older Node versions without glob support. In a `.cmd` file, double the
syntax-check loop variable (`%%F`). Do not
substitute the historical four helper suites for the new pane/layout/management/
workbench coverage. Do not opt into real-runtime or deployed-workspace checks.

| Evidence | Current record |
|---|---|
| Integrated Python suite results | 701 passed, 4 platform/permission skips, 17 existing warnings; 16 subtests passed |
| Browser suites | 232 passed, including default-off/opt-in shortcuts, compact headers, focus/drafts, transcript and restore |
| Syntax / bytes | 34 Python modules compiled; all JS syntax, UTF-8/BOM/EOL and CR-aware whitespace checks passed |
| Isolated browser integration | AgentBridge display spelling, restored navigation, split/overflow actions, fresh sessions, drafts/resize/zoom, reload without new sessions, native Ctrl+B by default, explicit tmux toggle, four-pane cap, close without ending, dark/light/narrow layouts passed |
| User visual/code review | Pending |
| Live multi-machine/real-runtime acceptance | Not performed for this draft |
| Deployment/external rename | Not performed; separate approval required |

**Historical iteration only:** the earlier tmux-shell pass recorded 699 Python
and 227 Node passes, plus isolated browser/keyboard checks with external requests
blocked and a terminal renderer stub. Those results do not validate this restored
workbench or its default-off shortcuts, and were not real xterm/model-CLI acceptance.
Final integration owns the current evidence; do not carry those counts forward.

The current browser run likewise used isolated data and a simulated connector/
terminal renderer, with no page errors or external requests. It did not operate a
real agent or validate a real model login or CDN connection.

Local bootstrap/account invitation and Microsoft-only login/logout contracts remain
unchanged. Catalog refresh revalidates open-pane controls, including recording permissions.
Session role frames never bypass the current workspace permission check.

## Remaining decisions

Resolve the checklist, record the final local evidence, and obtain explicit user
acceptance before deciding whether to publish or release. On-hardware supervisor
soak, network-churn collaboration, and production rollout checks remain separate
manual work; old simulation or release results do not certify this cut.

**Final external rename is a separate approval step:** the existing GitHub repo
URLs, `C:\Code\deepbox` path, Azure resource/domain, Entra callbacks, and installed
data/identities are deliberately unchanged. See the [migration gates](agentbridge.md#migration-gates)
for the required continuity/rollback plan. Local UI/name acceptance does not
authorize repository/resource/domain migration, deployment, or live setup.
