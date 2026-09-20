# DeepOrca v1 implementation validation

This records local branch verification, not a production deployment or external-provider certification.

- AgentBridge branch: `feat/deeporca-library-integration`; initial integration `36bc8e0`, bounded-stream hardening `97eaa75`, then the module/presentation follow-up documented below.
- Native SDK branch: `feat/deepbox-library-host`, tested commit `9206c0b` (embedded API v1).
- Setup: Windows, existing Python/Node/Chromium test dependencies. SDK and live integration tests used temporary profiles, projects, databases and test-only credentials, with a scripted loopback OpenAI/SSE provider. The user's agent/profile and running preview service were not used for model turns.

## Native security inheritance simplification (current working tree)

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

## Existing-profile binding and minimal-security extension (preceding round)

This extends DeepBox `8da1462` plus the preceding configuration work, and native
SDK `3978d2f` plus the narrow new-profile security-default opt-in. These changes
are uncommitted. See [existing-profile operations](deeporca-existing-profiles.md)
and [fixture screenshots](prototypes/deeporca-bind-profile/README.md).

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

## Web profile configuration extension (preceding round)

This extends DeepBox `8da1462` (the squash of the original three integration
commits) and native SDK `9206c0b`; it does not change their historical results
below. Scope: [complete browser profile setup/editing](deeporca-profile-configuration.md),
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

### Acceptance evidence

- `tests/test_deeporca_configuration_contract.py`: bounded declarative settings,
  native effort/window/model rules, invalid endpoint/plaintext-key rejection,
  envelope bounds, and mutable desired revision versus immutable identity.
- `tests/test_deeporca_settings.py`: create/update authorization, immutable
  project/profile fences, active-session conflict, retained sealed keys,
  endpoint-change safeguards, stale status and no-op/rename behavior.
- `tests/test_deeporca_configuration.py`: public-key publication, private key
  persistence/recovery fencing, worker-only decoding, tamper/endpoint failure,
  old SDK errors, revision updates and busy-worker safety.
- `tests/test_deeporca_profile_e2e.py`: actual browser-module WebCrypto in Node,
  real HTTP/WS Server, real Connector and spawned real native SDK, with **no model
  template**. A loopback scripted provider verifies dummy key, model and effort.
  The test updates the same profile, rejects changes with an active session,
  restarts the Connector, continues preserved native context and checks no
  plaintext dummy key enters Server HTTP bodies/responses, DB files, logs,
  canonical events or recordings.
- `tests/test_deeporca_browser.py`: real Chromium fixture UI, encrypted creation
  independently decrypted in Python, settings retention/replacement, failed-save
  draft preservation, explicit no-auth, runtime toggling and mobile sizing.
- `web/deeporca-agent-configuration.test.js` and `web/management.test.js`:
  native-compatible fields, randomized endpoint-bound encryption, runtime-agnostic
  delegation, legacy fixed models/rename-only behavior; deferred preparation
  cannot submit after permissions/context/dialog change.
- Native `tests/test_embedded_configuration.py`: ownership before writes,
  literal-key round trip, unrelated YAML/env preservation, interrupted two-file
  recovery, symlink/reparse rejection and protected Windows DACLs established
  **before** private staged/published bytes are written.

Fixture-only screenshots:
[`prototypes/deeporca-agent-config/`](prototypes/deeporca-agent-config/README.md).

### Retained intermediate failures and review fixes

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

## Historical module and native-style presentation follow-up

See [module layout and visual evidence](deeporca-module-layout.md). Runtime-specific
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

## Previous v1 verification (`97eaa75`)

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

The [design acceptance checklist](deeporca-design-checklist.md) maps v1 requirements to implementation and concrete regression tests, and distinguishes intentionally gated scope. The follow-up adds bounded 12ms / 16KiB text/thinking coalescing, an explicit safe generic-renderer fallback notice, and honest unfinished-tool presentation for inactive/replayed sessions. The initial committed tree was reverified at 883 Python passes before these additions; that earlier run is not evidence for the new code.

An earlier concurrent full run hit the existing Windows PTY reader-settlement assertion in `test_native_windows_kill_isolated_idle_python_releases_reader`. That test passed on isolated rerun, and the final full suite passed without parallel heavy validation jobs. Its code/test timeout was not weakened or changed. This remains an intermittent platform observation, not a hidden passing result.

Two intermediate follow-up full runs each reported **1 failed, 911 passed, 8 skipped, 20 subtests passed**: the existing Hub stalled-watcher test checked eviction after a fixed 30ms sleep. Its isolated module run passed all 10 tests. The fixture now exposes actual send/close events: the test preserves its 10ms send timeout and 200ms producer bound, observes healthy delivery, then waits for close with a one-second test deadline and retains all eviction/close-code assertions. No production Hub code or policy timeout was changed. The revised Hub module again passed all 10 tests.

A separate final review found that cancellation while a native callback waited for the batching lock was outside its failure handler. The handler now covers lock acquisition as well as sending. The new regression failed with the handler deliberately bypassed, then passed for both lock-wait and in-send cancellation with the fix; all 30 coalescing tests, the 112-test worker group and the 25-test actual-SDK group were rerun successfully afterward.

## Reproduction

From AgentBridge, with test dependencies already installed:

```bat
python -m pytest tests -q
python -c "import subprocess,pathlib,sys; sys.exit(subprocess.call(['node','--test',*[str(p) for p in pathlib.Path('web').rglob('*.test.js')]]))"
python -m pytest tests/test_deeporca_browser.py -q
python -m pytest tests/test_deeporca_coalescing.py tests/test_deeporca_worker.py tests/test_deeporca_session.py tests/test_deeporca_supervisor.py -q
```

Use an isolated test data directory/database when running the entire suite. During this verification `PYTHON_DOTENV_DISABLED=1`, `DEEPBOX_DATABASE_URL` and `DEEPBOX_DATA_DIR` were set command-locally to temporary test destinations, not the running preview's data.

The earlier local JUnit evidence is under `C:/Users/chec/.deeporca/agents/deepbox/tmp/deeporca-round2-evidence/`: `deepbox-full.xml` (initial committed tree), `deepbox-final.xml` and `deepbox-final-rerun.xml` (the two intermediate Hub failures described above), `deepbox-release.xml` (tree committed as `97eaa75`), `native.xml`, `sdk-e2e-final.xml`, `worker-stream-final.xml`, `coalescing-final.xml`, `browser.xml`, `hub-isolated.xml` and `hub-final.xml`; its Node output is `node-final.log`. These are local temporary artifacts, not committed fixtures or durable production evidence. Add `--junitxml=<local-path>` to the pytest commands to reproduce machine-readable evidence. At that point the checklist's 38 named test-function references were verified against both source trees.

The optional real-SDK group:

```bat
set "AGENTBRIDGE_DEEPORCA_SOURCE=C:\repos-gim\deeporca"
python -m pytest tests/test_deeporca_sdk_integration.py tests/test_deeporca_e2e.py tests/test_deeporca_supervisor.py -q
```

The source must contain compatible API v1 and its dependencies must be available to the Connector interpreter. The fixtures create their own fake provider/template, guard worker outbound networking, and use temporary managed profiles. Do not substitute a real user's profile or provider key into these tests.

From the native repository:

```bat
python -m pytest tests/test_embedded.py tests/test_turn_error_settlement.py tests/test_tools_cancel_cleanup.py tests/test_turn_characterization.py tests/test_turns.py tests/test_turns_phase3.py tests/test_turns_phase4.py tests/test_turns_phase5.py tests/test_turns_phase6.py tests/test_turns_phase7.py tests/test_turns_phase8.py tests/test_turns_phase9.py tests/test_tools.py tests/test_file_tools.py tests/test_security.py -q
```

## Merge with native writers and session-history actions

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

## Important assertions

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

## Screenshots and limits

These are the **real workbench renderer with test fixture data**, not external-model sessions:

- [Desktop conversation and existing CLI pane](prototypes/deeporca-workbench-v1.png)
- [Agent settings and readiness](prototypes/deeporca-agent-settings-v1.png)
- [Mobile workbench](prototypes/deeporca-workbench-mobile-v1.png)

The separate [offline prototype](prototypes/deeporca.md) is illustrative and non-normative.

Not verified here: real external-provider credentials/billing, remote deployment, binding a real user's live profile, autonomous/background execution, or arbitrary subprocess rollback. Existing-profile binding is verified with disposable native profiles and a loopback provider, not a shared native/embedded writer lease. No production deployment or restart for the binding extension was performed; the earlier model-configuration preview restart is a separate operational action. See [operational setup and limits](deeporca.md) and [the reconciled design](deeporca-integration-design.md) before enabling the runtime in a real Connector.
