# One-Line Installer

deepbox ships two one-line installers so a user can connect a machine **without
cloning the repo or installing dependencies by hand**:

```powershell
# Windows (PowerShell)
irm https://deeporca.blob.core.windows.net/deepbox/install.ps1 | iex
```

```bash
# macOS / Linux
curl -fsSL https://deeporca.blob.core.windows.net/deepbox/install.sh | bash
```

## What the installer does

1. Finds a Python 3.10+ interpreter (and prints install guidance if missing).
2. Downloads the connector source as a zip from the **public** mirror
   (`yusx-swapp/deepbox`) — anonymous, no GitHub access needed.
3. On a Windows reinstall, finds a running `-m connector` process from this
   installation's virtualenv and stops its connector-owned process tree before
   replacing the source directory.
4. Creates an isolated virtualenv under `~/.deepbox` and installs the
   connector dependencies (`httpx`, `websockets`, and `pywinpty` on Windows).
5. Writes a reusable launcher (`~/.deepbox/deepbox-connect.cmd` / `.sh`).
6. Connects immediately using the server URL + devbox token.

Everything lives under `~/.deepbox`; uninstall by deleting that folder. The
**token is never written to disk** — it is passed to the connector process via
an environment variable only.

### Reinstalling while a Windows connector is running

Re-running `install.ps1` detects only connector processes launched as
`~\.deepbox\venv\Scripts\python.exe -m connector`, snapshots and stops their
connector-owned child process tree, waits for the launcher to release
`~\.deepbox\app`, and retries the directory replacement. This intentionally
ends that connector's active local sessions before the new copy connects. It
does not stop unrelated Python processes and never logs their command lines. If
a separate shell itself has `~\.deepbox\app` as its working directory, leave
that directory or close the shell and run the installer again.

### Non-interactive use

Pre-set the two values so the piped installer doesn't prompt:

```powershell
$env:DEEPBOX_SERVER_URL = 'https://deepbox-sixingyu-pa.azurewebsites.net'
$env:DEEPBOX_TOKEN      = 'hpc_box_xxxxxxxx'
irm https://deeporca.blob.core.windows.net/deepbox/install.ps1 | iex
```

```bash
export DEEPBOX_SERVER_URL='https://deepbox-sixingyu-pa.azurewebsites.net'
export DEEPBOX_TOKEN='hpc_box_xxxxxxxx'
curl -fsSL https://deeporca.blob.core.windows.net/deepbox/install.sh | bash
```

Set `DEEPBOX_SOURCE_ZIP` to install from a fork or a specific branch.

### Reconnecting later

The generated launcher re-runs the connector without re-downloading anything:

```powershell
# Windows — prompts for URL/token if not already in the environment
%USERPROFILE%\.deepbox\deepbox-connect.cmd
```

```bash
# macOS / Linux
DEEPBOX_SERVER_URL=... DEEPBOX_TOKEN=... ~/.deepbox/deepbox-connect.sh
```

---

# Hosting the Installer on Azure Blob Storage

The one-line installers (`scripts/install.ps1` and `scripts/install.sh`) are
served from Azure Blob Storage so that anyone — including users without access
to the private GitHub repo — can run them anonymously:

```powershell
# Windows
irm https://deeporca.blob.core.windows.net/deepbox/install.ps1 | iex
```

```bash
# macOS / Linux
curl -fsSL https://deeporca.blob.core.windows.net/deepbox/install.sh | bash
```

This document records how that storage account was set up and how to publish
updates to the scripts.

## Prerequisites

- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) installed.
- Signed in with an account that has at least **Contributor** on the target
  subscription:

  ```powershell
  az login
  az account set --subscription "<subscription-name-or-id>"
  ```

## Current configuration

| Setting          | Value                |
| ---------------- | -------------------- |
| Resource group   | `singularity-webdata`|
| Storage account  | `deeporca`           |
| Location         | `eastus2`            |
| Container        | `deepbox`            |
| Public access    | Blob (anonymous read)|

Resulting public URLs:

- `https://deeporca.blob.core.windows.net/deepbox/install.ps1`
- `https://deeporca.blob.core.windows.net/deepbox/install.sh`

## One-time setup

### 1. Create the storage account

The account name must be globally unique, all lowercase, 3–24 characters,
letters and digits only.

```powershell
az storage account create --name deeporca --resource-group singularity-webdata --location eastus2 --sku Standard_LRS --kind StorageV2 --allow-blob-public-access true
```

> Some subscriptions enforce an Azure Policy that blocks public blob access. If
> `--allow-blob-public-access true` is rejected, use the **SAS** approach in
> [Alternative: SAS instead of public access](#alternative-sas-instead-of-public-access).

### 2. Create the public container

```powershell
az storage container create --name deepbox --account-name deeporca --public-access blob --auth-mode login
```

## Publishing / updating the scripts

Re-upload the same blob name with `--overwrite`; the public URL never changes
and updates take effect immediately (blobs are not CDN-cached by default).

```powershell
az storage blob upload --account-name deeporca --container-name deepbox --name install.ps1 --file "scripts/install.ps1" --content-type "text/plain; charset=utf-8" --overwrite --auth-mode key
```

```powershell
az storage blob upload --account-name deeporca --container-name deepbox --name install.sh --file "scripts/install.sh" --content-type "text/plain; charset=utf-8" --overwrite --auth-mode key
```

### `--auth-mode login` vs `--auth-mode key`

Being **Owner/Contributor** on the subscription controls the management plane,
but blob upload is a **data-plane** operation. If you see:

```
You do not have the required permissions needed to perform this operation.
```

either:

- Use `--auth-mode key` (shown above). The CLI looks up the account key
  automatically — this works because you can read the account keys.
- Or grant yourself a data-plane role once, then `--auth-mode login` works:

  ```powershell
  $me    = az ad signed-in-user show --query id -o tsv
  $scope = az storage account show --name deeporca --resource-group singularity-webdata --query id -o tsv
  az role assignment create --assignee $me --role "Storage Blob Data Contributor" --scope $scope
  ```

  Allow a minute or two for the role assignment to propagate.

## Verifying

```powershell
irm https://deeporca.blob.core.windows.net/deepbox/install.ps1 | Select-Object -First 5
```

You should see the script's header comments.

## Alternative: SAS instead of public access

If public blob access is disallowed by policy, keep the container private and
generate a long-lived read-only SAS URL:

```powershell
$expiry = (Get-Date).AddYears(2).ToString("yyyy-MM-ddTHH:mmZ")
az storage blob generate-sas `
  --account-name deeporca `
  --container-name deepbox `
  --name install.ps1 `
  --permissions r `
  --expiry $expiry `
  --https-only `
  --auth-mode login --as-user -o tsv
```

The resulting URL is long and ugly:

```
https://deeporca.blob.core.windows.net/deepbox/install.ps1?<sas-token>
```

Wrap it behind an `aka.ms` short link so users get a clean, stable command.

## Optional: aka.ms short link

1. Go to <https://aka.ms> and sign in with your corporate account.
2. Create a link:
   - **Short URL**: `deeporca-install` → `aka.ms/deeporca-install`
   - **Target URL**: the blob URL (public or SAS).
3. Users then run:

   ```powershell
   irm https://aka.ms/deeporca-install | iex
   ```

`aka.ms` issues a 302 redirect that `irm`/`curl` follow automatically. The
short link stays constant even if the storage backend changes later — just
update its target.