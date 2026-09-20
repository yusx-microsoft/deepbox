# DeepOrca Agent model settings — shipped UI fixtures

These screenshots show the actual workbench implementation, not a standalone mockup.

- `deeporca-add-agent-model-connection.png`: Add Agent, grouped model connection and behavior; API key masked.
- `deeporca-agent-settings-model.png`: edit an existing Agent, with readiness, immutable project/runtime, and retained authentication.
- `deeporca-agent-settings-mobile.png`: scrolled mobile Settings; model behavior and Save remain reachable without horizontal overflow.

All names, endpoints, keys and model IDs are synthetic fixtures. An ephemeral loopback static server and intercepted in-memory API/WebSocket requests were used. No real Server, Connector, profile or provider endpoint was contacted.

These model settings do not select a security policy. Native DeepOrca defaults
to `minimal`; explicit native/template/profile security settings are respected
without an embedded `standard`/`allowlist` floor. Existing files are not proactively
rewritten, but existing `minimal` is also honored at runtime. There is no security
migration or versioned new-profile opt-in. Final `REVIEW`/`DENY` and unsupported
background/autonomous tools remain refused; binding/exclusivity rules are
unchanged. See [security scope](../../deeporca-existing-profiles.md#security-policy-for-all-profiles).

Regenerate on Windows with installed Playwright/Chromium:

```cmd
set DEEPORCA_BROWSER_ARTIFACTS=C:/repos/deepbox/docs/prototypes/deeporca-agent-config
python -m pytest tests/test_deeporca_browser.py::test_real_workbench_encrypted_model_settings_and_mobile -q
```

The Chromium test also decrypts the browser's sealed API-key envelope independently with Python `cryptography` (RSA-OAEP SHA-256, AES-256-GCM, exact endpoint-bound AAD). It checks editing/409 draft preservation, endpoint-change replacement requirements, no secret readback or browser storage, refresh/retry, runtime toggling, and mobile scrolling. This is browser/crypto interop evidence, not live model inference evidence.
