# Onboarding and account management

Deepbox has two identity paths and two different invitation types:

- **Local account invitation**: a deployment owner creates a new password account.
- **Workspace invitation**: a workspace owner/admin grants an existing or future Microsoft identity access to one workspace.

The browser reads `GET /api/auth/config` and only shows the sign-in methods enabled by `DEEPBOX_AUTH_MODE`:

| Mode | Local password | Microsoft Easy Auth | Intended use |
|---|---:|---:|---|
| `local` | yes | no | local development and the safe default |
| `hybrid` | yes | yes | migrating an existing deployment |
| `microsoft` | no | yes | Microsoft-only Azure deployment |

`microsoft` and `hybrid` are safe only behind correctly configured Azure App Service Easy Auth. A directly reachable ASGI server must stay in `local` mode because client-supplied `X-MS-CLIENT-PRINCIPAL*` headers are not an identity boundary.

## 1. First deployment owner (local mode)

When the database has no bootstrap owner, open the root page. The setup panel asks for a username, display name, and password, then calls:

```text
POST /api/bootstrap
```

This route works exactly once and creates the first deployment-level owner. It is separate from workspace roles.

## 2. Local password-account onboarding

Production should keep public self-registration disabled:

```env
DEEPBOX_REGISTRATION_ENABLED=false
```

A deployment owner can create an account invitation from **Manage users**. The server returns a plaintext invitation code and join link once; the database stores only a SHA-256 hash plus a short preview. The recipient opens the link, chooses a username/password, and redeems the invitation atomically. Invalid, expired, revoked, exhausted, and conflicting claims all return the same opaque response.

These local account invitations do **not** add workspace access. Add the account as an existing member from the workspace manager afterward.

## 3. Microsoft account sign-in

On an Azure deployment configured per [azure-deployment.md](azure-deployment.md):

1. Select **Continue with Microsoft**.
2. Deepbox redirects through `/.auth/login/aad`; App Service validates the provider response.
3. `/api/auth/microsoft/callback` maps the injected tenant + subject to a Deepbox user and issues a time-limited `deepbox_session` cookie.
4. Deepbox stores no Microsoft access or refresh token.

New external users become deployment members and receive a personal workspace. Emails listed in `DEEPBOX_MICROSOFT_OWNER_EMAILS` become deployment owners; during migration, an allow-listed identity may claim the sole unlinked local owner. Keep this allow-list narrow and normalized.

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

Open the workspace manager to either add an existing enabled username or create an email invitation. A workspace invitation is single-use, expires after `DEEPBOX_WORKSPACE_INVITATION_TTL_DAYS`, can be revoked, and is bound to a normalized email. Reissuing an invitation for the same workspace/email invalidates the prior pending link.

The token remains in `#workspace-invite=...` so it is not sent in the initial HTTP request. The UI stores it in `sessionStorage` across the Microsoft OAuth redirect, previews it with `POST /api/workspace-invitations/preview`, and accepts it with `POST /api/workspace-invitations/accept`. The signed-in account email must match exactly.

## 5. Disable or re-enable users

Deployment owners can disable or re-enable accounts from **Manage users**. Disabling rejects new requests and login attempts and actively closes that user's current WebSocket connections. Workspace membership alone never grants deployment-owner controls.

## 6. Recommended production settings

```env
DEEPBOX_ENV=production
DEEPBOX_REGISTRATION_ENABLED=false
DEEPBOX_COOKIE_SECURE=true
DEEPBOX_COOKIE_SAMESITE=lax
DEEPBOX_INVITATION_TTL_HOURS=72
DEEPBOX_INVITATION_MAX_USES=1
DEEPBOX_SESSION_TTL_SECONDS=28800
DEEPBOX_WORKSPACE_INVITATION_TTL_DAYS=7
```

Also set `DEEPBOX_ALLOWED_ORIGINS` to exact trusted HTTPS origins and configure a stable `DEEPBOX_SECRET`. For Microsoft sign-in, follow the Easy Auth trust-boundary and secret-handling checklist in [azure-deployment.md](azure-deployment.md).
