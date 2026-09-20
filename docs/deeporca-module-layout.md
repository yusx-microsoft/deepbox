# DeepOrca integration modules and presentation

This follow-up reorganizes the existing v1 implementation and replaces the first
chat presentation. It does not introduce a second native WebServer, change the
wire protocol or enable interactive approvals. The later existing-profile
extension adds explicit binding without importing native chat history.

## Ownership

```text
agentbridge/integrations/deeporca/
  contract.py          # Path-free desired configuration; never imports the SDK
connector/integrations/deeporca/
  adapter.py           # Runtime descriptor registered by the platform
  probe.py             # Disposable, optional SDK capability probe
  profiles.py          # Bounded existing-profile discovery and opaque local references
  credentials.py       # Local key ownership and sealed profile credential decoding
  store.py             # Private binding/admission/native-context mappings
  supervisor.py        # Owned worker/session lifecycle extension
  worker.py            # Spawn/IPC, deadlines and bounded native event coalescing
  session.py           # Structured-session facade and settlement
  events.py            # Native event validation and canonical projection
server/app/integrations/
  policy.py            # Default control-plane policy for ordinary runtimes
  deeporca/policy.py   # DeepOrca configuration, readiness, input and control rules
web/integrations/deeporca/
  runtime.js           # Local renderer/access/input/presentation contract
  agent-ui.js          # Managed-profile creation and settings presentation
  agent-ui.css         # Scoped, responsive model-configuration form
  chat.js              # Pane-local conversation view and safe Markdown
  chat.css             # Scoped conversation styling
```

The Server retains ordinary authorization before sensitive runtime actions; it
owns HTTP/WebSocket control, workspace membership, persistence and recording.
It does not import or execute the DeepOrca SDK. Connector supervisor hooks retain
platform ownership of transport, spool/ACK, sequence and session bookkeeping;
the DeepOrca extension owns its implementation details. Optional probe and
adapter registration do not make the SDK a platform import dependency.

In the browser, `chat.js`, `pane.js` and `management.js` retain generic event,
transport, pane/modal lifetime, permission and stale-context handling. Runtime
contracts and the existing local-module allowlist select native behavior and
presentation. Connector metadata cannot supply executable script URLs.

Internal Python import paths changed. On a source checkout, restart idle Server
and Connector processes to load the new packages; an already-running process
does not hot-reload Python modules. **No state migration or deletion is needed.**
Binding database names/schema, managed homes/profile names, enrollment scopes,
session/receipt identifiers and `deeporca-chat-v1` are unchanged. Browser assets
use the new local paths; refresh the page to load the complete matching bundle.

## Native security policy boundary

Native DeepOrca defaults to `minimal`. The Connector calls `ensure_profile()`
without a security-default override; the SDK copies native defaults or trusted
template security unchanged. Explicit profile security is respected. Embedded
execution delegates to the unmodified native `SecurityManager`, with no forced
`standard` level or `allowlist` approval floor.

There is no security migration, new-profile-only opt-in, creation marker, extra
Connector flag or separately versioned security-default API. Existing files are
not proactively rewritten, but existing managed and bound profiles using `minimal`
now also receive that native policy at runtime rather than the former floor.
`NonInteractivePolicy` still rejects final `REVIEW`/`DENY` and unsupported
background/autonomous tools; no approval UI or transport is added. The
[existing-profile binding](deeporca-existing-profiles.md) native-stop requirement
and exclusive embedded ownership checks are unchanged.

## Native-style chat

The reference is the native repository's `deeporca/chat.html` conversation
styling and `deeporca/webui/js/conversation-view.js` view-state behavior. The
adaptation uses local DOM construction, not native application globals or its
standalone network client:

- A readable conversation column is anchored inside its own pane; the composer
  shares its width. Splitting panes or opening a narrow mobile view cannot
  inherit an unrelated centered generic-chat margin.
- User messages have a subtle surface; assistant prose is uncluttered. Adjacent
  tool calls form a compact group with action/target summaries and folded detail,
  rather than large colored success blocks.
- Thinking is folded and retains its disclosure state during streaming.
  Ordered and nested lists, tables, links, blockquotes and fenced code use safe
  DOM rendering. Source HTML is text, unsafe URL schemes are inactive, and code
  copying does not execute content. No remote fonts or renderer libraries load.
- Usage/model/status metadata is unobtrusive and collapsible. Real canonical
  `status` frames and native terminal usage are preserved, not just browser-only
  pseudo-tool fixtures.
- Error, interruption, uncertain or missing tool results and truncated previews
  remain explicit. An access-only disconnect redraws the view immediately;
  unfinished work is not labeled Running/Working while the view is offline.
- Only automatic managed-profile creation is supported in v1, so its UI is a
  clear explanation instead of a misleading one-option profile dropdown.

The Markdown renderer is a safe local subset, not a wholesale copy of the native
application. Arbitrary HTML, remote embeds and native autonomous/approval controls
are deliberately not enabled.

## Visual and behavioral evidence

The images below come from the actual workbench under Playwright with fixture
REST/WebSocket data. They demonstrate presentation, not model authentication or
real tool execution; the SDK/Server/Connector tests are separate.

- [Desktop](prototypes/deeporca-workbench-v2-desktop.png)
- [Independent native and CLI panes](prototypes/deeporca-workbench-v2-split.png)
- [Mobile conversation](prototypes/deeporca-workbench-v2-mobile.png)
- [Mobile nested Markdown and tables](prototypes/deeporca-workbench-v2-mobile-markdown.png)

See [validation](deeporca-validation.md) for test results and
[acceptance mapping](deeporca-design-checklist.md) for the original v1 guarantees.

The subsequent [web profile configuration](deeporca-profile-configuration.md)
extension adds DeepOrca-only model setup/editing, sealed credentials and
revisioned configuration updates without changing these ownership boundaries.
