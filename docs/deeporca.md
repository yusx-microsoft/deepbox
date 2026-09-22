# Optional DeepOrca library integration

See the [validation report](deeporca-integration-design.md#validation-record) for tested branches,
reproducible commands, workbench screenshots and verification limits.

DeepOrca is an optional **local library runtime** for AgentBridge. The Connector
starts a private Python worker for each managed DeepOrca Agent; it does not run
the interactive DeepOrca CLI, start a DeepOrca gateway, or expose a DeepOrca HTTP
port. The AgentBridge server and browser do not need the DeepOrca package.

This guide describes the implemented v1 path, not every feature proposed in the
[integration design](deeporca-integration-design.md). It is not a claim of
external-provider verification, deployment validation, or production readiness.

## Supported scope

- Runtime ID `deeporca`, embedded API version `1`, renderer `deeporca-chat-v1`.
- Create a **new managed profile**, using a **registered Connector-local
  project** as its workspace, or **bind an existing native profile**. Binding
  requires native writers to be manually stopped and kept stopped; it is not chat
  import or a shared lease. See [existing profiles](#existing-profile-binding).
- Text chat, streamed text/reasoning and tool events, saved native conversation
  context, display replay, and Stop. One foreground turn per Agent/profile,
  including across that Agent's sessions.
- Only the `connector-default` configuration-template reference is supported.
  It resolves on the Connector, not in the browser or server. Model settings can
  be completed directly in **Add agent** and edited in **Agent settings**;
  preparing a local model template is optional.
- No interactive approval workflow, autonomous/background agent work, subagents,
  arbitrary SDK options, file attachments, or terminal-style raw input.

The integration does not install DeepOrca automatically and does not change the
installation requirements for other runtimes.

In a Machine's **Runtimes** list, DeepOrca appears alongside the other reported
runtimes, including when its SDK is missing. Its **Setup guide** links to
[aka.ms/deeporca](https://aka.ms/deeporca) through the existing
`installation.guidance.url` field; no install command or separate setup flow is
added. **Add agent** uses the single **Runtime** selector for installed runtimes;
adapters remain an internal implementation detail. After changing an installation,
reconnect the Connector, then refresh the reported status. The shared inventory
rows wrap on narrow screens rather than overlapping names and setup commands.

## 1. Install into the Connector's Python environment

First follow [Install AgentBridge](install.md). Install a DeepOrca checkout or
package that actually includes `deeporca.embedded` API v1 and its Python
dependencies into the **same interpreter that runs the Connector**. An unrelated
`deeporca` command on `PATH`, or installation into another virtual environment,
does not make this runtime available.

Browser profile configuration additionally needs native
`PROFILE_CONFIGURATION_API_VERSION = 1` and an updated Connector with the
`cryptography` dependency. A package version alone is not sufficient: editable
installs follow the checkout's current branch. Keep the embedded SDK in a
dedicated checkout/worktree if the ordinary development branch lacks these APIs.

For a local checkout, the following Windows CMD command is illustrative. Replace
both paths with your own; do not run it with the placeholder paths:

```bat
"C:\path\to\connector-venv\Scripts\python.exe" -m pip install -e "C:\path\to\deeporca-checkout"
```

Use the checkout's `pyproject.toml` dependencies, not just a copied `embedded.py`.
They include, among others, OpenAI/httpx, PyYAML, python-dotenv, MCP, Playwright,
and the other DeepOrca runtime dependencies. AgentBridge's Connector requirements
alone do not install all of these. The SDK declares Python >=3.10; also satisfy
AgentBridge's own interpreter requirements.

For local development, an optional environment variable selects a checkout:

```bat
set "AGENTBRIDGE_DEEPORCA_SOURCE=C:\path\to\deeporca-checkout"
```

Use an absolute directory containing the `deeporca` package. It changes
the module source used by the probe/worker; it does **not** install dependencies
or choose a different Python interpreter. Treat this checkout as trusted code.
Set it in the environment of the process that launches the Connector. A service
or existing background process will not inherit a later `set` in another shell;
restart it with the intended environment.

### Check installation separately from enrollment and authentication

`agentbridge doctor` checks AgentBridge server reachability, credentials and
enrollment. At present, it does **not** run the DeepOrca embedded-API probe and is
not a DeepOrca compatibility or provider-authentication check. It can contact your
configured AgentBridge server; it is not an offline SDK test.

To run the actual local runtime probe from an AgentBridge checkout, use that same
Connector interpreter and environment:

```bat
"C:\path\to\connector-venv\Scripts\python.exe" -c "import json; from connector.runtime_probe import probe_family; print(json.dumps(probe_family('deeporca'), indent=2))"
```

Expect `installation.status: installed`, `compatibility.status: compatible`, and
an available `structured` surface. The probe imports `deeporca.embedded` in a
disposable subprocess of `sys.executable` and checks that `EMBEDDED_API_VERSION`
is the integer `1`. It reports the package version when available; this is not
the same thing as the embedded API version. It does not create a profile, start
the full runtime, exhaustively check its dependencies/methods, or make a model
request. Source paths and raw import errors are not sent as capability diagnostics.

The running Connector advertises a `deeporca` capability descriptor even when
unavailable; successful probing enables its surface. The descriptor includes
`backend: python-library`, `agent_config.profile_modes: [create, bind]` when the
SDK supports existing mode, its bounded profile inventory, template reference
`connector-default`, and unknown authentication. Reconnect/restart
after changing the package or environment so the browser receives fresh
capabilities. This is a local import/API-version gate, not proof that a particular
profile can start, all runtime dependencies are installed, or credentials work.

## 2. Optional trusted local defaults

**For ordinary setup, skip to [Add an Agent](#3-add-an-agent-in-the-browser).**
Endpoint, model, context window, reasoning effort and API key can all be supplied
in the DeepOrca form. No hand-written profile or model template is required.
This section is for operators who want local security/MCP defaults or an existing
locally configured provider. Browser setup currently supports OpenAI-compatible
endpoints; it does not implement Copilot's separate device-login flow.

Optionally set a Connector-local template directory before creating an Agent:

```bat
set "AGENTBRIDGE_DEEPORCA_TEMPLATE_DIR=C:\path\to\private\deeporca-template"
```

Use an absolute, existing directory. It is a **trusted local configuration
input**, not an upload location or a browser-supplied path. Restrict access with
the operating system's file permissions/ACLs. MCP configuration can start local
processes or connect to services; do not copy unreviewed configuration.

The SDK copies only these top-level configuration files from the template when
creating a new managed profile:

```text
deeporca-template/
  config.yaml
  .env
  security.yaml     (optional)
  mcp.yaml          (optional; trusted tool/server configuration)
```

Missing files fall back to the SDK's bundled defaults. It does not copy another
profile's persona, history, memory, sessions, token caches, or arbitrary files.
Copied configuration files must be ordinary files, not symlinks/junctions.
Without an explicit model and valid context-window configuration, a profile may
be created successfully but remain `needs_configuration`.

### Placeholder configuration example

These are **placeholders, not working provider settings** for the optional local
template. Do not commit credentials. In the browser, use only the dedicated
password-style API-key field, never a URL, model name or general text field.

`config.yaml`:

```yaml
llm:
  provider: openai
  model: "REPLACE_WITH_MODEL_ID"
  base_url: "https://provider.example.invalid/v1"
  api_key: "${LLM_API_KEY}"
  context_window: REPLACE_WITH_POSITIVE_INTEGER
```

Replace `context_window` with an unquoted positive integer token limit appropriate
to your chosen model. It is required in `config.yaml` under `llm`; there is no
environment-variable fallback for it. The placeholder string above is deliberately
not a valid context window. The current configuration reader accepts provider
identifiers `openai` and `copilot`; this example uses the OpenAI-compatible format
and does not attest to any particular provider/model combination.

`.env`:

```dotenv
LLM_API_KEY=REPLACE_WITH_LOCAL_API_KEY
```

The SDK keeps the API key in the local `.env`; other LLM settings belong in the
YAML `llm` block. That block takes precedence over legacy flat `LLM_*` settings.
Copilot authorization, if used, must be established locally; AgentBridge has no
device-login/token-import flow. The template copy is not a migration of a
standalone profile's credentials or cached login.

Native DeepOrca defaults to **minimal**. Managed creation copies the native
defaults or trusted template security configuration unchanged; explicit security
settings are respected. The embedded runtime adds no `standard`/`allowlist` floor.
Minimal permits broader tool execution; the following optional `security.yaml`
explicitly chooses a conservative policy, rather than an embedded requirement:

```yaml
level: standard
approval:
  mode: allowlist
```

Review DeepOrca's policy settings for your tools and workspace. Do not weaken
policy merely to make unattended actions succeed. This is not an OS sandbox.

### Profile names and one-time template copying

The browser supplies an Agent display name, not a filesystem profile name.
AgentBridge generates a stable SDK-safe name such as
`dbx-0123456789abcdef01234567`. At the SDK boundary, safe names contain 1–64 ASCII
letters, digits, `_` or `-`, start with a letter/digit, and are not reserved Windows
device names. `demo_agent-1` is a syntactically safe example; `../agent`,
`C:\agent`, `agent.name`, and `CON` are not. There is no browser field for choosing
one of these native names or adopting an existing profile.

Profile creation is idempotent for a completed SDK-managed profile. Existing
unmanaged/incomplete directories are not silently adopted. The configuration
template is copied **once**, not reapplied on every retry. To repair an already
created profile's model configuration, use **Agent settings**. Explicit browser
settings are applied locally while the SDK owns the profile lock; they do not
replace native memory, sessions, security or MCP configuration. Merely editing
the template does not repair existing profiles. Do not delete ownership markers
or manipulate the binding database to bypass a conflict.

## 3. Add an Agent in the browser

For an existing native profile, select **Bind an existing profile** and follow
the [native-stop and inventory workflow](#operator-flow).
Its model/security configuration is reused, not edited here. The following steps
describe creating a new managed profile with complete model configuration.

1. On the Connector machine, register the workspace directory:

   ```bat
   agentbridge project add "C:\path\to\workspace" --name "Example project"
   ```

2. Connect the Connector with the intended SDK/template environment. In the
   browser, choose the Machine, open **Add agent**, and select **DeepOrca**.
3. Select a registered local project. If no project appears, register one on
   that Machine and use **Refresh projects**. A private managed profile is created
   automatically; there is no separate profile-creation step.
4. Complete the DeepOrca-only model fields:
   - **LLM endpoint**: the provider's HTTP(S) base URL, normally including `/v1`.
     Loopback means the **Connector machine**, not the browser. Private/local
     endpoints are supported; URL credentials, query strings and fragments are not.
   - **Model**: the exact provider identifier; names containing `/` or `:` are
     supported. No provider model-discovery request is made.
   - **Authentication**: enter an API key or explicitly choose no authentication
     for a keyless local service.
   - **Context window**: a positive integer token count supported by the model.
   - **Reasoning effort**: Provider default, none, minimal, low, medium, high,
     xhigh or max. These are native options, not a promise every model supports them.
5. Add the Agent and inspect its runtime status. Creation records desired state;
   it does not mean that provisioning, credentials, or a model call succeeded.

The API-key path requires browser WebCrypto (HTTPS or a secure localhost origin)
and the Connector's advertised public key. It fails closed if either is absent;
there is no plaintext fallback. Other runtimes never display these fields.

The Add-agent payload uses a project ID and this allowlisted configuration:

```json
{
  "integration_version": 1,
  "profile": {
    "mode": "create",
    "configuration_template_ref": "connector-default"
  },
  "llm": {
    "provider": "openai",
    "base_url": "http://127.0.0.1:1234/v1",
    "model": "vendor/model:latest",
    "context_window": 32768,
    "reasoning_effort": ""
  },
  "credential": {"mode": "none"}
}
```

This is a keyless endpoint example, not a working provider recommendation.
For API-key authentication, `credential` instead contains an opaque sealed
envelope; the Server never accepts a plaintext key field. It does not accept
arbitrary work/home/template/checkout paths, commands, environment variables, or
free-form existing-profile names/paths. `profile.mode: bind` uses the separate
[existing-profile reference protocol](#existing-profile-binding), without
model/credential overrides. Legacy template-only configurations remain supported.

### Editing model settings

For managed-create mode, use **Agent settings** to fill an unconfigured profile or change its
model parameters. Keep the API-key field empty to retain the existing credential;
keys are never returned for display. Changing the endpoint requires a replacement
key or an explicit no-authentication choice. Close active conversations before
saving changed model settings; saving never silently interrupts a model/tool turn.
Profile/project identity and native history remain unchanged. A new desired
revision returns to pending until the Connector applies it; a stale ready report
cannot satisfy the new revision. Renaming alone does not restart the runtime.
For a legacy profile whose settings were only configured locally, the first web
configuration requires re-entering its key (or choosing no authentication): the
browser cannot safely assume its previously private endpoint. Legacy top-level
model overrides remain fixed; ordinary web-configured model IDs are editable.

See [Web profile configuration](#web-profile-configuration) for the sealed
credential protocol, ownership/crash boundaries and verification scope.

Native home/workspace paths and plaintext credentials remain Connector-local.
The Server stores declarative model settings and encrypted credential envelopes;
the browser necessarily handles a key while the operator enters it. A public
encryption key, never the private key, is advertised with runtime capabilities.
Conversation text and
tool outputs **do** travel through AgentBridge for display/replay and to the
configured provider as part of normal model use. They are not a general-purpose
secret-redaction channel: do not ask a tool to print credential files.

### What readiness and approvals mean

- `ready` means the local managed runtime started with usable configuration.
  Provider authentication remains **unverified**: no paid/authenticated model
  probe is performed as a readiness check. An expired key or unavailable model
  can still fail on the first turn.
- There is **no Approve button, pending-approval queue, or approval response
  protocol**. The embedded host evaluates DeepOrca's real security policy and
  immediately denies a final `REVIEW` verdict that would require human
  confirmation. Existing `DENY` decisions remain denied; ordinary permitted
  actions can run without a prompt.
- **Native security defaults to `minimal` for all profile modes.** Explicit
  security configuration is respected by the unmodified native `SecurityManager`;
  the embedded runtime does not force `standard` or `allowlist`. Existing files
  are not proactively rewritten, but existing managed and bound profiles using
  `minimal` now also receive that policy at runtime, not the former embedded floor.
  There is no security migration, new-profile-only opt-in, creation marker, extra
  Connector flag or separately versioned security-default API. `NonInteractivePolicy`
  still rejects final `REVIEW`/`DENY` and unsupported background/autonomous tools.
  Binding and exclusive ownership requirements are unchanged. See
  [security scope](#security-policy-for-all-profiles).
- Autonomous work, schedules/goals, background command sessions, subagents and
  live skill-catalog mutation are not supported in this host. Background command
  requests are denied. An SDK approval event is treated as a contract error,
  not shown as an actionable approval request.

## Sessions, Stop, recovery, and retained data

### Native context is different from display replay

Each AgentBridge session has a persisted mapping to a native DeepOrca session.
DeepOrca saves model context in its managed profile; continuing the same mapped
session can therefore resume context after a worker/Connector restart.
AgentBridge's normalized text/tool records and bounded native-event records
serve live display and read-only saved history, without a recording player. They
are **not** re-fed to the model to reconstruct context, and a displayed transcript
is not proof that native context was saved.
Large tool results can be displayed as labeled previews without truncating the
SDK's own model context. A native `done` event alone is not a durable commit marker.

After a normal Connector restart, an operator can choose **Session history →
Continue native conversation** for an inactive DeepOrca conversation. This uses
the same authorized conversation/native mapping with a new worker/output epoch.
The browser sends an explicit `resume` request with the expected `launch_id`;
after authorization and generation checks, the Server routes it to the library
session's `open` operation, not a CLI native-context adoption or writer lease.
DeepOrca continuation is not offered for ended conversations or viewers. Supported
CLI runtimes retain their separate native-writer/resume lifecycle. Ordinary
View history, opening an inactive conversation, and workspace-layout restore remain
read-only: the one-shot continuation choice is not saved in layout state.

Termination advances the Server's lifecycle generation immediately. A late exit
or input acknowledgment from the previous generation cannot revive the session.
Its stale-control rejection does not stop the Connector or acknowledge pending
durable output; those rows still require their exact output acknowledgment.

A private `.embedded-contexts.json` manifest outside native `chat/` records known
conversations. If one cannot be recovered from native files/history, it fails
with `native_context_unavailable` before another model/tool call. A new
conversation remains possible when only an older conversation's files were
lost; a missing/corrupt manifest requires local repair or a new managed profile.
Keep the manifest in backups. Pre-manifest development profiles are not migrated
automatically. Its checksum detects accidental corruption, not malicious edits
or a valid rollback.

### One active turn and duplicate receipts

One worker owns one Agent/profile and serializes its foreground work. A turn in
another session of the same Agent returns `runtime_busy`; there is no parallel
execution or automatic queue for that profile. Different managed Agents have separate
workers. Do not open a managed profile in the standalone DeepOrca CLI/gateway:
those entry points do not cooperate with the embedded host's ownership lock.

The Connector durably records `client_input_id` receipts. A replay with the same
ID is acknowledged from the stored receipt rather than blindly re-executed.
An accepted receipt is admission, not successful completion. A Connector crash
with an admitted/dispatched input can leave it `uncertain` on recovery; replay
then returns `execution_uncertain` instead of silently running it again. There is no
exactly-once guarantee across provider requests, tools, native-context saves and
AgentBridge's database. A new input ID represents a new request and can repeat
side effects.

The browser generates UUID receipt IDs even on ordinary LAN HTTP without
`crypto.randomUUID`. Correlated rejection clears optimistic pending state and
keeps the draft; it never automatically retries. A disconnect before
acknowledgment keeps the draft with an unconfirmed-delivery warning. Check
restored history and possible effects before explicitly resubmitting. An
in-memory draft is not a durable browser outbox.

### Output pressure

Native admission/output have a 64 MiB / 10,000 pending-frame high-water limit on
the shared spool, plus a bounded 64 KiB / 32-frame reserve for failure/terminal
records. At high water, new native inputs are rejected before admission. A turn
whose output cannot be recorded is cancelled/retired and settled as uncertain,
not completed. Records are not dropped; CLI runtimes retain their existing
spool policy. The byte limit counts encoded records, not SQLite/WAL disk size.
A full disk can prevent even the emergency notice from being saved.

### Stop is cancellation, not rollback

Stop requests interruption of the active turn. The Connector waits up to about
10 seconds for cooperative settlement, then retires/terminates an unresponsive
worker (`interrupt_timeout`); process cleanup adds a short delay. Stop can return
while cancellation is still settling. Worker loss or a forced stop may leave the
turn result, persistence, or external effects uncertain.

Already written files, started external work, provider charges and completed tool
effects are **not rolled back**. Cancellation is not an OS-wide process sandbox
or a guarantee that every descendant/external operation stopped. Inspect the
workspace/provider state before deciding whether to submit a new request. A
later reconciliation can start a fresh worker, but must not automatically replay
an ambiguous turn.

Embedded tool registries wait for actual synchronous-thread cleanup on
cancellation instead of detaching a still-running `to_thread` handler. This can
outlast a soft tool timeout; the worker's outer grace period is the hard bound.
Standalone registry cancellation defaults are unchanged.

### Enrollment and deletion

Connector-local `deeporca.sqlite3` sits beside its local project-state database.
Managed profiles are under the adjacent
`deeporca-native/<namespace-hash>/agents/<generated-profile-name>/` tree.
Bindings, native session IDs and receipts are scoped by an enrollment
namespace derived from the normalized AgentBridge server URL and Machine/devbox
ID, not by the Agent display name or enrollment token. Reconnecting to the same
identity reuses the bindings. Changing server identity or re-enrolling as another
Machine selects another namespace; it does not automatically adopt the old
profiles/context. Recreate/rebind operations by manually editing SQLite are not
a supported migration path.

A private per-spool ownership pin also prevents queued native output from going
to another enrollment. A reused spool with pending output, old queued controls,
or active workers/reconciliation fails closed with `enrollment_identity_conflict`.
No old frames or profiles are deleted. Generic pending frames on a native-used
shared spool conservatively block rebind too; CLI-only connectors create no pin.

Drain/ACK the old enrollment's output and close its workers before rebinding, or
use a separate OS-user/private Connector state **and** spool root while retaining
the old files for recovery. Changing only the project registry's `--state-path`
does not relocate the spool. On Windows, the normal root is under
`%LOCALAPPDATA%\deepbox`: a command-local, private `LOCALAPPDATA` in a separate
shell can isolate another enrollment. Unix uses `XDG_STATE_HOME`. Re-register
projects in a fresh registry; do not copy pending frames to another enrollment.

The inherited default spool filename includes Server URL and token hashes.
Token rotation preserves native profile/session identity but can select another
spool file: drain pending output first or deliberately retain/recover the old
spool. Native identity continuity is not automatic spool migration.

Deleting an Agent retires its worker and local binding; it **does not erase the
native profile, memory, history, credentials, or workspace data**. Recreating an
Agent with the same display name does not recover the old identity. Retained
native data needs deliberate local backup/cleanup by its owner, with the
Connector stopped and the relevant profile identified. Do not share raw database
or profile backups as diagnostics: they contain private local information.

## Troubleshooting

| Symptom | Check/action |
| --- | --- |
| DeepOrca is absent from Add agent | Run the local probe with the exact Connector interpreter. Install SDK dependencies there; check the optional absolute checkout path and API v1. Reconnect for fresh capabilities. `doctor` alone does not check this. |
| `needs_configuration` / `configuration_required` | Configure the managed profile locally: explicit model, supported provider, positive integer YAML context window. Keep credentials local. For an existing profile, changing only the template is insufficient. Stop/restart or reconnect after repair. |
| `pending` / `provisioning` / stale status | Keep the owning Connector online. Reopen **Agent settings** from the DeepOrca Agent menu; **Refresh status** reloads the observed state without creating a new Agent or copying the template again. Reconnect to trigger desired-state reconciliation. |
| Connector is offline | Local provisioning cannot run while it is disconnected. An HTTP Agent row or old `ready` state is not evidence of a live worker. A retry request can return pending while offline; execution still requires the owning Connector to reconnect. |
| Retry after local repair | Reconnect/restart the Connector as needed. Owners/admins can reopen **Agent settings** from the DeepOrca Agent menu and choose **Retry initialization** for `needs_configuration` or `error`. This calls `POST /api/agents/{agent_id}/runtime/retry` without a configuration body and requests reconciliation of the same Agent; pending is not proof of success, and no profile is rotated or binding changed. **Refresh status** only reloads the display. Settings also permit a display-name rename; the handle, runtime, profile mode, project and template remain fixed. |
| `invalid_local_runtime_configuration` | Check that source/template environment values resolve to existing directories in the Connector's environment; prefer absolute paths. No browser-supplied path can fix this. |
| `local_project_unavailable` / missing project | Register a real directory on this Machine, refresh inventory and select that project. Moving/deleting the directory or changing a bound workspace is not transparent migration. |
| `binding_identity_conflict` or profile startup failure | Check that the same managed identity/workspace is being reused and no other process owns the profile. Do not overwrite an unmanaged directory or force adoption. Generic `startup_failed` can conceal a more specific SDK failure. |
| `runtime_busy` | Wait for the other session's turn on this Agent or stop that turn. Opening another pane is not another worker. |
| `worker_lost`, `worker_closed`, `interrupt_timeout`, `execution_uncertain` | Treat any in-flight turn as potentially partially executed. Inspect effects before resubmitting. Reconcile/restart the worker; do not assume a new message ID makes replay safe. |
| `native_context_unavailable` | Restore the correct private native backup/manifest or explicitly start a new conversation/profile as appropriate. Display replay cannot repair model context. |
| `output_unavailable` | Drain/ACK output, inspect local storage and free capacity safely. An affected turn may be uncertain; never blindly repeat it. |
| `enrollment_identity_conflict` | Drain old output/controls and close workers, or use a fresh private state and spool root. Keep the old files for recovery; do not transplant queued output to the new enrollment. |
| `ready`, but a turn fails | Readiness is not provider authentication. Check the local provider/key/model/network setup without exposing secrets. Provider failures are sanitized to generic errors; child stdout/stderr are discarded, not relayed as a diagnostic log. |
| Tool requires approval / `approval_contract_violation` | There is no remote confirmation flow. Review local policy/tool choice. An emitted approval request indicates an incompatible SDK contract and retires the worker. |

Do not collect `.env`, token caches, raw provider exceptions or whole native
profiles in a bug report. Prefer the stable status/error code, interpreter/SDK
version, and a minimal synthetic reproduction.

## Local regression checks

From the AgentBridge repository root, with its test dependencies already
available in the selected Python environment, these are the actual test files:

```bat
python -m pytest -q tests/test_deeporca_runtime.py tests/test_deeporca_server.py tests/test_deeporca_configuration.py tests/test_deeporca_web.py
```

They cover the shared contract/probe, desired/observed state, local bindings and
receipts, worker lifecycle, session events and browser integration. The browser
tests use Node.js and skip when it is unavailable; the browser-level test also
needs Playwright and its locally installed Chromium, otherwise it skips.
Historical prototypes, their standalone test and screenshots are local-only
artifacts, excluded from Git; they do not validate the implemented runtime.

From the DeepOrca SDK checkout root, its real embedded-host regression file is:

```bat
set "DEEPORCA_TEST_OFFLINE=1"
python -m pytest -q tests/test_embedded.py
```

Those SDK tests use temporary profiles and fake provider behavior; they exercise
real bootstrap, security, persistence, locking and cancellation without a live
provider login. Test results are not evidence of external-provider compatibility,
deployment success, or production readiness.

Runtime-specific implementation lives under `agentbridge/integrations/deeporca/`
(path-free contract), `connector/integrations/deeporca/` (probe, adapter,
bindings, worker, sessions and events), `server/app/integrations/deeporca/`
(control-plane policy), and `web/integrations/deeporca/` (renderer and runtime UI).
The platform owns authorization, transport, spool/ACK, recording and pane lifetime.
The SDK entry points remain `deeporca/embedded.py` and `deeporca/llm_config.py`.
See [module layout and presentation](deeporca-integration-design.md#module-boundaries-and-presentation) for the package
boundaries, native-style workbench screenshots and source-checkout restart notes.

---

## Existing-profile binding

This small extension adds existing-profile selection to the DeepOrca-only
workbench form. It uses the native SDK's `profile_mode="existing"`; it is not an
importer, a profile migration, a shared native/Connector lease system or a way to
launch a standalone DeepOrca server.

### Operator flow

1. Update/restart the Connector with a native SDK supporting existing profiles.
2. **Stop native DeepOrca for the chosen profile.** Keep all native writers for
   that profile stopped for the entire time the Connector owns it. Closing a
   browser tab or ending a conversation is not equivalent to stopping the writer.
3. In **Add agent**, choose DeepOrca and a registered local project, then choose
   **Bind an existing profile**. Select a profile from this Connector's inventory
   and explicitly confirm the native-stop condition.
4. Add the Agent. Readiness follows normal pending/provisioning/ready/error
   reporting. Creation is not a guarantee that the profile is free or configured.
5. Open a new AgentBridge conversation. Existing persona, memory, skills, tools and
   model configuration are reused. **Native chat history is not automatically
   imported into AgentBridge conversations.**

The Connector discovers direct profile directories in the SDK's local
`get_deeporca_dir()/agents` location. A local operator can select another native
home through `AGENTBRIDGE_DEEPORCA_HOME` (legacy `DEEPBOX_DEEPORCA_HOME`). This is
the home containing `agents/`, not an individual profile directory. Paths never
come from browser input. Reconnect to refresh inventory after creating/renaming
a native profile. The bounded catalog lists at most 100 eligible profile names.

An older SDK/Connector does not advertise binding. No arbitrary directory or
profile-name text box replaces a missing catalog. Unknown/stale references fail
closed locally even if the Server's last advertised inventory is stale.

### Identity and ownership

The browser sends only an opaque, stable Connector reference and consent:

```json
{
  "integration_version": 1,
  "profile": {
    "mode": "bind",
    "profile_ref": "native-0123456789abcdef0123456789abcdef",
    "native_stopped": true
  }
}
```

The reference is derived from the canonical local profile path; it is an
identifier, not a bearer credential. Public inventory includes only reference and
label, not home paths, credentials or configuration files. Server creation checks
ordinary workspace authorization, the registered project and that the reference
was advertised by the **target** Machine. The Connector independently resolves
the reference and pins its local home/name in the existing binding store.

Mode, profile selection and project remain immutable. Bind mode rejects `llm`,
`credential`, legacy `model`, template references and caller paths. Settings show
the binding/status and allow rename/retry, not profile configuration changes.
Managed-create mode retains its complete editable model form. Selecting a bind
does not forward hidden managed-form credentials or configuration drafts.

The worker skips `ensure_profile()` and calls:

```python
runtime = EmbeddedRuntime(name, workspace, home=local_home, profile_mode="existing")
await runtime.start()  # no configuration= argument
```

The native SDK owns acquisition and validation. It refuses known live native PIDs,
invalid/incomplete profiles and conflicting embedded owners. A second active
binding cannot claim the same profile. Stopping/retiring a binding never deletes
the native profile. Configuration, `.state.json`, persona and original native
chats are not copied, reset or reconfigured by binding; permitted embedded
context/session bookkeeping still writes its own local state.

The authoritative Agent directory also retires reservations for Agents deleted
while the Connector was offline. Retirement is scoped to the current enrollment
and waits for any associated worker's confirmed exit; it never deletes native
profile files or history. An uncertain stop keeps the reservation in place.

Startup is not globally read-only: the SDK can create derived indexes/runtime
directories and connect configured MCP servers. Authorized turns and tools can
write new memory/history. Closing a workbench tab or merely losing the Server
connection does not establish that the profile's worker has stopped.

**Important limitation:** the SDK's existing-profile implementation assumes native
writers were manually paused. Its PID checks and embedded ownership lock are
not a universal standalone/embedded locking guarantee and do not prevent a native
writer from being started later. The checkbox records this operational condition;
it is not a tool approval or permission to run both applications concurrently.

### Security policy for all profiles

**Native DeepOrca defaults to `minimal`.** Managed creation uses `ensure_profile()`
to copy the native defaults or trusted Connector template, including its security
configuration, unchanged. Explicit native/template/profile security settings are
respected. The embedded runtime delegates to the unmodified native
`SecurityManager`; it imposes no `standard` level or `allowlist` approval floor.

This applies to new and existing managed profiles and bound native profiles.
Existing profile files are not proactively rewritten on upgrade, retry or binding,
but an existing `minimal` configuration is now also honored at runtime rather
than promoted to `standard`. Preserving files does **not** preserve the former
embedded policy floor. There is no security migration, new-profile-only opt-in,
creation marker, extra Connector flag or separately versioned security-default API.

**Minimal permits broader tool execution**; use it only on a trusted
Connector/workspace. The non-interactive boundary is unchanged:
`NonInteractivePolicy` rejects final native `REVIEW` and `DENY` verdicts and
unsupported background/autonomous tools. No approval UI, transport, waiting or
auto-approval is added. Other runtime adapters and native application defaults
are unchanged. Binding still requires native writers to remain manually stopped
and retains the same exclusive embedded ownership checks described above.

See [operator setup](deeporca.md), [managed model configuration](#web-profile-configuration)
and [validation](deeporca-integration-design.md#validation-record) for their separate guarantees and evidence.

---

## Web profile configuration

This is the configuration extension to the Connector-local embedded integration.
It supersedes the initial template-only onboarding flow, not its ownership,
authorization, native-context or non-interactive tool-policy boundaries. See
[operator setup](deeporca.md) and [module boundaries](deeporca-integration-design.md#module-boundaries-and-presentation).

This editable form applies to **managed-create** mode. The separate
[existing-profile binding](#existing-profile-binding) flow reuses local
configuration without sending `llm`/`credential` or writing model settings.

Native DeepOrca defaults to `minimal` security. Managed creation copies native
defaults or trusted template security unchanged; explicit security configuration
is respected. The embedded runtime delegates to the unmodified native
`SecurityManager`, without a forced `standard`/`allowlist` floor. Existing files
are not proactively rewritten, but existing managed and bound profiles configured
as `minimal` now also receive that native policy at runtime. There is no security
migration, new-profile-only opt-in, creation marker, extra Connector flag or
separately versioned security-default API. `NonInteractivePolicy` still rejects
final `REVIEW`/`DENY` and unsupported background/autonomous tools. Model settings
do not change this security boundary or the existing binding/exclusivity rules;
see [security scope](#security-policy-for-all-profiles).

### Experience and configuration contract

Only selecting DeepOrca displays the model-connection and model-behavior fields.
Other runtime forms/payloads remain unchanged. The operator supplies an
OpenAI-compatible HTTP(S) base URL, exact model identifier, positive integer
context-window token count, and native reasoning effort (empty string means
provider default). Authentication is a dedicated password field or explicit
keyless mode; settings also allow retaining the existing credential. No model,
window size, successful authentication or paid verification is inferred.

Native efforts are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, plus
provider default. Support depends on the selected model; the form does not call
external model catalogs. Slashes/colons in model names are supported. Endpoint
URLs cannot contain userinfo, query parameters or fragments. Loopback/private
addresses are intentional: the Connector, not the Server/browser, accesses them.
Plain HTTP does not protect the eventual Connector-to-provider request; prefer
HTTPS outside a trusted local endpoint.

`runtime_config.llm` contains only `provider`, `base_url`, `model`,
`context_window`, `reasoning_effort`; web configuration currently selects
`provider: openai`. This is a protocol dialect, not an OpenAI-hosted-only feature.
Copilot device login, arbitrary provider credentials and browser-specified
executables/SDK paths/environment variables remain outside this form's scope.
Existing template-only/native Copilot bindings still follow their previous path.

At the API level, `runtime_config.credential` is absent to retain local/template credentials,
`{mode: "none"}` to clear authentication, or a bounded opaque sealed envelope.
The Server rejects plaintext key fields even when nested in `llm` or `credential`.
An existing Agent's omitted credential is merged with its stored desired
credential, rather than accidentally clearing it. Changing the endpoint cannot
reuse a previous sealed envelope; replace the key or explicitly select no auth.
Local/template key retention is also checked at the SDK boundary to avoid
forwarding a key to a different endpoint.
The creation form deliberately requires an explicit key/keyless choice, rather
than assuming a working template. A legacy locally configured Agent needs its
key re-entered on first web setup because its previous endpoint is not known to
the browser. An untouched unconfigured Agent can still be renamed without setup.

### Sealed credential protocol (version 1)

The Connector owns a persistent machine-local RSA-2048 private key. Its public
key is advertised under `agent_config.credential_key`:

- `version`: integer `1`;
- `algorithm`: `RSA-OAEP-256+A256GCM`;
- `key_id`: lowercase SHA-256 of DER SubjectPublicKeyInfo;
- `public_key`: encrypt-only RSA JWK, base64url modulus/exponent.

For each submission the browser generates a new 32-byte AES key and 12-byte IV.
It encrypts the UTF-8 API key with AES-256-GCM, including its 16-byte tag, and
wraps the AES key with RSA-OAEP/SHA-256 (empty label). The authenticated data is
the exact UTF-8 concatenation:

```text
agentbridge/deeporca/credential/v1 + NUL + key_id + NUL + llm.base_url
```

The envelope has exactly `mode: sealed`, `key_id`, `wrapped_key`, `iv`,
`ciphertext`. The last three use canonical standard base64. Limits are 256
wrapped-key bytes, 12 IV bytes and 17–4112 ciphertext bytes (1–4096 key bytes
plus tag). Keys must satisfy the local environment-file safety policy: no
control/whitespace or environment interpolation. The browser needs WebCrypto
on HTTPS/secure localhost; no plaintext or insecure-JavaScript crypto fallback
exists. An encrypted envelope is durable desired state, so offline provisioning
and retries do not require plaintext Server storage or browser resend.

Only the worker decrypts, using a machine-selected path, never a browser path.
The private key and its fingerprint pin are persisted with private filesystem
permissions; losing/corrupting them fails closed instead of silently rotating
the key. Back them up together with Connector state. A public-key replacement
requires re-entering keys for old sealed configurations. No key is returned in
settings, put into localStorage, added to output recordings, or placed in native
worker diagnostics by this configuration path.

**Threat boundary:** this protects passive Server storage/logs/backups from raw
provider keys. It does not protect against a compromised browser, an actively
malicious Server substituting JavaScript/public keys, a compromised Connector,
or tools deliberately reading credential files. Machine/workspace operators
remain trusted to select providers and execute local tools. The public-key trust
chain is the existing authenticated Server/Connector transport, not out-of-band
key attestation. Envelopes are endpoint-bound, not single-use authorizations.

### Application and updates

Native `deeporca.embedded` exports the additive capability
`PROFILE_CONFIGURATION_API_VERSION = 1`; embedded API v1 remains compatible.
For a web-configured binding the worker calls:

```python
await runtime.start(configuration={
    "provider": "openai",
    "base_url": "http://127.0.0.1:1234/v1",
    "model": "example/model:latest",
    "context_window": 32768,
    "reasoning_effort": "",
    "api_key": "",  # explicit keyless mode; omission means retain
})
```

The SDK validates and applies settings **after acquiring profile ownership** and
before bootstrap reads configuration. LLM fields go in `config.yaml`; the API
key stays in the profile's `.env`, referenced through `${LLM_API_KEY}`. Applying
provider default also clears legacy reasoning-effort fallback. Unrelated YAML,
environment variables, memory, sessions and policy files are preserved. Private
atomic writes and a recovery marker prevent booting a partially applied
credential/endpoint pair. No provider request is made while saving or checking
readiness. Missing configuration support yields `configuration_api_unavailable`,
not a fallback write outside the SDK's profile lock.

The desired revision includes LLM settings and the sealed credential. Immutable
binding identity excludes those two fields but still includes Agent/project,
profile/template and legacy top-level model override. Server settings updates
retain ordinary authorization, refuse identity changes and reject changed model
settings while a conversation is active. Rename/no-op saves do not reinitialize
the Agent. Connector reconciliation independently defends against active work,
retires the old idle worker, updates the same binding, and starts with the new
desired revision. Native context/receipts are not deleted or replayed as inputs.

Offline updates remain pending; stale ready reports never satisfy a newer
revision. A save response acknowledges desired state, not successful SDK startup,
external-provider authentication, or a completed model request.

### Verification

Shared contract, Server authorization/update, Connector crypto/lifecycle, native
profile ownership/crash-safety, Node form/encryption, and Chromium interaction
tests exercise separate boundaries. Opt-in real SDK E2E uses only an owned
loopback scripted provider and temporary accounts/profiles/databases. Refer to
[validation evidence](deeporca-integration-design.md#validation-record) for completed run results. Fixture
screenshots and import/readiness probes are not provider-authentication evidence.

Updating source does not reload existing Server/Connector processes. Restart
both during an appropriate idle window to load the new capability and writer.
No Server reset, new account, project recreation or Token rotation is needed.
