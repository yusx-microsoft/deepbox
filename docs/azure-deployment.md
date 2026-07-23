# Azure App Service (Linux) deployment

This guide describes deploying the Deepbox **control-plane server** to Azure
App Service on Linux. The **connector** and any live agents are NOT part of
this deployment — they continue to run on your own machines and connect
outbound to the server.

> Nothing in this repo creates Azure resources automatically. Run the deploy
> script yourself when ready.

## Topology

- **App Service (Linux, B1, Python 3.12)** runs the FastAPI server under
  gunicorn + a single uvicorn worker.
- **TLS** is terminated by the App Service front end (HTTPS-only, min TLS 1.2,
  HTTP/2). It forwards to the container's published port.
- Because the platform front end is the only ingress, the server binds
  `0.0.0.0`. This is the *only* production configuration allowed to do so and
  is gated behind `DEEPBOX_PLATFORM=azure-app-service`.
- **SQLite + DVR recordings** live on the persistent `/home` volume
  (`WEBSITES_ENABLE_APP_SERVICE_STORAGE=true`), under `/home/deepbox` by
  default. Paths come from app settings — no cloud path is hardcoded in code.

## Configuration (app settings)

| Setting | Value | Notes |
|---|---|---|
| `DEEPBOX_ENV` | `production` | enables prod validation |
| `DEEPBOX_PLATFORM` | `azure-app-service` | permits `0.0.0.0` bind |
| `DEEPBOX_HOST` | `0.0.0.0` | reachable behind the front end |
| `DEEPBOX_PORT` / `WEBSITES_PORT` | `8000` | App Service publishes this port |
| `DEEPBOX_FORWARDED_ALLOW_IPS` | `*` | trust the platform reverse proxy |
| `DEEPBOX_SECRET` | *secure param* | generated at deploy time or Key Vault |
| `DEEPBOX_DATABASE_URL` | `sqlite:////home/deepbox/deepbox.db` | on persistent volume |
| `DEEPBOX_DATA_DIR` | `/home/deepbox` | DVR recordings |
| `DEEPBOX_PUBLIC_URL` | `https://<app>.azurewebsites.net` | required by Microsoft mode |
| `DEEPBOX_ALLOWED_ORIGINS` | `https://<app>.azurewebsites.net` | HTTPS required |
| `DEEPBOX_COOKIE_SECURE` | `true` | required in production |
| `DEEPBOX_COOKIE_SAMESITE` | `lax` | survives top-level OAuth redirect |
| `DEEPBOX_REGISTRATION_ENABLED` | `false` | fail-closed local sign-up |
| `DEEPBOX_AUTH_MODE` | `local`, then `microsoft` | do not switch until Easy Auth is verified |
| `DEEPBOX_SESSION_TTL_SECONDS` | `28800` | Deepbox cookie lifetime, minimum 300 |
| `DEEPBOX_MICROSOFT_OWNER_EMAILS` | explicit email list | required in `microsoft` mode; keep narrow |
| `DEEPBOX_WORKSPACE_INVITATION_TTL_DAYS` | `7` | allowed range 1–30 |
| `MICROSOFT_PROVIDER_AUTHENTICATION_SECRET` | *secure app setting* | consumed by Easy Auth, never Deepbox |

Port precedence in code: `DEEPBOX_PORT` → `PORT` → `WEBSITES_PORT` → `8077`.

## Microsoft account sign-in (Easy Auth v2)

Keep `DEEPBOX_AUTH_MODE=local` until every item below is complete. Enabling the application mode without the platform identity boundary would trust spoofable client headers.

1. Create or select a Microsoft identity platform app registration whose supported account type is **Accounts in any organizational directory and personal Microsoft accounts** (`signInAudience = AzureADandPersonalMicrosoftAccount`).
2. Add the exact Web redirect URI `https://<app>.azurewebsites.net/.auth/login/aad/callback`. This is the Easy Auth provider callback; `/api/auth/microsoft/callback` is Deepbox's post-login route and is not registered with Entra.
3. Store the client secret in the App Service setting `MICROSOFT_PROVIDER_AUTHENTICATION_SECRET`, and point the Easy Auth v2 Microsoft provider's `clientSecretSettingName` to that setting. Do not put the secret in source, Bicep parameter files, deployment ZIPs, or Deepbox `.env` files.
4. Enable App Service Authentication v2 with the Microsoft provider and the app registration client ID. Configure global validation to **Allow unauthenticated access** (`requireAuthentication=false`, `unauthenticatedClientAction=AllowAnonymous`): public landing/auth routes and connector bearer-token routes must reach Deepbox, which performs their own authorization. Do not use the platform default redirect for every request.
5. Verify HTTPS-only, the exact redirect URI, supported account audience, provider secret setting, and that a completed sign-in produces platform-injected `X-MS-CLIENT-PRINCIPAL*` headers. External requests must not be able to choose these trusted values; never expose the ASGI process directly in Microsoft mode.
6. Set a narrow normalized `DEEPBOX_MICROSOFT_OWNER_EMAILS` list, then change `DEEPBOX_AUTH_MODE=microsoft` (or `hybrid` for a migration), restart, and verify `GET /api/auth/config`, both organization and personal Microsoft sign-in, logout, cookie expiry, and a workspace invitation end to end.

Deepbox never receives or stores Microsoft access/refresh tokens. It maps the Easy Auth tenant + subject to a user and issues its own signed, time-limited cookie. An allow-listed identity may claim the sole unlinked local owner during migration; ordinary identities are deployment members and join shared workspaces through invitations.

Official references:

- [Configure Microsoft identity for App Service Authentication](https://learn.microsoft.com/azure/app-service/configure-authentication-provider-aad)
- [Supported account types and `signInAudience`](https://learn.microsoft.com/entra/identity-platform/supported-accounts-validation)
- [App Service Authentication overview](https://learn.microsoft.com/azure/app-service/overview-authentication-authorization)

## Health, WebSockets, scaling

- Health check path: `/api/ready`.
- WebSockets enabled (terminal streaming).
- Always On enabled; single worker, single instance. Do **not** scale out:
  SQLite and in-process session state are not multi-instance safe.

## Secrets

`DEEPBOX_SECRET` is never committed. `scripts/deploy-azure.ps1` generates a
strong random secret at deploy time (or accepts `-DeepboxSecret` / a Key Vault
source) and stores it only as an Azure app setting. `infra/main.parameters.json`
contains no secret value.

## Deploy

```powershell
# From the repo root (Windows PowerShell). Requires Azure CLI + login.
./scripts/deploy-azure.ps1 `
    -WebAppName my-deepbox-42 `      # must be globally unique
    -ResourceGroup deepbox-rg `
    -Location eastus
```

This creates the resource group, deploys `infra/main.bicep`, and zip deploys
`server/`, `web/`, `azure-startup.sh`, and `requirements.txt`. Oryx
(`SCM_DO_BUILD_DURING_DEPLOYMENT=true`, `ENABLE_ORYX_BUILD=true`) installs the
**root** `requirements.txt`. App Service selects the most recently extracted
startup script, which resolves its own Oryx app root before launching the single
Gunicorn worker. The deploy helper writes POSIX ZIP entry paths so Linux extracts
the Python package directories correctly. Server dependencies are
Linux-clean and do not include the Windows-only `pywinpty` (that lives in
`requirements-connector.txt`).

## Dependency split

- `requirements.txt` — server only; installed by Oryx. No Windows packages.
- `requirements-connector.txt` — connector; `pywinpty` gated to `win32`.

## Security notes

- Registration is disabled by default in production; the route returns HTTP 403.
- There is **no** auto-seeded `demo/demo` account. `provision_demo.py` is a
  dev-only helper that calls the public register API; with production's default
  registration setting, that call is rejected with HTTP 403.
- Reassess exposing the app publicly vs. keeping it behind Tailscale/Front
  Door with auth — see risks in the task summary.
