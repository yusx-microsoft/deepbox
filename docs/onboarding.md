# Onboarding and account management

agentbridge has two identity paths and two different invitation types:

- **Local account invitation**: a deployment owner creates a new password account.
- **Workspace invitation**: a workspace owner/admin grants an existing or future Microsoft identity access to one workspace.

The browser reads `GET /api/auth/config` and only shows the sign-in methods enabled by `AGENTBRIDGE_AUTH_MODE` (legacy `DEEPBOX_AUTH_MODE` remains supported):

| Mode | Local password | Microsoft Easy Auth | Intended use |
|---|---:|---:|---|
| `local` | yes | no | local development and the safe default |
| `hybrid` | yes | yes | migrating an existing deployment |
| `microsoft` | no | yes | Microsoft-only Azure deployment |

`microsoft` and `hybrid` are safe only behind correctly configured Azure App Service Easy Auth. A directly reachable ASGI server must stay in `local` mode because client-supplied `X-MS-CLIENT-PRINCIPAL*` headers are not an identity boundary. Production Microsoft auth also requires `AGENTBRIDGE_MICROSOFT_ALLOWED_TENANT_IDS`; agentbridge rejects a valid Easy Auth principal whose tenant claim is not in that list.

### Rename compatibility and review boundary

Configuration prefers `AGENTBRIDGE_<name>` over `DEEPBOX_<name>` **by presence**:
an explicitly empty canonical value does not fall back to the legacy value.
Existing Azure app settings may keep their `DEEPBOX_*` names and values; do not
rename resources, change the server host, rotate secrets or edit login callbacks
just to update the displayed product name. The startup script continues to use
the configured persistent data directory (including `/home/deepbox`), outside
`wwwroot`, with one server worker. Cookie names, invitation formats and identity
records remain compatible; this is not account re-enrollment.

The source repository and installer URLs remain `yusx-microsoft/deepbox` (with
the `deeporc-ai/deepbox` mirror), pending separate external-migration approval.
For local UI review, inspect sign-in labels, the token dialog and copied command
text without executing an installation or connection command. New command text
uses `agentbridge`; old `deepbox` and `deepbox-connect` shortcuts remain valid.
See [install.md](install.md) for fresh `.agentbridge`, reused `.deepbox`, and
explicit custom-root selection. No data is automatically moved or copied.

## 1. First deployment owner (local mode)

When the database has no bootstrap owner, open the root page. The setup panel asks for a username, display name, and password, then calls:

```text
POST /api/auth/bootstrap
```

This route works exactly once and creates the first deployment-level owner. It is separate from workspace roles.

## 2. Local password-account onboarding

Production should keep public self-registration disabled:

```env
AGENTBRIDGE_REGISTRATION_ENABLED=false
```

A deployment owner can create an account invitation from **Manage users**. The server returns a plaintext invitation code and join link once; the database stores only a SHA-256 hash plus a short preview. The recipient opens the link, chooses a username/password, and redeems the invitation atomically. Invalid, expired, revoked, exhausted, and conflicting claims all return the same opaque response.

These local account invitations do **not** add workspace access. Add the account as an existing member from the workspace manager afterward.

## 3. Microsoft account sign-in

On an Azure deployment configured per [azure-deployment.md](azure-deployment.md):

1. Select **Continue with Microsoft**.
2. agentbridge redirects through the unchanged `/.auth/login/aad`; App Service validates the provider response.
3. `/api/auth/microsoft/callback` maps the injected tenant + subject to an agentbridge user and issues the existing time-limited `deepbox_session` cookie.
4. agentbridge stores no Microsoft access or refresh token.

New external users become deployment members and receive a personal workspace. Emails listed in `AGENTBRIDGE_MICROSOFT_OWNER_EMAILS` become deployment owners; during migration, an allow-listed identity may claim the sole unlinked local owner. Keep this allow-list narrow and normalized.

## 4. Create and share a workspace

Any signed-in user can create another workspace. Its creator is the workspace `owner`. The left panel renders:

```text
Workspace
  Devbox
    Agent
```

Every member can discover the Devboxes and Agents inside that workspace. Permissions are role-based:

- `viewer`: observe workspace resources and sessions
- `operator`: viewer rights plus input/message operations
- `admin`: manage members and invite `viewer`/`operator` users
- `owner`: admin rights, may grant `admin`, and is protected as the last owner

Open the workspace manager to either add an existing enabled username or create an email invitation. A workspace invitation is single-use, expires after `AGENTBRIDGE_WORKSPACE_INVITATION_TTL_DAYS`, can be revoked, and is bound to a normalized email. Reissuing an invitation for the same workspace/email invalidates the prior pending link.

The token remains in `#workspace-invite=...` so it is not sent in the initial HTTP request. The UI stores it in `sessionStorage` across the Microsoft OAuth redirect, previews it with `POST /api/workspace-invitations/preview`, and accepts it with `POST /api/workspace-invitations/accept`. The signed-in account email must match exactly.

## 5. Disable or re-enable users

Deployment owners can disable or re-enable accounts from **Manage users**. Disabling rejects new requests and login attempts and actively closes that user's current WebSocket connections. Workspace membership alone never grants deployment-owner controls.

## 6. Recommended production settings

```env
AGENTBRIDGE_ENV=production
AGENTBRIDGE_REGISTRATION_ENABLED=false
AGENTBRIDGE_COOKIE_SECURE=true
AGENTBRIDGE_COOKIE_SAMESITE=lax
AGENTBRIDGE_SESSION_TTL_SECONDS=28800
AGENTBRIDGE_WORKSPACE_INVITATION_TTL_DAYS=7
```

Also set `AGENTBRIDGE_ALLOWED_ORIGINS` to exact trusted HTTPS origins and configure a stable `AGENTBRIDGE_SECRET`. Legacy keys remain accepted; do not change existing Azure settings during the local rename/review phase. For Microsoft sign-in, follow the Easy Auth trust-boundary and secret-handling checklist in [azure-deployment.md](azure-deployment.md).
