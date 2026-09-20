# DeepOrca Library Integration for DeepBox

**Status:** v1 implemented on `feat/deeporca-library-integration` (DeepBox) and `feat/deepbox-library-host` (DeepOrca). This is the architectural design, not a production deployment claim. See [operational setup](deeporca.md) and the implementation reconciliation below.

**Scope decision:** Interactive tool approval is **not implemented in v1**. This includes approval prompts, approval buttons, approval request/response transport, and approval waiting states for the DeepOrca backend. Existing approval behavior for other runtimes is unchanged.

**Naming:** This document uses **DeepBox** for the host product and repository. The current command-line package and user-facing commands use **`agentbridge`**; the integration does not introduce another rename.

**Configuration extension:** [Web profile configuration](deeporca.md#web-profile-configuration)
supersedes the original template-only onboarding sections below. Add Agent and
Agent settings now configure endpoint/model/context window/reasoning effort.
The browser seals API keys to a Connector public key; only ciphertext is stored
on the Server. Plaintext credentials and native configuration writes remain on
the Connector. Profile/project/template identity stays immutable; LLM settings
have separately revisioned, idle-only updates.

**Reviewed baseline:** DeepBox `50699fa` and DeepOrca `dd6670d`, including the local working trees. The baseline analysis and examples explicitly marked *proposed* describe the design phase; the implementation map below identifies the shipped branch interfaces.

### Implementation reconciliation

- The public SDK is `deeporca.embedded`, with `EMBEDDED_API_VERSION = 1`, `ensure_profile(...)`, and `EmbeddedRuntime`. The Connector probes it in a disposable interpreter and hosts each Agent in its own spawned process; it does not start the native CLI or WebServer.
- Browser provisioning supports managed creation and [existing-profile binding](deeporca.md#existing-profile-binding) through Connector-advertised references. The latter explicitly requires native writers to be manually stopped; it does not claim a shared standalone/embedded lease. The only managed-create template reference is `connector-default`, resolved locally.
- Add agent, persistent **Agent settings** (name, model settings, readiness, refresh/retry), and opening a conversation are separate actions. Runtime/project/profile/template identity is immutable. The Server tracks desired/observed revisions; initialization and native data stay local.
- `connector/integrations/deeporca/` owns the probe, adapter, store, supervisor extension, worker, session and event projection. It implements enrollment isolation, durable admission/receipts, native session mapping, lifecycle, and canonical newline-terminated event records. A private per-spool ownership pin refuses unsafe enrollment changes while old output/controls/work remain. The inherited default spool filename still includes the token; token rotation preserves native identity but is not automatic spool migration. A forced stop or crash is uncertain, not an exactly-once execution or rollback guarantee.
- Output admission stops at 64 MiB / 10,000 pending frames, with bounded failure-record headroom. Affected native turns settle as uncertain rather than continuing with unrecorded success. Existing frames are not discarded. Full-disk failure can also prevent an emergency record; this is not a SQLite/WAL disk-quota guarantee.
- Compatible incremental text/thinking is coalesced within a 12ms / 16KiB window. Native identity and ordering are preserved, boundaries flush first, original-event bytes still count toward the turn budget, and the owned deadline task is joined before terminal results. A cancelled or failed callback, including cancellation while waiting for the writer lock, cannot silently become successful settlement.
- Inactive native conversations have an explicit operator-only **Continue native conversation** history action. Replay and layout restore never persist or imply continuation consent. The SDK's private context manifest rejects unrecoverable missing native context instead of silently creating an empty conversation; native history, not UI history, is the recovery source.
- `web/integrations/deeporca/` owns the pane-local renderer and runtime UI selected by `deeporca-chat-v1`. It uses the existing reducer, socket, recording and restore paths. Tools are correlated by turn plus provider tool ID; incomplete outcomes and truncated display previews are labeled. Other runtimes retain their existing renderer and approval behavior.
- Unknown renderer IDs produce a visible generic-fallback notice, never a descriptor-supplied script load. Inactive/replayed unfinished tools are labeled as having no recorded result rather than still running.
- Native DeepOrca defaults to `minimal`; `ensure_profile()` copies native defaults or trusted template security unchanged, and explicit profile security is respected. Embedded execution delegates to the unmodified native `SecurityManager` with no forced `standard`/`allowlist` floor for any profile mode. Existing files are not proactively rewritten, but existing `minimal` profiles are also honored at runtime. There is no security migration, creation opt-in/marker, extra Connector flag or separately versioned security-default API. Final `REVIEW` and `DENY` are refused, and unsupported background/autonomous tools stay unavailable. Binding and exclusivity requirements are unchanged. See [the exact security scope](deeporca.md#security-policy-for-all-profiles).
- The SDK owns a minimal bootstrap built from native model/configuration, tool, skill, memory and persona components, plus `SessionManager`, `TurnEngine`, and `SessionRunCoordinator`; it does not initialize the standalone gateway or autonomous services. Provider failure events now settle as errors rather than successful completed turns. Embedded registries wait for synchronous-thread cleanup on cancellation, bounded externally by worker retirement; standalone defaults are unchanged. No model request is sent merely to advertise readiness.
- Verification includes real SDK + local fake OpenAI/SSE provider tests, real Server/Connector/WebSocket/recording integration, explicitly paused existing-profile binding, native-context recovery after worker restart, and Chromium workbench checks. Browser fixtures are not real-provider or deployment evidence. External provider authentication, production deployment, concurrent standalone/embedded writers, uploads, and autonomous execution remain outside this verification.

Implementation and test entry points: [operations and test commands](deeporca.md), `tests/test_deeporca_integration.py`, `tests/test_deeporca_browser.py`, and the consolidated Connector/Server unit and regression suites. The optional SDK/E2E tests require a local source checkout and use temporary profiles and loopback-only fake providers.

See the [v1 acceptance checklist](#design-to-implementation-checklist) for requirement-to-code/test mapping and the [validation report](#validation-record) for actual run results, commands and verification limits.

The [module and presentation follow-up](#module-boundaries-and-presentation) documents the
dedicated integration packages, platform hooks, native-WebUI-inspired layout and
visual evidence. It preserves the v1 wire, persistence and security boundaries.

---

## 1. Summary

Run DeepOrca as a Python library on the user's Connector machine. Keep DeepBox responsible for identity, workspaces, machine enrollment, session routing, durable display events, and browser delivery. Render DeepOrca conversations inside the existing DeepBox workspace rather than embedding a second web application.

The main integration components are:

1. A **DeepOrca runtime capability** advertised by the Connector.
2. An extension to **Add agent** for managed creation or explicitly paused native-profile binding.
3. A Connector-side **library session backend**, using an isolated Python worker.
4. An **event adapter** that preserves DeepOrca semantics within DeepBox's existing structured output channel.
5. An embeddable **DeepOrca-style chat renderer**, driven by the existing DeepBox transport.

The implementation must not invoke the DeepOrca CLI to run conversations, require a DeepOrca WebServer, place model credentials on the DeepBox Server, or reinterpret a display transcript as native model context.

### 1.1 Decisions at a glance

| Area | v1 decision |
| --- | --- |
| User entry point | Workspace → Machine → **Add agent** → **DeepOrca** |
| Runtime ID | `deeporca`; backend is a Python library, not a CLI command |
| Execution location | Connector machine |
| Isolation | One library worker per independent Agent binding |
| Chat surface | Existing `structured` surface |
| Renderer | `deeporca-chat-v1`, selected from a local allowlist |
| Agent configuration | Create a managed profile or bind an advertised existing profile with native writers paused |
| Model configuration | DeepOrca-only browser form; sealed credentials, Connector-local SDK writes under profile ownership; optional local template defaults |
| Interactive approval | Out of scope; approval-required calls fail immediately |
| Concurrency | One active human turn per Agent worker in v1; other sessions may be viewed concurrently |
| Browser disconnect | Detach the viewer; do not automatically cancel or resubmit the turn |
| Display recovery | DeepBox event log |
| Model context recovery | DeepOrca `SessionManager` |
| Installation | Explicit local setup; creating an Agent never installs packages automatically |

## 2. Goals and non-goals

### Goals

- Let a user create a usable DeepOrca Agent from the existing DeepBox management flow.
- Support persistent conversations, streamed text, exposed thinking/status events, local tools, and locally configured skills and memory.
- Preserve DeepBox's workspace and session authorization rules.
- Support stop/cancel, page refresh, viewer reattachment, and Connector transport reconnect.
- Make runtime readiness, policy refusals, and initialization failures understandable.
- Reuse DeepOrca's chat appearance and interactions without importing its page-level networking or global state.
- Keep package paths, project paths, credentials, and native profile storage under Connector control.

### Non-goals for v1

- Interactive tool approval, including an "Allow once" or "Always allow" UI.
- Automatic installation or upgrading of DeepOrca from a browser action.
- A standalone DeepOrca HTTP/WebSocket service or an iframe integration.
- An OS sandbox: process isolation does not provide filesystem or network confinement.
- Concurrent turns within the same Agent worker.
- Scheduled jobs, channels, autonomous goal loops, or detached child tasks without an explicit ownership and shutdown contract.
- Full WebUI settings parity, image uploads, and generated-file browsing. These can follow the text/tool integration through authenticated file APIs.
- Exactly-once execution of arbitrary external tool side effects after a process crash.

## 3. Existing capabilities and gaps

### 3.1 DeepBox

The current path is:

```text
Browser
  → DeepBox Server
  → Connector transport
  → Supervisor
  → PTY or StructuredAgentSession
```

- `web/management.js::createAgent()` already implements the Add agent form.
- `POST /api/devboxes/{devbox_id}/agents` creates an Agent record, validates its local project association, and pushes the Agent directory. It currently does **not** initialize a DeepOrca profile.
- `connector/runtimes.py` and the Supervisor's session creation path primarily describe executable/CLI runtimes.
- `connector/agent_session.py` emits canonical structured events such as `user.echo`, `message.delta`, `tool.call`, `tool.result`, and `turn.end`.
- Structured output already uses `kind=event`, durable spooling, server recording, and `event_restore`.
- `web/chat.js` separates state reduction from DOM rendering; `web/pane.js` owns transport integration and pane lifecycle.
- The server accepts and forwards `interrupt`, but the reviewed Supervisor dispatcher has no corresponding cancellation branch. This must be completed for the new backend.
- Local project inventory deliberately excludes absolute paths. The new binding design must preserve that boundary.

### 3.2 DeepOrca

Existing library entry points include:

| API | Existing responsibility |
| --- | --- |
| `run_agent(...)` | Low-level asynchronous agent event stream |
| `ainit_deeporca()` | Initialize the selected native profile, tools, memory, skills, and background client setup |
| `TurnEngine(llm, tools, system_prompt, ...)` | Construct the turn executor |
| `TurnEngine.execute(request, sink, *, session_manager=None, compaction_state=None)` | Resolve and lock the native session, execute the turn, save context, and close the sink |
| `SessionRunCoordinator` | Track and cancel session execution tasks |
| `SessionManager(chat_dir=...)` | Native conversation storage and session locks |
| `to_wire_dict(event)` | Translate typed events to the legacy DeepOrca WebUI event shape |
| `ToolRegistry.set_security(manager)` | Install tool security behavior |

Important limitations:

- `ainit_deeporca()` takes no configuration arguments. Initialization selects the profile through process-level context and loads its `.env` with `override=True`.
- Initialization requires an existing `.state.json`; a missing profile raises `SystemExit`.
- The task registry is a process-wide singleton. Filtered/snapshot tool registries share security and MCP references.
- Native profile creation currently lives in the private CLI helper `_create_agent()`, which prints, exits, and writes directly into the profile directory.
- There is no existing `DeepOrcaCore.create_turn_engine()` factory. The DeepOrca server constructs the engine explicitly.
- Omitting `session_manager` from `execute()` selects an ephemeral path without the same locking or persistence behavior.
- `to_wire_dict()` drops part of the typed event envelope. It is not a complete persistent identity schema.
- The reviewed tool event model does not provide sufficient stable call identity for every concurrent/nested tool rendering case.
- `TurnPolicy.approval_mode` exists as a model field, but setting it alone does not enforce non-interactive security in the reviewed execution path.

The embedding design must address these gaps rather than assume a fully isolated, production-ready SDK already exists.

## 4. User experience: creating an Agent

### 4.1 Prerequisites

The user has a workspace, an enrolled online machine, and at least one Connector-local project. The machine's runtime inventory reports whether the supported DeepOrca package is available.

- No machine: show **Connect a machine**.
- No local project: explain local project registration using `agentbridge project add`.
- DeepOrca unavailable: show a machine-specific setup message. Do not offer a misleading ready state or an automatic installation button.
- Model configuration unavailable: allow an Agent to be recorded as `needs_configuration`, but do not label it ready to chat.

### 4.2 Add agent

Extend the existing machine menu instead of introducing a separate DeepOrca management application:

```text
Add agent

Machine          WIN-DEV01
Name             Project assistant
Runtime          DeepOrca — Python library
Local project    deepbox

Native profile
  (•) Create a new DeepOrca profile
  ( ) Bind an existing registered profile

Model setup      Connector-default configuration template

Tools follow the machine's local execution policy.
Interactive tool confirmation is not available in this release.

                                  [Cancel] [Create agent]
```

Do not ask for an API key, arbitrary import path, Python executable, package checkout, or raw working directory in the ordinary Agent form. These belong to machine-side setup. Do not expose an approval-policy selector that promises a workflow v1 does not implement.

Isolation is fixed to the library worker in v1. A same-process option may be introduced only after the shared-state boundaries have been refactored and tested.

### 4.3 Create versus bind

**Extension note:** the original shared-lock requirement below remains a stronger
future ownership model, not a claim about current standalone entrypoints. The
implemented [limited binding workflow](deeporca.md#existing-profile-binding) instead
requires an explicit native-paused operational precondition. It uses SDK existing
mode, preserves configuration and does not import old native chats.

**Create new** is the default:

- Generate an internal profile identifier from the binding identity, not the editable display name.
- Initialize separate persona, memory, skills, configuration, and native chat storage.
- Apply a locally registered configuration template. A template may reference local secrets; its contents and secrets do not pass through DeepBox.
- If no usable template is available, create the profile in `needs_configuration` and explain the local setup step.

**Bind existing** is explicit:

- Select an opaque profile reference from a Connector-published, administratively registered inventory.
- Verify that it belongs to this machine and is compatible with the selected project and execution policy.
- Do not overwrite persona, memory, credentials, security configuration, or existing chats.
- Reject a conflicting live binding in v1. The same mutable profile must not silently be driven by two independent workers or an unrelated standalone process.

Existing-profile binding uses an explicit manual exclusivity contract: the operator stops native writers and keeps them stopped while the Connector owns the profile. Embedded workers use the SDK profile lock, and the SDK refuses a known live native PID. Connector bookkeeping cannot fence all unrelated native writers; a shared native/embedded lease or automatic takeover is not claimed or required for this MVP.

### 4.4 Success and failure states

```text
created → provisioning → ready
                      ↘ needs_configuration
                      ↘ error
```

Machine connectivity is separate from provisioning state. An offline machine is not proof that the profile is missing, and a created directory is not proof that the model is usable.

On success, add the Agent to the normal machine tree and open an empty chat view:

```text
Workspace
  WIN-DEV01
    Project assistant                 DeepOrca · Ready
      New conversation
      Conversation history
```

Creating another conversation creates a native **session**, not another Agent profile.

## 5. Architecture and responsibility boundaries

```text
DeepBox browser
  Management UI       DeepOrcaChatRenderer
       │                 │ input / interrupt
       └────────┬────────┘
                ▼
DeepBox Server
  Authorization · Agent desired state · Session routing · Event recording
                │ existing authenticated Connector connection
                ▼
Connector transport + Supervisor
  Inventory · Binding reconciliation · Input journal · Durable output spool
                │
                ▼
DeepOrcaSession adapter                         [new]
  Admission · Task ownership · Native identity · Event normalization
                │ bounded, private local IPC
                ▼
Isolated library worker                         [new]
  DeepOrca core → TurnEngine → model / permitted tools / skills / memory
                    │
                    └── SessionManager → local native context

Output: library events → adapter → spool → Server → renderer
```

### 5.1 Library worker

One worker owns one Agent binding: native profile, project root, model configuration, and security context. It may hold multiple native chat sessions, but admits only one active human turn at a time in v1. Other Agents can run concurrently in separate workers.

The worker directly imports DeepOrca. Starting a Python worker is process isolation for a library integration, not a DeepOrca CLI wrapper.

- Use a spawn-based lifecycle compatible with Windows.
- Use a locally configured interpreter with an explicitly supported DeepOrca version; never accept executable/module paths from browser input.
- Set the worker's environment and working directory before importing/initializing profile-dependent modules. Do not mutate the Supervisor's process environment or working directory.
- Use dedicated private IPC with bounded, versioned JSON messages; keep stdout/stderr logs separate from control framing. No additional network listener is required.
- Do not treat Python object deserialization as an acceptable wire protocol.
- Treat process separation as state isolation, not a security sandbox.
- Catch startup failures, including missing-profile `SystemExit`, inside the worker boundary and report structured errors without terminating the Supervisor.

### 5.2 Runtime session interface

Introduce a session factory keyed by runtime backend rather than requiring every runtime to resolve an executable command.

The following interface is **proposed**, not an existing API:

```python
class RuntimeSession:
    async def start(self): ...
    async def submit_input(self, client_input_id, text, options): ...
    async def interrupt(self, turn_id=None): ...
    async def close(self, reason): ...
    def is_alive(self): ...
    def can_accept_turn(self): ...
```

Wrap existing PTY/CLI implementations where necessary; do not rewrite them as part of this integration. Preserve legacy transport identifiers such as `pty_instance_id` as opaque stream identity until a separately versioned protocol migration is justified.

There is deliberately no DeepOrca approval-response method in the v1 contract.

## 6. Agent provisioning and binding data

### 6.1 Server-side desired state

Reuse the existing Agent creation route and Agent directory propagation. Extend it with validated runtime-specific configuration and a provisioning-state projection.

Example **proposed** request shape:

```json
{
  "handle": "project-assistant",
  "display_name": "Project assistant",
  "runtime": "deeporca",
  "local_project_id": "project_opaque_id",
  "runtime_config": {
    "integration_version": 1,
    "profile": {
      "mode": "create",
      "configuration_template_ref": "connector-default"
    }
  }
}
```

For binding, the profile object instead contains `mode: "bind"` and a registered `profile_ref`. It never contains a filesystem path or secret.

The Server must enforce the existing Agent-management role requirement, validate project ownership and payload limits, reject unknown/unsafe configuration keys, and avoid logging credentials. Runtime compatibility is also validated authoritatively on the Connector.

Return a created resource with a pending provisioning state; do not hold an HTTP request open while initializing a model client or MCP connection.

### 6.2 Connector-side authoritative binding

Store the actual binding in Connector-local state. A proposed binding record contains:

| Field | Purpose |
| --- | --- |
| Enrollment namespace + Agent ID | Isolate bindings across server/devbox enrollments |
| Binding ID and desired revision | Idempotent reconciliation |
| Local project ID | Resolve the authoritative working directory locally |
| Native profile ID/path | Machine-private native state location |
| Ownership mode | Managed profile versus externally existing profile |
| Configuration/template revision | Detect incompatible changes |
| Package compatibility version | Diagnose SDK/adapter mismatches |
| Provisioning state and sanitized error | Report readiness without leaking local secrets |

The enrollment namespace must be stable and persisted. Do not key native state only by a display name or derive it from arbitrary browser paths.

### 6.3 Reconciliation, not fire-and-forget creation

The Agent directory is desired state. A Connector reconciler ensures that the binding exists and reports status with the Agent ID and revision.

1. Persist the desired Agent and provisioning identity on the Server.
2. Deliver the directory revision to the Connector.
3. Resolve the registered project and reserve the local binding identity.
4. Create or validate the native profile using an idempotent library initializer.
5. Validate configuration and initialize the worker as needed.
6. Persist the local result, then publish sanitized runtime status.

Duplicate directory delivery must not create duplicate profiles or overwrite an existing profile. If the connection drops between local initialization and acknowledgement, replaying desired state discovers and reuses the same completed binding.

Provisioning should be retryable. A partial initialization must not be mistaken for a ready profile. Extract a public initializer from `_create_agent()` that reports typed results, validates names, writes a completion marker last, and never overwrites an unrelated directory. Do not call the CLI's private exit-based function from the Supervisor.

Changing project/profile identity is not a casual in-place edit in v1; create a new binding or explicitly retire the old one first. Deleting the DeepBox Agent stops its worker and removes its binding projection, but does **not** silently delete native persona, memory, or chat files. Destructive native cleanup is a separate explicit operation.

## 7. Runtime initialization and turn execution

The embedded runtime owns:

- A fully initialized core and its client startup tasks.
- A non-interactive security wrapper installed before registry snapshots or child registries are made.
- A `SessionManager` rooted in the selected profile's chat directory.
- A session-keyed compaction-state map with explicit reset/recreation behavior.
- A run coordinator and ownership mapping from Agent/session/turn to tasks.
- A bounded event sink connected to the Supervisor's durable output path.

Mark an Agent `ready` only after required initialization succeeds. A constructed client is not proof that a provider credential is valid; report provider authentication errors on actual use. Readiness checks must not send a billable model completion. Optional MCP availability should be reported separately rather than misrepresented as complete tool readiness.

### 7.1 Existing engine API usage

This is an integration sketch. The dependencies and `deepbox_sink` are constructed by the **new adapter**, not supplied by an existing DeepOrca factory:

```python
from deeporca.turns.engine import TurnEngine
from deeporca.turns.models import (
    SessionRef, TurnInput, TurnOrigin, TurnPolicy, TurnRequest,
)

# tools already has the non-interactive security wrapper installed.
engine = TurnEngine(
    llm,
    tools,
    system_prompt,
    memory_store=memory_store,
    files_dir=files_dir,
)

request = TurnRequest(
    session=SessionRef("chat", native_session_id),
    input=TurnInput(text=user_text, message_id=client_input_id),
    origin=TurnOrigin(trigger="human", surface="deepbox"),
    policy=TurnPolicy(approval_mode="fail_fast"),
    requested_model=resolved_model,
)

result = await engine.execute(
    request,
    deepbox_sink,
    session_manager=session_manager,
    compaction_state=compaction_state,
)
```

`approval_mode="fail_fast"` expresses intent here; it is **not** the enforcement mechanism. Section 8 defines the required security enforcement.

Do not acquire the native session lock a second time around `execute()`: the engine already resolves and locks the session. Do not omit `session_manager` and assume that browser-visible history provides persistence.

### 7.2 Admission, cancellation, and shutdown

- Serialize human-turn admission at the worker boundary. Reject a second active turn with a stable `runtime_busy` reason rather than silently dropping or invisibly queuing it.
- Browser disconnect detaches the viewer. The local turn continues subject to configured budgets and spool capacity.
- `interrupt` targets the owned active turn, cancels its task through the coordinator, and waits for cleanup/native save within a locally configured grace period (proposed v1 default: 10 seconds). On success it emits a cancelled terminal event. If the deadline expires, mark the worker unhealthy, block new admissions, terminate only the owned worker/processes as needed, and emit an interrupted/uncertain outcome without claiming that native save succeeded. Stop must not wait indefinitely for an uncooperative tool.
- Cancellation must stop execution, not merely stop consuming events or hide a spinner.
- Distinguish explicit user cancellation from a wall-clock timeout, including when both mechanisms are configured.
- Cancelling one turn must not normally terminate the whole Agent worker or affect another Agent.
- On Agent retirement/worker shutdown: stop admissions, cancel owned tasks, await bounded cleanup, settle native context, close MCP and client resources, and persist buffered output before terminating the process if needed. Leave unacknowledged records in the durable spool for later delivery; an offline Server must not make orderly shutdown wait indefinitely.
- Forced termination is a last resort. Report an interrupted/uncertain state; do not imply that every subprocess or external side effect has been rolled back.

Detached/background features remain unavailable until they obey this ownership contract. A child tool or subagent must inherit the same non-interactive policy and cancellation scope; otherwise do not expose that capability in v1.

## 8. No interactive approval in v1

This is a scope boundary, not permission to bypass tool security.

DeepOrca currently evaluates tool operations as `ALLOW`, `REVIEW`, or `DENY`. v1 applies the following behavior:

| Native final verdict | Embedded behavior |
| --- | --- |
| `ALLOW` | Execute using the configured local security policy and argument checks |
| `DENY` | Return the existing policy refusal; do not execute |
| `REVIEW` | Convert to immediate refusal before the interactive branch; do not execute or wait |

An allowed command may run without a per-command browser prompt. Therefore workspace operators must be trusted to exercise the locally allowed tool capabilities. "No approval UI" does not mean all commands are allowed or that the project directory is a sandbox.

Selecting a project is not a filesystem sandbox. Native DeepOrca defaults to the broader `minimal` policy; explicit native/template/profile security configuration is respected. Existing files are not proactively rewritten, but bound and older profiles configured as `minimal` now also use that policy at runtime without an embedded `standard`/`allowlist` floor. There is no migration or versioned security opt-in. Configured native policy and the non-interactive host's unsupported-tool restrictions can still block writes or commands; readiness is not proof that every tool is executable.

### 8.1 Enforcement point

Install a delegating security adapter through the existing `ToolRegistry.set_security()` API:

```text
original SecurityManager.check_tool(name, args)
                  │
           final security verdict
                  │
     ALLOW ───────┼────── DENY
       │          │         │
   unchanged    REVIEW   unchanged
                  │
            immediate DENY
```

Map the **final** verdict returned by `SecurityManager.check_tool()`, not only the result of `ApprovalManager.check()`. The security manager can escalate an initial allowed verdict to `REVIEW` after additional checks.

Security behavior delegates to the unmodified native `SecurityManager` for every profile, honoring its configured level and approval mode. The embedded runtime does not promote `minimal` to `standard`, force `allowlist`, or branch on profile age or a creation marker. `NonInteractivePolicy` preserves final denials, refuses final review and rejects unsupported background/autonomous tools; it does not override a denied call to make it succeed. The SDK installs this wrapper through `ToolRegistry.set_security()` before tool registration/snapshots; it does not duplicate the policy evaluator or change standalone entrypoint defaults.

Install the wrapper before snapshots, filtered registries, or subagent registries are created. These currently share the security object, making that object the appropriate policy boundary rather than a registry subclass that cloning might discard.

The adapter should expose an understandable refusal such as:

> This tool call requires interactive approval, which is not supported by this integration. The tool was not executed.

Represent this as a failed/blocked tool result, not an approval card. Use a stable diagnostic code where the adapter can preserve the reason. The model may continue with allowed alternatives within its execution budget.

### 8.2 Explicit exclusions

Do not:

- Rewrite a profile's security configuration or override the native manager's final verdict merely to eliminate a `REVIEW`. Honoring native `minimal` for new and existing profiles is not per-call auto-approval.
- Automatically answer every approval with `true`.
- Emit an approval request and then ignore it or wait for its timeout.
- Assume that returning `false` from a callback prevents an already-emitted approval event.
- Advertise interactive approval capability or render DeepOrca approval controls.
- Remove or change existing CLI-runtime approval behavior in DeepBox.

Any unexpected approval request reaching the DeepOrca sink is an integration-contract failure. Fail the affected operation promptly and surface a diagnostic instead of leaving a turn stuck in a waiting state. Tests must prove that the normal `REVIEW` path never reaches this fallback.

## 9. Events, input delivery, and persistence

### 9.1 Reuse the structured output channel

Keep `surface="structured"` and the current outer `kind="event"` framing. Add versioned DeepOrca metadata to the existing canonical event payload rather than creating a second browser socket.

Example **proposed** payload inside the existing output envelope:

```json
{
  "ev": "message.delta",
  "text": "The adapter belongs in the Connector.",
  "turn_id": "turn_opaque_id",
  "turn_seq": 12,
  "message_id": "message_opaque_id",
  "native": {
    "runtime": "deeporca",
    "schema": "deeporca.turn.v1",
    "type": "text",
    "content": "The adapter belongs in the Connector."
  }
}
```

Agent/session/stream identity remains in the authoritative outer envelope. Turn-local sequence numbers do not replace DeepBox output sequence numbers or byte offsets. Assign stable event identity so duplicate delivery can be ignored.

The canonical projection supports a basic generic renderer; the native extension preserves richer DeepOrca behavior. Emit one durable record per display event, not two independent events that can duplicate visible text.

### 9.2 Mapping

| DeepOrca source | DeepBox projection |
| --- | --- |
| Accepted human input | `user.echo`, correlated by `client_input_id` |
| `TextEvent` / wire `text.content` | `message.delta`; content is incremental, not the full final answer |
| `ThinkingEvent` | Versioned thinking extension rendered in a collapsible block |
| `ToolCallEvent` | `tool.call` with stable `tool_id`, name, input, and optional parent identity |
| `ToolResultEvent` | `tool.result` with the same `tool_id`, content and error/refusal status |
| Status / usage | Versioned status/usage metadata |
| Turn result after settlement | Exactly one display `turn.end` for that admitted turn |
| Tool requiring review | Immediate blocked tool result; **no approval event** |

Preserve the typed turn envelope before any `to_wire_dict()` conversion. Add provider/tool call identity to the native event path where needed. Never pair concurrent results only by tool name or by "the last card." Stable IDs are a prerequisite for advertising concurrent/nested tool rendering.

The engine may emit `DoneEvent` before native context is saved. Do not interpret that intermediate event as committed completion. Finalize display state when `execute()`/sink closure confirms settlement; handle save exceptions explicitly and avoid a duplicate `turn.end`.

### 9.3 Durable input admission

Reuse the existing `client_input_id` and input-ack protocol, but make the library adapter's admission semantics explicit.

- Scope deduplication by Agent/session and input ID.
- Reject invalid or busy submissions before acknowledging admission.
- Persist the accepted input and its execution state before sending a success receipt or handing it to an asynchronous worker.
- An acknowledgement means **accepted/delivered according to the existing protocol**, not "model finished" or "all tool effects are committed."
- Keep one authoritative input echo and reconcile optimistic UI rows by input ID, not merely equal text.

The reviewed Supervisor records a deduplication receipt before invoking the session writer. For an asynchronous library handoff, a receipt alone is insufficient: add a durable admission journal or an equivalent atomic queue entry. Do not claim crash-safe execution simply because the input ID is remembered.

Proposed execution journal states are `accepted`, `running`, and `settled`, with an explicit `interrupted/uncertain` recovery outcome. Mark dispatch intent durably before handing work to the worker. A definitely undispatched accepted input can be recovered; an input whose execution might have started must not be automatically replayed after a crash. A new user retry must explain possible prior side effects.

### 9.4 Two independent histories

| Store | Owner | Purpose |
| --- | --- | --- |
| Event spool and recorded structured output | DeepBox transport/Server | Rebuild the user's display and resume output delivery |
| Native sessions, persona, memory, compaction-related state | DeepOrca on Connector | Continue model execution with the correct native context |

Use a stable native `SessionRef` per DeepBox conversation within the bound profile. Persist that mapping; do not generate a new native session just because a viewer reloads or the Connector reconnects.

If the display log exists but native context is missing, show a context-unavailable state. Do not synthesize model history from rendered bubbles. Conversely, a missing display log must not cause native tool execution to repeat.

Server retention settings and native profile retention are separate. A "no recording" session must not silently recreate a permanent server transcript; deleting a display session must not silently erase native memory. Label recoverability limitations when retained display events are unavailable.

### 9.5 Backpressure and large outputs

- Bound worker-to-Supervisor queues and respect the existing output frame and spool limits.
- Coalesce small text deltas within a bounded latency window, without changing order or Unicode byte accounting.
- Never split a JSON record into invalid fragments to fit a frame limit.
- Project large tool output into explicitly truncated display results when necessary; do not claim the preview is the full result or alter native model context to match it.
- On storage failure or exhausted spool capacity, stop accepting turns and settle/cancel affected work explicitly rather than continuing with unrecorded "successful" output.
- Browser reconnect must use output cursors and the same reducer as live delivery; it must not resubmit user input.

## 10. DeepOrca-style rendering in DeepBox

### 10.1 Extract a view, not a page

DeepOrca's `chat.html` contains both presentation and application-level behavior. `webui/js/conversation-view.js` manages view state but is not a complete standalone chat component.

Extract or port the conversation presentation behind an injected host interface. Do not copy global page variables, connect to `/ws/mux`, or call DeepOrca session/file endpoints directly.

Proposed interface:

```javascript
const view = createDeepOrcaChatView(container, {
  sendInput,
  interrupt,
  getCapabilities,
});

view.applyEvent(event);
view.restore(events);
view.setAccess({ canSend, canInterrupt, readOnly });
view.destroy();
```

There is no `approve` callback in v1. File resolution/upload can be added later through authenticated DeepBox capabilities, not raw local URLs.

### 10.2 Presentation scope

Retain the DeepBox workspace, machine/Agent tree, pane tabs, connection state, role boundaries, and session management. Replace only the conversation interior with DeepOrca-style elements:

- User and assistant messages.
- Streaming Markdown and code blocks, with safe rendering and copy actions.
- Collapsible thinking/status content actually provided by the runtime; do not fabricate hidden reasoning.
- Tool input/result cards, including immediate policy refusals.
- Run status, stop action, and supported model/context indicators.
- Empty, loading, disconnected, failed, and read-only replay states.

Each pane owns its reducer and DOM state. Avoid global `currentChatId` variables and document-wide ID lookups. Scope styles to a renderer root or an appropriately designed Shadow DOM boundary.

### 10.3 Compatibility and safety

- Persist the renderer/schema selection with the session or its initial durable configuration event so archived chats work even when the machine is offline.
- Resolve renderer identifiers from a local allowlist. Never load code or CSS from a Connector-supplied URL.
- Provide a generic structured fallback for unsupported renderer versions; preserve readable messages and show an unsupported-feature notice rather than silently dropping the conversation.
- Treat Markdown, tool output, filenames, and model-supplied links as untrusted. Sanitize HTML, restrict URL schemes, and avoid unsafe `innerHTML` interpolation.
- Replay must be side-effect-free and use the same state reducer as live output.
- Viewer permissions are enforced on the Server, not just by disabled buttons. Agent creation requires the existing management permission; input and interrupt require the existing session/operator permission.
- Keep absolute paths out of inventory and binding metadata. Tool/message contents may themselves mention paths; apply the configured recording/redaction policy rather than claiming that arbitrary transcript text is path-free.

## 11. Prototype alignment

The local-only interactive prototype and its notes demonstrate layout and event-driven interaction. They are not committed artifacts or a normative v1 specification.

The next prototype revision should:

1. Start from an empty/connected-machine workspace and expose **Add agent → DeepOrca**.
2. Separate Agent creation, Agent settings, and new conversation actions.
3. Replace raw package/workspace path entry with registered machine-side references.
4. Remove approval cards, approval actions, the approval demo shortcut, and the interactive approval policy selector.
5. Demonstrate a normal allowed tool and an immediate policy-blocked tool instead.
6. Show provisioning, needs-configuration, ready, and initialization-error states.
7. Keep cancellation, reconnect, event inspection, and side-effect-free replay.

The old prototype's "all simulated" labeling remains necessary until its mock transport is replaced. This document does not itself change that HTML or implement the backend.

## 12. Implementation work breakdown

### DeepBox changes

| Area | Change |
| --- | --- |
| `connector/runtimes.py` | Describe backend type, structured surface and renderer capability independently of CLI commands |
| `connector/runtime_probe.py` | Detect supported Python package/version without initializing an Agent in the Supervisor |
| Connector inventory/client | Publish sanitized runtime/profile/template references and provisioning status |
| `connector/local_store.py` or dedicated binding store | Persist binding identities, revisions, ownership and native session mappings |
| `connector/supervisor.py` | Introduce the session factory, durable admission handoff, interrupt dispatch and asynchronous close |
| New library worker/session modules | Initialize DeepOrca, own lifecycle, enforce non-interactive security and bridge events |
| `connector/spool.py` | Add/extend durable input admission state where needed; preserve output ordering and ACK behavior |
| Server Agent/runtime metadata | Validate desired state and store/display provisioning and renderer metadata without secrets or local paths |
| `web/management.js` | Extend Add agent with native profile creation/binding and readiness/error UX |
| `web/pane.js` and renderer registry | Select the renderer, inject host actions, preserve reconnect/replay semantics |
| New chat view/styles/reducer | DeepOrca presentation without page-level network dependencies or approval UI |

### DeepOrca changes

- Extract a public, idempotent profile initializer from the CLI-specific creation path.
- Expose a narrow initialization/security hook or accessor needed to wrap the existing security manager without private-field coupling.
- Add a cooperative profile-ownership lock for supported embedded and standalone entry points before enabling existing-profile binding.
- Preserve stable tool invocation/parent identity and structured failure classification through native events and typed translation; do not infer tool identity or security outcomes by parsing human-readable result text.
- Define orderly close and task ownership for the embedded runtime; do not assume a single MCP close call settles all resources.
- If a reusable frontend package is introduced, have the original WebUI and DeepBox consume the same network-independent conversation components.

DeepOrca remains optional for a Connector that only runs other runtimes. A missing package must disable/report that runtime, not prevent the Connector or unrelated agents from starting. Pin and test a supported package/adapter combination; do not automatically install the latest version during Agent creation.

## 13. Delivery plan

### Phase 1 — Native Agent creation and execution

- Runtime inventory and machine-local setup diagnostics.
- Add agent creation with idempotent provisioning; enable the bind-existing path only when profile ownership locking is supported.
- Isolated library worker and persistent session mapping.
- Non-interactive policy enforcement before every relevant tool path.
- Text streaming and basic tool results using the existing generic structured renderer.
- Cancellation, asynchronous close, and durable admission/output behavior.

**Exit criterion:** A user can create an Agent, run a permitted tool, receive a prompt refusal for a review-required tool, stop a turn, and continue the native conversation after a normal restart. No approval UI or approval traffic is involved.

### Phase 2 — DeepOrca conversation renderer

- Extract/port the styled conversation components.
- Stable event identities, richer thinking/tool/status views, generic fallback.
- Multi-pane isolation, role-aware controls, refresh/reconnect and archived replay.
- Revise the prototype to match the actual no-approval onboarding flow.

**Exit criterion:** The same recorded event sequence produces equivalent live, reconnected and replayed conversation state, and the renderer does not depend on a DeepOrca WebServer.

### Phase 3 — Optional extensions

Authenticated artifacts/images, richer configuration management, advanced child-task support, and additional SDK isolation work can follow independently.

Interactive approval is **not a hidden dependency or an automatic next milestone**. It requires a separate future design decision if requested.

## 14. Acceptance tests

### Provisioning and authorization

- Admin/owner can create an Agent on an authorized machine; viewer/operator cannot bypass management authorization.
- Wrong-machine project/profile/template references, arbitrary filesystem paths and unsafe configuration keys are rejected.
- Missing package, incompatible SDK, missing model configuration and failed bootstrap produce distinct actionable states.
- Repeated creation/reconciliation delivery does not duplicate or overwrite profiles.
- Reconnect after local initialization but before status acknowledgement converges to the same binding.
- Binding an existing profile preserves its files and rejects conflicting ownership.
- Renaming an Agent does not change its native profile or session identity.

### Non-interactive execution

- `ALLOW` executes the tool normally and preserves argument/path checks.
- `DENY` does not execute it.
- Final `REVIEW` is refused before emitting an approval request; no pending approval future, timeout, or approval UI appears.
- The same behavior holds for registry snapshots and every supported child/subagent path.
- No code selects permissive development mode or auto-approves requests to meet these tests.
- Unexpected approval events fail visibly and cannot strand a running session.

### Lifecycle and recovery

- Turn completion is not acknowledged as settled before native save succeeds.
- Stop cancels the owned task; post-cancel output cannot resurrect a finished turn.
- A hung tool cannot keep Stop pending past its bounded cleanup deadline; escalation retires the worker instance, rejects late output from that instance, and reports uncertain persistence/side effects honestly.
- User cancellation remains distinct from timeout when a wall budget is configured.
- Two sessions on one Agent respect v1 admission serialization; separate Agents do not share environment, security state, workspace or native history.
- Browser refresh/disconnect does not cancel or duplicate a turn.
- Output resend does not duplicate text or tool cards.
- An accepted input survives a transport disconnect; an uncertain in-flight input after a crash is not automatically re-executed.
- Agent deletion stops admissions and owned work without silently deleting native files.
- Disk-full/backpressure and abrupt worker death produce explicit outcomes, not silent successful completion.

### Renderer and security

- Real-time, restore and read-only replay share reducer fixtures, including cancellation and blocked tools.
- Unicode deltas, large tool output, repeated tool names and unknown event/schema versions are handled correctly.
- Viewer controls are disabled and equivalent forged control requests are rejected server-side.
- Untrusted HTML, script URLs, tool arguments and filenames cannot execute browser code.
- No model credentials or machine-private binding paths appear in API metadata or diagnostic logs.
- Existing PTY/CLI runtimes and their approval paths continue to pass their regression tests.

## 15. Source references

DeepBox paths are relative to this repository:

- [Agent management UI](../web/management.js)
- [Agent creation, authorization and session forwarding](../server/app/main.py)
- [Runtime descriptions](../connector/runtimes.py)
- [Supervisor](../connector/supervisor.py)
- [Structured CLI session](../connector/agent_session.py)
- [Local project registry](../connector/local_store.py)
- [Durable spool](../connector/spool.py)
- [Structured state reducer and generic renderer](../web/chat.js)
- [Pane integration](../web/pane.js)
- [Existing architecture](design.md)

DeepOrca paths are relative to `C:/repos-gim/deeporca` in the reviewed checkout:

- `examples/run_embedded_agent.py`
- `deeporca/cli.py::_create_agent`
- `deeporca/bootstrap.py::ainit_deeporca`, `init_deeporca`
- `deeporca/turns/engine.py::TurnEngine`
- `deeporca/turns/models.py`
- `deeporca/turns/coordinator.py::SessionRunCoordinator`
- `deeporca/turns/wire.py::to_wire_dict`
- `deeporca/server.py` (existing WebUI engine construction)
- `deeporca/tools.py::ToolRegistry`
- `deeporca/security/__init__.py::SecurityManager`
- `deeporca/security/approval.py`
- `deeporca/agent.py` (tool security verdict handling)
- `deeporca/task_registry.py`
- `deeporca/chat.html` and `deeporca/webui/js/conversation-view.js`

---

## Module boundaries and presentation

This follow-up reorganizes the existing v1 implementation and replaces the first
chat presentation. It does not introduce a second native WebServer, change the
wire protocol or enable interactive approvals. The later existing-profile
extension adds explicit binding without importing native chat history.

### Ownership

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

### Native security policy boundary

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
[existing-profile binding](deeporca.md#existing-profile-binding) native-stop requirement
and exclusive embedded ownership checks are unchanged.

### Native-style chat

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

### Visual and behavioral evidence

Local-only screenshots come from the actual workbench under Playwright with fixture
REST/WebSocket data. They demonstrate presentation, not model authentication or
real tool execution; the SDK/Server/Connector tests are separate. Prototype
directories and screenshots are not committed. Production presentation checks
remain in `tests/test_deeporca_browser.py`, covering desktop, independent native
and CLI panes, mobile conversations, and nested Markdown/tables.

See [validation](#validation-record) for test results and
[acceptance mapping](#design-to-implementation-checklist) for the original v1 guarantees.

The subsequent [web profile configuration](deeporca.md#web-profile-configuration)
extension adds DeepOrca-only model setup/editing, sealed credentials and
revisioned configuration updates without changing these ownership boundaries.

---

## Design-to-implementation checklist

This checklist maps the v1 requirements in [the design](deeporca-integration-design.md) to current source and executable tests. Native paths below refer to the sibling DeepOrca repository, not a second service running on the Server. A listed test is a coverage reference; actual execution results and commands are recorded in [validation](#validation-record).

### Functional and safety requirements

| Design requirement | Implementation evidence | Verification references |
| --- | --- | --- |
| Connector-side library, not CLI/WebServer/iframe; optional compatible SDK probe | `connector/integrations/deeporca/adapter.py`, `probe.py`, `worker.py`; native `deeporca/embedded.py::EmbeddedRuntime`; public API version 1 | `test_library_descriptor_has_no_cli_or_approval_arguments`, `test_optional_library_probe`, `test_probe_never_publishes_raw_diagnostics`; actual SDK integration tests |
| Browser configuration uses registered project/template/profile references, never executable/package/env/arbitrary-path inputs; API keys are sealed before Server submission | `agentbridge/integrations/deeporca/contract.py::validate_runtime_config`; Server create/update validation; `DeepOrcaStore.ensure_binding`; fixed `connector-default` reference | `test_shared_contract_rejects_unknown_or_unsafe_configuration`, `test_create_validation_pending_directory_and_offline_sessions`, `test_permissions_project_isolation_and_retry`, native template/profile security tests |
| Add agent provisions asynchronously with desired/observed revisions and safe failure states | `server/app/models.py`, Server runtime status/provisioning handlers; `DeepOrcaSupervisorMixin._ensure_library_worker` and reconciliation | `test_observed_status_ws_spoof_stale_and_sanitization`, `test_binding_immutable_delete_only_pushes_directory_and_generic_unchanged`, live E2E create/pending/ready |
| Profile creation idempotent; identity immutable; deletion retires owned work, does not delete native data | `connector/integrations/deeporca/store.py`, `supervisor.py`, native `ensure_profile`; Server update/delete checks | `test_bindings_idempotent_private_and_namespace_isolated`, `test_binding_immutable_delete_only_pushes_directory_and_generic_unchanged`, `test_retiring_agent_preserves_profile_and_rejects_new_turns`, native ownership/profile tests |
| One isolated owned worker per Agent; one foreground turn across its conversations | `DeepOrcaWorker.reserve/dispatch`, `DeepOrcaSession`, native process/profile ownership and `SessionRunCoordinator` | `test_worker_shared_busy_stop_and_closed_session_settlement`, worker/SDK lock and busy tests |
| Enrollment identities cannot silently share native context or pending private output | Namespace from canonical Server URL + devbox ID; private per-spool pin in `DeepOrcaStore`; `set_enrollment` guard before transport | `test_restart_rejects_shared_native_spool_before_sender`, `test_spool_ownership_is_private_per_spool_and_ack_drained`, `test_unpinned_legacy_data_is_not_assigned_to_new_pending_scope`, CLI-only compatibility tests |
| Durable input admission/receipt and duplicate suppression; uncertain work never automatically replayed | `DeepOrcaStore.admit_input/settle_input/recover_interrupted`; `_library_handle_input`; correlated ACKs | `test_real_worker_delivery_jsonl_and_duplicate_receipts`, `test_uncertain_admission_refused_on_reconnect_without_resubmit`, `test_crash_admission_is_uncertain_not_replayed`, real E2E duplicate request count |
| Native context separate from display history, persisted across worker/Connector restart | Deterministic native session mapping, native guarded `SessionManager`, native `TurnEngine`; no UI transcript injection | `test_real_sdk_ready_turn_persistence_and_restart`; `test_real_connector_sdk_browser_roundtrip` includes normal Connector restart |
| Missing native context must not silently become a fresh conversation | Native `.embedded-contexts.json` manifest and guarded session loading; safe `native_context_unavailable` projection through worker/session | `test_real_sdk_deleted_native_chat_rejects_without_replay_and_fresh_chat_works`, focused native context/history/manifest corruption tests |
| Stop waits for owned cleanup, has an outer deadline, and never promises rollback | Native `ToolRegistry(wait_for_sync_cleanup=True)`, cloned-registry preservation; SDK interrupt/close; worker interrupt/retirement grace | `tests/test_tools_cancel_cleanup.py`, native SDK interrupt tests, worker forced-retirement tests, Supervisor stop/uncertainty tests |
| No DeepOrca interactive approval UI, response transport, pending loop, or auto-approval; CLI behavior retained | Native `NonInteractivePolicy` delegates to the unmodified `SecurityManager`, rejects final `REVIEW`/`DENY` and unsupported background/autonomous tools; no embedded `standard`/`allowlist` floor; unexpected-event checks; Server permission-control rejection; pane composer and renderer | `test_deeporca_permission_rejected_after_attachment_and_role_authorization`, `test_generic_permission_and_runtime_specific_input_options_still_forward`, `test_real_sdk_workspace_read_and_failfast_approval_denial`, Chromium native+CLI pane test |
| Canonical newline-terminated JSONL, preserved native turn/tool identity, UTF-8 byte/sequence reliability | `connector/integrations/deeporca/events.py`, `session.py`, shared spool/recording/restore; native typed/wire tool IDs; reducer matches turn plus tool ID | Worker/event/session tests, `test_real_worker_delivery_jsonl_and_duplicate_receipts`, live recording/restore E2E, Node reused-tool-ID tests |
| Short-latency coalescing preserves identity, order and unmerged boundaries | `connector/integrations/deeporca/worker.py::_NativeEventBatcher`: 12ms / 16KiB window for schema-known adjacent text/thinking; original-event byte accounting; supervised timer joined before terminal result | `test_latency_deadline_is_not_reset_by_more_deltas`, `test_boundaries_flush_first_and_are_never_coalesced`, `test_unicode_serialized_budget_and_original_turn_accounting`, `test_real_spawn_burst_final_flush_precedes_result_or_fault`, `test_real_spawn_durable_output_failure_is_uncertain` |
| Bounded transport/output and explicit pressure failure, not dropped records or false success | IPC/frame/turn limits; O(1) spool pending usage; native high water and bounded failure reserve | `test_backlog_rejects_without_receipt_and_same_id_retries_after_ack`, `test_pressure_during_native_turn_is_bounded_and_uncertain`, `test_terminal_enqueue_failure_never_records_completed_receipt`, CLI pressure-policy compatibility |
| Provider failure must settle as failure, not completed | Native `TurnEngine` handles error events and `done.error`; SDK projects safe errors | `tests/test_turn_error_settlement.py`, embedded provider-failure tests |
| Add agent, Agent settings and new conversation are separate; readiness is not provider-auth proof | `web/management.js`, Agent menu in `web/app.js`, Server settings/retry routes | Node management stale-context/permissions tests, `test_real_workbench_add_agent_project_and_status`, full E2E provisioning |
| Explicit continuation after restart; passive replay/layout restore cannot create work | History-only one-shot native continuation in `web/pane.js`, gated by role/runtime/inactive state | `test_continue_native_is_explicit_operator_action_not_layout_restore`, live E2E normal restart |
| Pane-local native rendering, safe Markdown/links, thinking/tools/copy, mobile, read-only replay; unsupported renderer safely falls back with notice | `web/integrations/deeporca/`, `web/chat.js`, allowlisted factory and notice in `web/pane.js`; explicit live/replay access state | `test_real_workbench_renderer_panes_and_mobile`, `test_unknown_renderer_notice_and_unfinished_native_replay`, `test_v2_pane_anchored_layout_markdown_and_compact_native_tools`, `test_v2_streaming_error_and_disclosure_state_are_accurate`, `test_native_canonical_usage_and_disconnected_tool_state`, all Node web suites |
| Runtime-specific implementation is isolated from platform code | Dedicated `integrations/deeporca/` packages; registered probe/adapter, supervisor hooks, Server policies, pane renderer contract | `test_deeporca_implementation_is_not_flattened_into_platform_packages`, `test_shared_deeporca_contract_does_not_import_optional_sdk`, `test_server_extension_imports_without_app_database_connector_or_sdk`, `test_platform_main_has_no_deeporca_contract_or_runtime_branches` |
| Rejected/ambiguous browser input must not stay falsely running or resend automatically; IDs valid on LAN HTTP | UUID fallback and correlated ACK/draft handling in `web/pane.js`; native Server validation rejection ACKs | `test_uuid_fallback_and_rejected_native_input_keep_the_draft` (two crypto modes), `test_deeporca_text_input_options_rejected_before_queue_or_forward`, ownership-loss/correlation tests |
| Install/setup and operational failure guidance | `docs/deeporca.md`, native `docs/embedded-runtime.md`, validation report | Documentation syntax/link checks, `tests/test_deeporca_browser.py` |
| Existing profile binding uses local inventory, immutable identity and explicit manual native-stop consent; no native history import or configuration writes | `profiles.py`, Server capability validation, existing-mode worker branch; native `profile_mode="existing"` | `test_create_binding_requires_inventory_from_target_machine`, `test_advertised_native_binding_is_read_only_and_busy_safe`, `test_authoritative_directory_retires_offline_deleted_binding_only_in_its_namespace`, `test_directory_retirement_keeps_reservation_until_worker_exit_is_confirmed` |
| Native default is `minimal`; explicit security configuration is respected for every profile mode, including existing minimal profiles at runtime; existing files are not proactively rewritten | Native `ensure_profile()` copies default/trusted-template security unchanged; embedded runtime uses the unmodified native manager; no security migration, creation opt-in/marker, extra Connector flag or separately versioned security-default API | Native embedded security tests: default/template copying, retry file preservation, existing-profile minimal behavior, final review/denial and unsupported-tool refusal; actual run evidence belongs in validation |

Text/thinking batching does not merge unknown extension fields, identities or tool/error/status boundaries. A valid native event larger than the batching window passes through within the existing frame limit rather than being split into duplicate native identities. Callback/timer failures prevent a successful terminal receipt; `test_cancelled_sdk_callback_is_supervised_including_lock_wait` explicitly covers cancellation both while sending and while waiting for the timer's writer lock.

### Explicit design gates and limits, not claimed implementation

- **Existing-profile binding requires manual native-writer exclusion**: stop native CLI/WebUI writers and keep them stopped while the Connector owns the profile. The SDK rejects known live native PIDs and embedded lock conflicts, but does not provide a shared native/embedded lease or automatic takeover. Native history is not imported.
- Autonomous/background execution, schedules/goals, subagents, persistent shell sessions, live catalog mutation and uploads are outside the supported v1 surface. Allowed foreground tools still run under local policy.
- Real-provider authentication/billing and production deployment are not acceptance evidence here. A locally configured/ready worker is not proof of provider credentials. The live E2E uses a loopback scripted provider; browser tests use fixture API/socket data.
- Durable admission is not exactly-once tool effects. Forced stop, crash and full-disk conditions can be uncertain. Browser drafts are not a durable browser outbox.
- Native context manifests detect missing/corrupt state, not malicious edits or valid rollback. Pre-manifest development profiles are not automatically migrated.
- Native identity survives token rotation, but the inherited token-hashed default spool filename does not migrate itself. Drain/recover old pending output deliberately.
- Native spool high water counts pending encoded records, not total SQLite/WAL disk size. CLI spool policy is unchanged. A full disk can prevent the emergency failure record itself.
- The historical proposed SDK examples in the design are not the literal shipped API; the design's implementation reconciliation and `deeporca.embedded` public contract are authoritative.

---

## Validation record

This records local branch verification, not a production deployment or external-provider certification.

- AgentBridge branch: `feat/deeporca-library-integration`; initial integration `36bc8e0`, bounded-stream hardening `97eaa75`, then the [module/presentation follow-up](#module-boundaries-and-presentation) documented above.
- Native SDK branch: `feat/deepbox-library-host`, tested commit `9206c0b` (embedded API v1).
- Setup: Windows, existing Python/Node/Chromium test dependencies. SDK and live integration tests used temporary profiles, projects, databases and test-only credentials, with a scripted loopback OpenAI/SSE provider. The user's agent/profile and running preview service were not used for model turns.

### Native security inheritance simplification (current working tree)

At the user's request, the security-default implementation was reduced to
removing the embedded overrides. Native DeepOrca already defaults to `minimal`.
`ensure_profile()` now copies native/template security unchanged, and runtime
startup no longer forces `standard` or `allowlist`. The added security-default
capability, keyword, persisted marker and opt-in branches were removed, along
with the Connector capability requirement. No old-profile compatibility or
migration mechanism is introduced. Explicit native security configuration is
respected; final `REVIEW` is still refused, actual denials remain denied, and
unsupported autonomous/background tools remain unavailable.

Validation for this focused simplification:

- Native embedded/default/existing/configuration: **247 passed, 6 skipped**;
  native security: **62 passed**. Windows skips are unavailable symlink checks.
- Connector worker/configuration/existing/supervisor/import checks:
  **97 passed, 2 skipped** (`security-default-simplification/connector.xml`).
- Actual SDK and Server → Connector → SDK E2E: **7 passed**
  (`security-default-simplification/sdk-e2e.xml`, 112.79s). These use disposable
  profiles, dummy credentials and a scripted loopback provider.
- Updated documentation and CRLF-aware diff checks passed.

Artifact paths above are under
`C:/Users/chec/.deeporca/agents/deepbox/tmp/`. This was a focused regression, not
another full-suite run. No commit, push, service restart or live-profile edit
was performed. Earlier implementation results below remain historical evidence,
not a claim that the removed opt-in machinery is still present.

### Existing-profile binding and minimal-security extension (preceding round)

This extends DeepBox `8da1462` plus the preceding configuration work, and native
SDK `3978d2f` plus the narrow new-profile security-default opt-in. These changes
are uncommitted. See [existing-profile operations](deeporca.md#existing-profile-binding).
Fixture screenshots are local-only artifacts, not committed files.

Evidence directory:
`C:/Users/chec/.deeporca/agents/deepbox/tmp/existing-profile-evidence/`.

| Group | Result | Artifact / scope |
|---|---:|---|
| Complete DeepBox suite before final startup-retirement hardening | **1112 passed, 13 skipped, 20 subtests passed** | `deepbox-release.xml`; 548.65s |
| Complete DeepBox suite including final hardening | **1113 passed, 1 failed, 13 skipped, 20 subtests passed** | `deepbox-final.xml`; 583.46s; Windows PTY reader-shutdown failure described below |
| PTY file and repeated failing case | **10 passed, 1 skipped**, then **1 passed** | `pty-confirm.xml`, `pty-reader-confirm.xml`; 20.88s / 3.50s |
| Actual SDK / Server–Connector E2E / existing binding / supervisor | **52 passed, 1 skipped** | `sdk-e2e-final.xml`; 182.48s; real SDK, disposable profiles and scripted loopback provider |
| Native focused regression | **597 passed, 6 skipped** | `native.xml`; 90.26s; existing mode, security default, configuration, turn/tool/security suites |
| Recursive Node suite | **336 passed** | `node-release.log` |
| Chromium / web / import boundary | **23 passed within final full run** | `deepbox-final.xml`: 12 browser scenarios, 9 web cases, 2 import-boundary cases; not a separate run |
| Retirement / configuration / supervisor regression | **105 passed, 2 skipped, 9 subtests passed** | `retirement.xml`; 65.85s; includes cancellation during startup and provisioning |
| Actual existing-profile E2E and Server validation | **31 passed** | `existing-e2e.xml`; 26.10s; includes current list and legacy capability descriptor shapes |

These scopes overlap and must not be added together. Seven opt-in SDK cases in
the full suite were exercised in the explicit SDK group. Other skips are POSIX
checks or unavailable Windows symlink privileges.

The final full run is **not claimed to be all-green**. Its sole failure was
`test_native_windows_kill_isolated_idle_python_releases_reader`: the unchanged
Windows PTY reader thread had not exited at the assertion. The complete PTY file
and a subsequent repeat of that exact case both passed without code changes.
The failed full-run artifact is retained. A separate overlapping browser run
(`browser-release.xml`) had a 10-second `page.goto` setup timeout (22 passed,
1 error); all 23 cases later passed in the final full run. The earlier
`deepbox-full.xml` branded-environment boundary failure was corrected by resolving
source/home through `agentbridge.product.env` before the disposable probe child.

Verified behavior includes target-Machine advertised references, strict native-stop
consent, immutable local target resolution, duplicate binding refusal, known-live
native PID refusal, read-only bound configuration and retained native files/history.
The actual Server → Connector → SDK binding test executes a turn without changing
the original configuration/persona/native-chat files. It does not import old chats
or prove provider authentication outside the scripted provider.

Authoritative directory reconciliation retires offline-deleted Agent reservations
only in the current enrollment. Startup/provisioning workers remain owned through
cancellation, and an unconfirmed exit retains the worker and reservation. New
DeepBox-managed profiles use real native minimal policy, not merely a YAML label;
trusted template levels win, retries/older/bound profiles are not migrated, and
final review/denial plus unsupported autonomous-tool restrictions remain intact.

Documentation links, fences, examples and named test references, plus CRLF-aware
diff checks, passed. No real user's profile was bound, no external provider was
contacted, and this extension did not restart the preview Server or Connector.

### Web profile configuration extension (preceding round)

This extends DeepBox `8da1462` (the squash of the original three integration
commits) and native SDK `9206c0b`; it does not change their historical results
below. Scope: [complete browser profile setup/editing](deeporca.md#web-profile-configuration),
DeepOrca-only fields, sealed keys, authorized idle-only updates, profile
configuration/recovery under SDK ownership, and ordinary-runtime compatibility.

Evidence directory:
`C:/Users/chec/.deeporca/agents/deepbox/tmp/profile-config-evidence/`.

| Final group | Result | Artifact / scope |
|---|---:|---|
| Complete DeepBox Python suite | **1058 passed, 11 skipped, 20 subtests passed** | `deepbox-release.xml`; 379.42s, Chromium cache located outside isolated LOCALAPPDATA; six SDK opt-ins separately executed, three POSIX guards, two unavailable symlink privileges |
| SDK integration + Server/Connector E2E + configuration/lifecycle | **51 passed, 1 skipped** | `sdk-e2e-final.xml`; all six SDK opt-in scenarios executed; skip is unavailable Windows symlink privilege |
| Recursive Node suites | **321 passed** | `node-final.log`; async permission/dialog invalidation, runtime-contract delegation and crypto/form regressions |
| Chromium / web / import-boundary group | **21 passed** | `browser-release.xml`; 44.68s, ten Chromium scenarios, nine web tests, two boundary tests |
| Native SDK focused regression | **501 passed, 3 skipped** | `native-final-rerun.xml`; embedded configuration, ownership, LLM parsing, turns/cancellation and tools/security; not the entire native suite |

Counts overlap and must not be summed. Tests never use the operator's live
preview database, profiles or provider. No operator credentials/Tokens are
regenerated and these checks do not restart their Connector.

#### Acceptance evidence

- `tests/test_deeporca_server.py`: bounded declarative settings,
  native effort/window/model rules, invalid endpoint/plaintext-key rejection,
  envelope bounds, and mutable desired revision versus immutable identity.
- `tests/test_deeporca_server.py`: create/update authorization, immutable
  project/profile fences, active-session conflict, retained sealed keys,
  endpoint-change safeguards, stale status and no-op/rename behavior.
- `tests/test_deeporca_configuration.py`: public-key publication, private key
  persistence/recovery fencing, worker-only decoding, tamper/endpoint failure,
  old SDK errors, revision updates and busy-worker safety.
- `tests/test_deeporca_integration.py`: actual browser-module WebCrypto in Node,
  real HTTP/WS Server, real Connector and spawned real native SDK, with **no model
  template**. A loopback scripted provider verifies dummy key, model and effort.
  The test updates the same profile, rejects changes with an active session,
  restarts the Connector, continues preserved native context and checks no
  plaintext dummy key enters Server HTTP bodies/responses, DB files, logs,
  canonical events or recordings.
- `tests/test_deeporca_browser.py`: real Chromium fixture UI, encrypted creation
  independently decrypted in Python, settings retention/replacement, failed-save
  draft preservation, explicit no-auth, runtime toggling and mobile sizing.
- `web/deeporca.test.js` and `web/management.test.js`:
  native-compatible fields, randomized endpoint-bound encryption, runtime-agnostic
  delegation, legacy fixed models/rename-only behavior; deferred preparation
  cannot submit after permissions/context/dialog change.
- Native `tests/test_embedded_configuration.py`: ownership before writes,
  literal-key round trip, unrelated YAML/env preservation, interrupted two-file
  recovery, symlink/reparse rejection and protected Windows DACLs established
  **before** private staged/published bytes are written.

Fixture-only screenshots are local artifacts, excluded from Git. To regenerate
them, run `tests/test_deeporca_browser.py` with `DEEPORCA_BROWSER_ARTIFACTS`
pointing to a local output directory.

#### Retained intermediate failures and review fixes

- The first full Python run isolated `LOCALAPPDATA`, making installed Chromium
  undiscoverable: **1040 passed, 29 skipped** (`deepbox-full.xml`). The release
  command explicitly sets `PLAYWRIGHT_BROWSERS_PATH`; browser skips are not
  counted as presentation coverage.
- A subsequent browser/full run exposed an obsolete fixture API key containing
  surrounding whitespace, which the SDK cannot round-trip. It now uses a valid
  dummy key and separately asserts whitespace rejection. Failure evidence remains
  in `deepbox-final.xml` / `browser-final.xml`; the corrected scenario, browser
  group and complete release suite passed (`cmd_abad87b3`, `cmd_00af7020`,
  task `6109c2cd`).
- The first broad native run had a Windows `WinError 5` directory-rename failure
  creating one temporary fixture, before configuration application. The
  configuration group and complete focused group subsequently passed; the failed
  `native-final.xml` remains, rather than being presented as passing evidence.
- Review found Windows atomic replacement could weaken an existing file's ACL;
  protected current-user/SYSTEM DACLs and before-write tests were added. Node
  regressions caught status sanitization, an asynchronous Add Agent permission
  recheck gap, and hardcoded runtime-ID handling; these production paths were
  corrected before the final Node run.

Readiness does not authenticate externally. This report does not claim paid
model execution, Copilot browser login, external deployment or protection against
actively compromised Server JavaScript.

### Historical module and native-style presentation follow-up

See [module layout and visual evidence](#module-boundaries-and-presentation). Runtime-specific
implementation now lives in dedicated packages; the Server main module and
browser management module no longer contain DeepOrca-specific runtime branches.
The renderer adapts native conversation/tool/thinking patterns without importing
native application globals, transport, remote assets or approval controls.

| Group | Result | Scope |
| --- | --- | --- |
| Complete AgentBridge Python suite | **937 passed, 8 skipped, 20 subtests passed** | Final tree, 388.22s; 16 existing SQLite datetime warnings |
| Actual SDK/E2E + Supervisor | **25 passed, zero skips** | Relocated imports, spawned workers, real SDK, Server/Connector/recording/restart; loopback fake provider only |
| Node web suites | **300 passed** | All recursively discovered web test files; native rendering/contracts and existing CLI/management behavior |
| Browser/web/boundary group | **17 passed** | Nine Chromium workbench scenarios, six web-contract tests, two package/import boundary tests |
| Focused Connector regressions | **150 passed** | Library worker/session/spool behavior, adapter/probe extension, normal Connector supervisor; overlaps full suite |
| Focused Server regressions | **154 passed** | Runtime policies, routes, workspace/onboarding/structured-event behavior; overlaps full suite |

The full run took place after all production/test changes, including management
delegation, access-only disconnect redraw and actual canonical status/native
terminal usage retention. Subsequent edits only organized documentation/evidence
and restored the original HTML line endings (normalized content unchanged).
The eight skip reasons remain three POSIX guards, one unavailable
symlink-permission check and four SDK opt-in tests separately executed above.
The SDK repository was unchanged; its earlier 323-test focused result below is
historical evidence, not a newly executed full native suite.

Browser assertions cover pane-relative geometry at 1774px and 390px, split-pane
isolation, compact tool groups, ordered/nested Markdown and tables, safe links and
HTML-as-text, persistent disclosure state, streaming/error/cancellation/missing
results, real-shaped usage frames, offline access transitions, UUID/draft/ACK
behavior, explicit continuation and preserved CLI approvals. Screenshots use
fixture APIs and do not prove external model authentication or execution.

During development, existing expectations for the removed profile selector and
old CSS classes were updated; a lost disclosure-state regression was fixed rather
than hidden by relaxing the assertion. Parent review then found and fixed the
access-only redraw and canonical metadata gaps, adding Node/browser regressions
before the final runs. A bounded read-only review found no blocking issues in the
backend/policy files it inspected; it was not an exhaustive security audit.

Current temporary evidence directory:
`C:/Users/chec/.deeporca/agents/deepbox/tmp/deeporca-modular-ui-97eaa75/`:
`deepbox-full.xml`, `sdk-e2e.xml`, `browser-final.xml`, `node-final.log`, and
`screenshots/`. No real model call, preview/Connector restart, state deletion or
schema migration was performed for this follow-up. Source processes must be
restarted when idle to load relocated Python modules; browser assets need a refresh.

### Previous v1 verification (`97eaa75`)

| Group | Result | Scope |
| --- | --- | --- |
| Complete AgentBridge Python suite | **914 passed, 8 skipped, 20 subtests passed** | Final tree, 344.10s: Connector, Server, lifecycle/authorization/recording, existing runtimes, new DeepOrca tests; 16 existing SQLite datetime deprecation warnings |
| Opt-in SDK/E2E + Supervisor group | **25 passed** | Actual SDK workers, native context recovery, review-required tool denial, missing-context refusal, live Server/Connector/WebSocket/recording, normal Connector restart, spool pressure and enrollment ownership |
| Worker/coalescing/session/Supervisor group | **112 passed** | Fake SDK workers and deterministic batching deadlines; preserved identity/boundaries/UTF-8 budgets, joined timer shutdown, spawned burst/fault/interrupt cases, uncertain receipts on output failure |
| Focused native regressions | **323 passed** | Embedding, thread-cleanup cancellation, provider-error settlement, turn phases, tools, files and security |
| Node web suites | **277 passed** | All `web/*.test.js` suites, including existing CLI controls and management behavior |
| Chromium workbench checks | **6 passed** | Real workbench modules with fixture REST/WebSocket data: Add agent/settings/retry, pane isolation, safe rendering/replay, preserved CLI approvals, mobile layout, UUID fallback/rejected drafts, explicit native continuation versus passive restore, unknown renderer fallback and unfinished replay tools |
| Documentation/static checks | **Passed** | Local Markdown links, balanced fences, Python/JSON example syntax, Git whitespace checks |

These groups overlap; their counts must not be added together. Optional SDK/E2E tests are skipped without a configured local SDK source and were explicitly run separately. The native row is a focused regression set, **not the entire native repository test suite**.

The eight full-suite skips comprise three POSIX-only checks, one unavailable symlink-permission check, and four real-SDK opt-in tests. The separately opted-in 25-test group has **zero skips**. That complete run occurred after the batching cancellation fix and Hub test synchronization change described below; no production or test source changed between that run and commit `97eaa75`.

The [design acceptance checklist](#design-to-implementation-checklist) maps v1 requirements to implementation and concrete regression tests, and distinguishes intentionally gated scope. The follow-up adds bounded 12ms / 16KiB text/thinking coalescing, an explicit safe generic-renderer fallback notice, and honest unfinished-tool presentation for inactive/replayed sessions. The initial committed tree was reverified at 883 Python passes before these additions; that earlier run is not evidence for the new code.

An earlier concurrent full run hit the existing Windows PTY reader-settlement assertion in `test_native_windows_kill_isolated_idle_python_releases_reader`. That test passed on isolated rerun, and the final full suite passed without parallel heavy validation jobs. Its code/test timeout was not weakened or changed. This remains an intermittent platform observation, not a hidden passing result.

Two intermediate follow-up full runs each reported **1 failed, 911 passed, 8 skipped, 20 subtests passed**: the existing Hub stalled-watcher test checked eviction after a fixed 30ms sleep. Its isolated module run passed all 10 tests. The fixture now exposes actual send/close events: the test preserves its 10ms send timeout and 200ms producer bound, observes healthy delivery, then waits for close with a one-second test deadline and retains all eviction/close-code assertions. No production Hub code or policy timeout was changed. The revised Hub module again passed all 10 tests.

A separate final review found that cancellation while a native callback waited for the batching lock was outside its failure handler. The handler now covers lock acquisition as well as sending. The new regression failed with the handler deliberately bypassed, then passed for both lock-wait and in-send cancellation with the fix; all 30 coalescing tests, the 112-test worker group and the 25-test actual-SDK group were rerun successfully afterward.

### Reproduction

From AgentBridge, with test dependencies already installed:

```bat
python -m pytest tests -q
python -c "import subprocess,pathlib,sys; sys.exit(subprocess.call(['node','--test',*[str(p) for p in pathlib.Path('web').rglob('*.test.js')]]))"
python -m pytest tests/test_deeporca_browser.py -q
python -m pytest tests/test_deeporca_runtime.py -q
```

Use an isolated test data directory/database when running the entire suite. During this verification `PYTHON_DOTENV_DISABLED=1`, `DEEPBOX_DATABASE_URL` and `DEEPBOX_DATA_DIR` were set command-locally to temporary test destinations, not the running preview's data.

The earlier local JUnit evidence is under `C:/Users/chec/.deeporca/agents/deepbox/tmp/deeporca-round2-evidence/`: `deepbox-full.xml` (initial committed tree), `deepbox-final.xml` and `deepbox-final-rerun.xml` (the two intermediate Hub failures described above), `deepbox-release.xml` (tree committed as `97eaa75`), `native.xml`, `sdk-e2e-final.xml`, `worker-stream-final.xml`, `coalescing-final.xml`, `browser.xml`, `hub-isolated.xml` and `hub-final.xml`; its Node output is `node-final.log`. These are local temporary artifacts, not committed fixtures or durable production evidence. Add `--junitxml=<local-path>` to the pytest commands to reproduce machine-readable evidence. At that point the checklist's 38 named test-function references were verified against both source trees.

The optional real-SDK group:

```bat
set "AGENTBRIDGE_DEEPORCA_SOURCE=C:\repos-gim\deeporca"
python -m pytest tests/test_deeporca_integration.py tests/test_deeporca_runtime.py -q
```

The source must contain compatible API v1 and its dependencies must be available to the Connector interpreter. The fixtures create their own fake provider/template, guard worker outbound networking, and use temporary managed profiles. Do not substitute a real user's profile or provider key into these tests.

From the native repository:

```bat
python -m pytest tests/test_embedded.py tests/test_turn_error_settlement.py tests/test_tools_cancel_cleanup.py tests/test_turn_characterization.py tests/test_turns.py tests/test_turns_phase3.py tests/test_turns_phase4.py tests/test_turns_phase5.py tests/test_turns_phase6.py tests/test_turns_phase7.py tests/test_turns_phase8.py tests/test_turns_phase9.py tests/test_tools.py tests/test_file_tools.py tests/test_security.py -q
```

### Merge with native writers and session-history actions

PR #1 was reconciled with `main` at `0aa3e09`, retaining native writer ownership,
history rename and explicit lifecycle generations alongside the DeepOrca library
runtime, managed configuration and existing-profile binding. New regressions cover
library continuation without CLI adoption, authorization and generation fencing,
originating-generation input acknowledgments, and passive browser restoration.

The merged-tree validation results were:

- Full Python suite: **1321 passed, 14 skipped, 24 subtests passed**. The skips
  are seven real-SDK opt-ins and seven host/platform permission cases; the SDK
  cases were exercised separately below.
- All 17 recursively discovered Node test files: **354 passed**.
- Chromium/browser, web integration and web assets: **23 passed** (overlaps the
  full Python suite).
- Connector transport plus real SDK/E2E group: **24 passed** with isolated SDK
  source archived from `b222de5`. This includes managed model reconfiguration
  after termination, restart/continuation, and existing-profile operation.
- CRLF-aware whitespace, residual conflict markers and documentation checks
  passed. Counts above overlap and must not be added together.

The first full run had five failures from old lifecycle/UI test expectations;
these were retained and corrected for immediate `status: ended`, generation
tokens, authoritative session metadata and the **View history** action. Real
E2E then exposed a transport defect: a late exit after termination receives
`stale_launch`, which had incorrectly stopped the entire Connector as an output
protocol error. Only identity-free stale lifecycle rejections are now ignored;
pending durable rows still require their exact output ACK, and output/unknown
errors still fail closed. Regression tests cover both sides of this boundary.

An earlier SDK run against concurrent native history-log work failed a history
filename assertion. Stable-SDK verification used an archive rather than changing
that live checkout. No live services/profiles or external model providers were
used or modified in this merge validation.

Local temporary evidence is under
`C:/Users/chec/.deeporca/agents/deepbox/tmp/pr1-merge-evidence/`:
`full.xml` retains the first failure run; `full-resolved.xml`/`.log` and
`sdk-e2e-transport-fixed.xml`/`.log` contain the final Python results;
`ui-regressions-b739728384/` contains browser and recursive Node logs. These
artifacts are not committed fixtures or deployment evidence.

### Important assertions

- Browser inputs/configuration never choose an executable, package, environment or arbitrary workspace/template/profile path. Configuration uses registered projects, the Connector's `connector-default` template or advertised opaque existing-profile references. Web-entered API keys are sealed before Server submission.
- DeepOrca has no approval request/response UI, transport or waiting state. Native defaults/configuration determine security policy for every profile; there is no embedded standard/allowlist floor. Final review and denial are never auto-approved. Other runtimes retain their approval behavior.
- Durable input receipts prevent re-execution of repeated input IDs; crash/forced-stop uncertainty is not automatically retried and is not called exactly-once tool execution.
- Synchronous threaded tools are not declared quiescent merely because an await was cancelled. The embedded host waits for their cleanup, bounded by outer worker retirement.
- A previously used conversation with unrecoverable native state is refused before another provider/tool call. UI transcript replay never becomes native model context.
- Output pressure rejects new admission and settles affected work as uncertain, without deleting older output or falsely reporting completion. Full-disk conditions can prevent even an emergency notice.
- Incremental text/thinking batching keeps the last native sequence without inventing identities, flushes before nonmergeable events and terminal results, counts original unmerged bytes against the turn budget, and surfaces callback/deadline-writer failures even if the SDK swallows a callback exception.
- Unknown renderer identifiers never load a descriptor-supplied script; the generic fallback is visibly identified. Unfinished replay tools say that their result was not recorded, not that they are still running.
- Native-used shared spools cannot be silently handed to another enrollment with old pending output/controls. Default spool naming still inherits token hashes; native identity continuity does not automatically migrate spool files.
- Normal restart can explicitly resume the same conversation with a new output epoch. Read-only replay and layout restore do not resume it, and ended sessions/viewers do not get continuation controls.
- Rejected or unconfirmed browser submissions preserve a draft and do not automatically resend it. Browser draft storage is not a durable outbox.

### Screenshots and limits

Local screenshots show the **real workbench renderer with test fixture data**,
not external-model sessions: desktop conversation and existing CLI panes, Agent
settings/readiness, and mobile workbench. They are not committed artifacts.

The separate local-only offline prototype is illustrative and non-normative.
Prototype directories and their standalone test are excluded from Git. Historical
suite counts above predate this exclusion; production Workbench tests remain.

Not verified here: real external-provider credentials/billing, remote deployment, binding a real user's live profile, autonomous/background execution, or arbitrary subprocess rollback. Existing-profile binding is verified with disposable native profiles and a loopback provider, not a shared native/embedded writer lease. No production deployment or restart for the binding extension was performed; the earlier model-configuration preview restart is a separate operational action. See [operational setup and limits](deeporca.md) and [the reconciled design](deeporca-integration-design.md) before enabling the runtime in a real Connector.
