# DeepOrca Library Integration for DeepBox

**Status:** v1 implemented on `feat/deeporca-library-integration` (DeepBox) and `feat/deepbox-library-host` (DeepOrca). This is the architectural design, not a production deployment claim. See [operational setup](deeporca.md) and the implementation reconciliation below.

**Scope decision:** Interactive tool approval is **not implemented in v1**. This includes approval prompts, approval buttons, approval request/response transport, and approval waiting states for the DeepOrca backend. Existing approval behavior for other runtimes is unchanged.

**Naming:** This document uses **DeepBox** for the host product and repository. The current command-line package and user-facing commands use **`agentbridge`**; the integration does not introduce another rename.

**Configuration extension:** [Web profile configuration](deeporca-profile-configuration.md)
supersedes the original template-only onboarding sections below. Add Agent and
Agent settings now configure endpoint/model/context window/reasoning effort.
The browser seals API keys to a Connector public key; only ciphertext is stored
on the Server. Plaintext credentials and native configuration writes remain on
the Connector. Profile/project/template identity stays immutable; LLM settings
have separately revisioned, idle-only updates.

**Reviewed baseline:** DeepBox `50699fa` and DeepOrca `dd6670d`, including the local working trees. The baseline analysis and examples explicitly marked *proposed* describe the design phase; the implementation map below identifies the shipped branch interfaces.

### Implementation reconciliation

- The public SDK is `deeporca.embedded`, with `EMBEDDED_API_VERSION = 1`, `ensure_profile(...)`, and `EmbeddedRuntime`. The Connector probes it in a disposable interpreter and hosts each Agent in its own spawned process; it does not start the native CLI or WebServer.
- Browser provisioning supports managed creation and [existing-profile binding](deeporca-existing-profiles.md) through Connector-advertised references. The latter explicitly requires native writers to be manually stopped; it does not claim a shared standalone/embedded lease. The only managed-create template reference is `connector-default`, resolved locally.
- Add agent, persistent **Agent settings** (name, model settings, readiness, refresh/retry), and opening a conversation are separate actions. Runtime/project/profile/template identity is immutable. The Server tracks desired/observed revisions; initialization and native data stay local.
- `connector/integrations/deeporca/` owns the probe, adapter, store, supervisor extension, worker, session and event projection. It implements enrollment isolation, durable admission/receipts, native session mapping, lifecycle, and canonical newline-terminated event records. A private per-spool ownership pin refuses unsafe enrollment changes while old output/controls/work remain. The inherited default spool filename still includes the token; token rotation preserves native identity but is not automatic spool migration. A forced stop or crash is uncertain, not an exactly-once execution or rollback guarantee.
- Output admission stops at 64 MiB / 10,000 pending frames, with bounded failure-record headroom. Affected native turns settle as uncertain rather than continuing with unrecorded success. Existing frames are not discarded. Full-disk failure can also prevent an emergency record; this is not a SQLite/WAL disk-quota guarantee.
- Compatible incremental text/thinking is coalesced within a 12ms / 16KiB window. Native identity and ordering are preserved, boundaries flush first, original-event bytes still count toward the turn budget, and the owned deadline task is joined before terminal results. A cancelled or failed callback, including cancellation while waiting for the writer lock, cannot silently become successful settlement.
- Inactive native conversations have an explicit operator-only **Continue native conversation** history action. Replay and layout restore never persist or imply continuation consent. The SDK's private context manifest rejects unrecoverable missing native context instead of silently creating an empty conversation; native history, not UI history, is the recovery source.
- `web/integrations/deeporca/` owns the pane-local renderer and runtime UI selected by `deeporca-chat-v1`. It uses the existing reducer, socket, recording and restore paths. Tools are correlated by turn plus provider tool ID; incomplete outcomes and truncated display previews are labeled. Other runtimes retain their existing renderer and approval behavior.
- Unknown renderer IDs produce a visible generic-fallback notice, never a descriptor-supplied script load. Inactive/replayed unfinished tools are labeled as having no recorded result rather than still running.
- Native DeepOrca defaults to `minimal`; `ensure_profile()` copies native defaults or trusted template security unchanged, and explicit profile security is respected. Embedded execution delegates to the unmodified native `SecurityManager` with no forced `standard`/`allowlist` floor for any profile mode. Existing files are not proactively rewritten, but existing `minimal` profiles are also honored at runtime. There is no security migration, creation opt-in/marker, extra Connector flag or separately versioned security-default API. Final `REVIEW` and `DENY` are refused, and unsupported background/autonomous tools stay unavailable. Binding and exclusivity requirements are unchanged. See [the exact security scope](deeporca-existing-profiles.md#security-policy-for-all-profiles).
- The SDK owns a minimal bootstrap built from native model/configuration, tool, skill, memory and persona components, plus `SessionManager`, `TurnEngine`, and `SessionRunCoordinator`; it does not initialize the standalone gateway or autonomous services. Provider failure events now settle as errors rather than successful completed turns. Embedded registries wait for synchronous-thread cleanup on cancellation, bounded externally by worker retirement; standalone defaults are unchanged. No model request is sent merely to advertise readiness.
- Verification includes real SDK + local fake OpenAI/SSE provider tests, real Server/Connector/WebSocket/recording integration, explicitly paused existing-profile binding, native-context recovery after worker restart, and Chromium workbench checks. Browser fixtures are not real-provider or deployment evidence. External provider authentication, production deployment, concurrent standalone/embedded writers, uploads, and autonomous execution remain outside this verification.

Implementation and test entry points: [operations and test commands](deeporca.md), `tests/test_deeporca_sdk_integration.py`, `tests/test_deeporca_e2e.py`, `tests/test_deeporca_browser.py`, and the Connector/Server unit and regression suites. The optional SDK/E2E tests require a local source checkout and use temporary profiles and loopback-only fake providers.

See the [v1 acceptance checklist](deeporca-design-checklist.md) for requirement-to-code/test mapping and the [validation report](deeporca-validation.md) for actual run results, commands and verification limits.

The [module and presentation follow-up](deeporca-module-layout.md) documents the
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
implemented [limited binding workflow](deeporca-existing-profiles.md) instead
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
