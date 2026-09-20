# Binding an existing DeepOrca profile

This small extension adds existing-profile selection to the DeepOrca-only
workbench form. It uses the native SDK's `profile_mode="existing"`; it is not an
importer, a profile migration, a shared native/Connector lease system or a way to
launch a standalone DeepOrca server.

## Operator flow

1. Update/restart the Connector with a native SDK supporting existing profiles.
2. **Stop native DeepOrca for the chosen profile.** Keep all native writers for
   that profile stopped for the entire time the Connector owns it. Closing a
   browser tab or ending a conversation is not equivalent to stopping the writer.
3. In **Add agent**, choose DeepOrca and a registered local project, then choose
   **Bind an existing profile**. Select a profile from this Connector's inventory
   and explicitly confirm the native-stop condition.
4. Add the Agent. Readiness follows normal pending/provisioning/ready/error
   reporting. Creation is not a guarantee that the profile is free or configured.
5. Open a new DeepBox conversation. Existing persona, memory, skills, tools and
   model configuration are reused. **Native chat history is not automatically
   imported into DeepBox conversations.**

The Connector discovers direct profile directories in the SDK's local
`get_deeporca_dir()/agents` location. A local operator can select another native
home through `AGENTBRIDGE_DEEPORCA_HOME` (legacy `DEEPBOX_DEEPORCA_HOME`). This is
the home containing `agents/`, not an individual profile directory. Paths never
come from browser input. Reconnect to refresh inventory after creating/renaming
a native profile. The bounded catalog lists at most 100 eligible profile names.

An older SDK/Connector does not advertise binding. No arbitrary directory or
profile-name text box replaces a missing catalog. Unknown/stale references fail
closed locally even if the Server's last advertised inventory is stale.

## Identity and ownership

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

## Security policy for all profiles

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

See [operator setup](deeporca.md), [managed model configuration](deeporca-profile-configuration.md)
and [validation](deeporca-validation.md) for their separate guarantees and evidence.
