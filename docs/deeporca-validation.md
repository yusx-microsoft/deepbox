# DeepOrca v1 implementation validation

This records local branch verification, not a production deployment or external-provider certification.

- AgentBridge branch: `feat/deeporca-library-integration`; initial integration `36bc8e0`, bounded-stream hardening `97eaa75`, then the module/presentation follow-up documented below.
- Native SDK branch: `feat/deepbox-library-host`, tested commit `9206c0b` (embedded API v1).
- Setup: Windows, existing Python/Node/Chromium test dependencies. SDK and live integration tests used temporary profiles, projects, databases and test-only credentials, with a scripted loopback OpenAI/SSE provider. The user's agent/profile and running preview service were not used for model turns.

## Module and native-style presentation follow-up

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

## Important assertions

- Browser inputs/configuration never choose an executable, package, environment, credential, or arbitrary workspace/template path. Only registered project references and the Connector's `connector-default` template reference are accepted.
- DeepOrca has no approval request/response UI, transport or waiting state. Native checks are preserved under the embedded standard/allowlist floor; final human-review requirements fail immediately. Other runtimes retain their approval behavior.
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

Not verified here: real external-provider credentials/billing, remote deployment, existing standalone-profile adoption, autonomous/background execution, or arbitrary subprocess rollback. No production deployment or restart of the user's existing preview/Connector was performed. See [operational setup and limits](deeporca.md) and [the reconciled design](deeporca-integration-design.md) before enabling the runtime in a real Connector.
