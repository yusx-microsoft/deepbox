# DeepOrca web profile configuration

This is the configuration extension to the Connector-local embedded integration.
It supersedes the initial template-only onboarding flow, not its ownership,
authorization, native-context or non-interactive tool-policy boundaries. See
[operator setup](deeporca.md) and [module boundaries](deeporca-module-layout.md).

This editable form applies to **managed-create** mode. The separate
[existing-profile binding](deeporca-existing-profiles.md) flow reuses local
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
see [security scope](deeporca-existing-profiles.md#security-policy-for-all-profiles).

## Experience and configuration contract

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

## Sealed credential protocol (version 1)

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

## Application and updates

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

## Verification

Shared contract, Server authorization/update, Connector crypto/lifecycle, native
profile ownership/crash-safety, Node form/encryption, and Chromium interaction
tests exercise separate boundaries. Opt-in real SDK E2E uses only an owned
loopback scripted provider and temporary accounts/profiles/databases. Refer to
[validation evidence](deeporca-validation.md) for completed run results. Fixture
screenshots and import/readiness probes are not provider-authentication evidence.

Updating source does not reload existing Server/Connector processes. Restart
both during an appropriate idle window to load the new capability and writer.
No Server reset, new account, project recreation or Token rotation is needed.
