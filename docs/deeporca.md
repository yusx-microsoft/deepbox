# Optional DeepOrca library integration

See the [validation report](deeporca-validation.md) for tested branches,
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
  import or a shared lease. See [existing profiles](deeporca-existing-profiles.md).
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
the [native-stop and inventory workflow](deeporca-existing-profiles.md#operator-flow).
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
[existing-profile reference protocol](deeporca-existing-profiles.md), without
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

See [Web profile configuration](deeporca-profile-configuration.md) for the sealed
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
  [security scope](deeporca-existing-profiles.md#security-policy-for-all-profiles).
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
serve browser display/replay. They are **not** re-fed to the model to reconstruct
context, and a replayed transcript is not proof that native context was saved.
Large tool results can be displayed as labeled previews without truncating the
SDK's own model context. A native `done` event alone is not a durable commit marker.

After a normal Connector restart, an operator can choose **Session history →
Continue native conversation** for an inactive DeepOrca conversation. This uses
the same authorized conversation/native mapping with a new worker/output epoch.
It is not offered for ended conversations, other runtimes or viewers. Ordinary
Replay, opening an inactive conversation, and workspace-layout restore remain
read-only: the one-shot continuation choice is not saved in layout state.

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
python -m pytest -q tests/test_deeporca_runtime.py tests/test_deeporca_routes.py tests/test_deeporca_supervisor.py tests/test_deeporca_worker.py tests/test_deeporca_session.py tests/test_deeporca_web.py
```

They cover the shared contract/probe, desired/observed state, local bindings and
receipts, worker lifecycle, session events and browser integration. The browser
tests use Node.js and skip when it is unavailable; the browser-level test also
needs Playwright and its locally installed Chromium, otherwise it skips. The
historical UI prototype has a separate check; passing it does not validate the
implemented runtime:

```bat
python -m pytest -q tests/test_deeporca_prototype.py
```

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
See [module layout and presentation](deeporca-module-layout.md) for the package
boundaries, native-style workbench screenshots and source-checkout restart notes.
