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
| `DEEPBOX_PUBLIC_URL` | `https://<app>.azurewebsites.net` | |
| `DEEPBOX_ALLOWED_ORIGINS` | `https://<app>.azurewebsites.net` | HTTPS required |
| `DEEPBOX_COOKIE_SECURE` | `true` | |
| `DEEPBOX_REGISTRATION_ENABLED` | `false` | fail-closed sign-up |

Port precedence in code: `DEEPBOX_PORT` → `PORT` → `WEBSITES_PORT` → `8077`.

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
