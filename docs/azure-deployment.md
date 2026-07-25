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
| `DEEPBOX_AUTH_MODE` | `local`, then `hybrid`, then `microsoft` | keep the local fallback until interactive sign-in is verified |
| `DEEPBOX_SESSION_TTL_SECONDS` | `28800` | Deepbox cookie lifetime, minimum 300 |
| `DEEPBOX_MICROSOFT_OWNER_EMAILS` | explicit email list | required in `microsoft` mode; keep narrow |
| `DEEPBOX_MICROSOFT_ALLOWED_TENANT_IDS` | explicit Entra tenant ID list | required whenever Microsoft auth is enabled in production |
| `DEEPBOX_WORKSPACE_INVITATION_TTL_DAYS` | `7` | allowed range 1–30 |
| `OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID` | user-assigned managed identity client ID (slot setting) | reserved Easy Auth FIC pointer; non-secret and never read by Deepbox |

Port precedence in code: `DEEPBOX_PORT` → `PORT` → `WEBSITES_PORT` → `8077`.

## Microsoft account sign-in (Easy Auth v2)

Keep `DEEPBOX_AUTH_MODE=local` until every item below is complete. Enabling the application mode without the platform identity boundary would trust spoofable client headers.

1. For an employee-only deployment, create a **single-tenant** app registration (`signInAudience = AzureADMyOrg`) in the organization's Entra tenant. Broader organizational or personal-account audiences must be an explicit product decision, not the default. Tenants that enforce a Service Tree reference require `az ad app create --service-management-reference <service-tree-guid>`.
2. Add the exact Web redirect URI `https://<app>.azurewebsites.net/.auth/login/aad/callback`. This is the Easy Auth provider callback; `/api/auth/microsoft/callback` is Deepbox's post-login route and is not registered with Entra. Retain both the public application/client ID and the app registration object ID.
3. Configure the secretless Easy Auth identity and deploy `authsettingsV2`:

   ```powershell
   ./scripts/configure-microsoft-auth.ps1 `
     -SubscriptionId <subscription-guid> `
     -ResourceGroup <resource-group> `
     -WebAppName <app> `
     -TenantId <tenant-guid> `
     -ClientId <application-client-guid> `
     -ApplicationObjectId <application-object-guid>
   ```

   The helper enables ID-token issuance on the app registration because App Service Easy Auth requests an ID token as part of its hybrid login flow. It creates or reuses the app registration's home-tenant service principal (Enterprise Application); an app object alone cannot complete the authorization-code exchange. It then creates or reuses a user-assigned managed identity, assigns it to the web app, and creates an Entra federated identity credential with issuer `https://login.microsoftonline.com/<tenant-guid>/v2.0`, subject equal to the managed identity principal/object ID, and audience `api://AzureADTokenExchange`. It writes the identity's client ID to the sticky, reserved App Service setting `OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID`, then deploys `infra/microsoft-auth.bicep`. The Bicep template points `clientSecretSettingName` at that reserved setting; despite the schema property name, no client secret or certificate exists.

   The template enables Easy Auth v2, uses the tenant-specific issuer, accepts only the app's audiences, requires HTTPS, disables the unused token store, and deliberately allows anonymous requests through to Deepbox's own route authorization. The helper validates the current tenant, single-tenant app registration with ID-token issuance, and enabled home-tenant service principal; it reuses an exact existing FIC, fails closed on a conflicting FIC, and **does not** change `DEEPBOX_AUTH_MODE`.
4. Set `DEEPBOX_MICROSOFT_ALLOWED_TENANT_IDS=<tenant-guid>` and a narrow normalized `DEEPBOX_MICROSOFT_OWNER_EMAILS` list. Keep `DEEPBOX_AUTH_MODE=local` while validating the platform redirect, then use `hybrid` for the first interactive sign-in so the password path remains a rollback route.
5. Verify HTTPS-only, the exact redirect URI, ID-token issuance, issuer and audiences, enabled home-tenant service principal, UAMI assignment, reserved slot setting, FIC tuple, and that `/.auth/login/aad` redirects to the expected tenant and client ID. A completed sign-in must produce platform-injected `X-MS-CLIENT-PRINCIPAL*` headers. Test a different tenant and confirm Deepbox returns 403. Never expose the ASGI process directly in Microsoft mode.
6. After sign-in, exercise at least one cookie-authenticated mutation such as creating a workspace or devbox. Deepbox emits `Referrer-Policy: same-origin` because Easy Auth uses the same-origin `Referer` for its cookie-request CSRF validation; `no-referrer` makes the platform reject these requests with HTTP `403.60` before they reach FastAPI.
7. After sign-in, logout, cookie expiry, owner linking, a mutation, and a workspace invitation pass end to end, change `DEEPBOX_AUTH_MODE=microsoft` to remove password login.

Deepbox never receives or stores Microsoft access/refresh tokens, app credentials, or model credentials. It maps the Easy Auth tenant + subject to a user, applies its own tenant allowlist, and issues its own signed, time-limited cookie. An allow-listed identity may claim the sole unlinked local owner during migration; ordinary identities are deployment members and join shared workspaces through invitations.

Official references:

- [Configure Microsoft identity for App Service Authentication](https://learn.microsoft.com/azure/app-service/configure-authentication-provider-aad)
- [Use a managed identity instead of a secret](https://learn.microsoft.com/azure/app-service/configure-authentication-provider-aad#use-a-managed-identity-instead-of-a-secret)
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

- There is no auto-seeded account. Registration is disabled by default in
  production, and the registration route returns HTTP 403.
- Reassess exposing the app publicly versus keeping it behind Tailscale or
  Front Door with authentication. For a private multi-machine setup, see
  [remote-deployment.md](remote-deployment.md).
