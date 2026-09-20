# Existing native profile binding — real Workbench UI

Screenshots are from Chromium against the real Workbench with intercepted fixture HTTP and WebSocket data. They do not use a live Server, Connector, provider, or native profile.

- `deeporca-bind-existing-desktop.png` / `deeporca-bind-existing-mobile.png`: inventory-selected native profile and explicit stopped-native confirmation; managed model and credential controls are disabled and hidden.
- `deeporca-bound-settings-desktop.png` / `deeporca-bound-settings-mobile.png`: immutable native label/reference, name-only save, safe busy guidance, refresh and retry. No editable model or credential controls.

Current runtime policy is separate from these UI fixtures: native DeepOrca
defaults to `minimal` and explicit security configuration is respected without an
embedded `standard`/`allowlist` floor. Bound profile files are not proactively
rewritten, but existing `minimal` is also honored at runtime; there is no security
migration or versioned new-profile opt-in. Final `REVIEW`/`DENY` and unsupported
background/autonomous tools remain refused. Binding consent, manually stopped
native writers and exclusive ownership checks are unchanged. See
[security scope](../../deeporca-existing-profiles.md#security-policy-for-all-profiles).

Reproduce in Windows CMD (Playwright Chromium installed):

```cmd
set DEEPORCA_BROWSER_ARTIFACTS=docs/prototypes/deeporca-bind-profile
python -m pytest tests/test_deeporca_browser.py -q -k bind_existing_profile
```

The desktop/mobile scenario asserts default create mode, required selection and boolean consent, preserved managed drafts across toggles and request failures, no plaintext hidden HTML values, exact minimal binding payload, absent CLI fields, rename-only settings updates, body-free retry, no horizontal overflow, and no external network traffic.

Additional form/contract regression coverage:

```cmd
node --test web/*.test.js
python -m pytest tests/test_deeporca_web.py tests/test_deeporca_browser.py -q
```
