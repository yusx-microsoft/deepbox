# AgentBridge: three-machine remote deployment over a Tailscale private network

> Target topology: computer A opens the browser; computer B hosts the AgentBridge
> server; computer C runs the connector and the real agents. The three
> computers are not on the same LAN but join the same Tailscale tailnet.
>
> This guide targets the current private alpha. **Use Tailscale Serve, not
> Tailscale Funnel, and never expose the Uvicorn port directly to the public
> internet.**

The only canonical repository and production installation source is
[yusx-swapp/AgentBridge](https://github.com/yusx-swapp/AgentBridge);
`yusx-microsoft/AgentBridge` is only a fork. Verify the reviewed scripts/packages
are published to canonical `main` before using its installer URLs.

Keep the existing worktree at `C:\Code\deepbox`; the rename does not move local
files. Existing Azure resources, auth, database, spool and IPC identifiers remain
unchanged. Fresh connector installations use `~/.agentbridge`; existing `.deepbox`
or custom install roots remain compatible, without automatic migration. The legacy
`DEEPBOX_*` server settings below are still supported; canonical `AGENTBRIDGE_*`
settings take precedence by presence.

---

## 1. Topology and trust boundary

```text
Computer A — Viewer
  Browser
     │ HTTPS / WSS
     ▼
Tailscale WireGuard private network
     │
     ▼
Computer B — Server host
  Tailscale Serve (TLS termination)
     │ http://127.0.0.1:8077
     ▼
  AgentBridge server
  ├── SQLite metadata
  └── session DVR
     ▲
     │ HTTPS / WSS (same tailnet)
     │
Computer C — Agent devbox
  deepbox connector
     │ PTY
     ▼
  Claude Code / Codex / Copilot
```

Security boundary:

- The agent CLI, provider API keys, sign-in state, and code directories stay
  only on computer C.
- The server receives only the terminal event stream and non-secret config.
- Tailscale provides device-to-device WireGuard encryption and private DNS.
- deepbox sign-in and devbox tokens remain the application-layer authentication.
- Uvicorn on computer B listens only on `127.0.0.1`; neither the LAN nor the
  public internet can reach port 8077 directly.

---

## 2. Prerequisites

On all three computers:

1. Install Tailscale: <https://tailscale.com/download/windows>
2. Sign in to the same tailnet.
3. Verify from a Windows command prompt:

   ```bat
   tailscale status
   ```

The computers should appear in each other's device list. Enabling MagicDNS in
the Tailscale admin console is recommended.

> Do not run `tailscale funnel`. Funnel publishes the service to the public
> internet, which is not appropriate for the current security stage.

---

## 3. Computer B: deploy the server host

### 3.1 Install the code and dependencies

```bat
cd /d C:\Code
git clone --origin upstream https://github.com/yusx-swapp/AgentBridge.git deepbox
cd /d C:\Code\deepbox
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

The explicit `deepbox` destination preserves this guide's `C:\Code\deepbox`
layout; without it, Git names a fresh checkout `AgentBridge`. Clone only into a
new destination; do not rename or overwrite an existing `C:\Code\deepbox`
worktree. For an existing **deployment-only** checkout, first
confirm `upstream` points to the canonical repository, the worktree is clean and
its checked-out deployment branch is intended to track published upstream `main`:

```bat
cd /d C:\Code\deepbox
git fetch upstream
git merge --ff-only upstream/main
.venv\Scripts\python -m pip install -r requirements.txt
```

Do not apply that deployment update to an active development branch or live
process without planning the update. For code changes, create a feature branch
from canonical `upstream/main` and open a PR against **yusx-swapp/AgentBridge:main**;
do not develop directly on `main` or use a fork as production upstream.

### 3.2 Configure Tailscale Serve

With a current Tailscale CLI:

```bat
tailscale serve --bg http://127.0.0.1:8077
tailscale serve status
```

The command prints an HTTPS URL that is reachable only inside the tailnet, for
example:

```text
https://server-name.example-tailnet.ts.net
```

The Serve CLI differs slightly between Tailscale versions. If the command is
rejected, run `tailscale serve --help` and use its equivalent "HTTPS proxy to
`http://127.0.0.1:8077`" form.

### 3.3 Create the server configuration

```bat
cd /d C:\Code\deepbox
copy .env.example .env
py -3 -c "import secrets; print(secrets.token_urlsafe(48))"
notepad .env
```

Write the random value and the Tailscale HTTPS URL into `.env`:

```dotenv
DEEPBOX_ENV=production
DEEPBOX_SECRET=<the value you just generated>
DEEPBOX_DATABASE_URL=sqlite:///C:/deepbox-data/deepbox.db
DEEPBOX_DATA_DIR=C:/deepbox-data
DEEPBOX_PUBLIC_URL=https://server-name.example-tailnet.ts.net
DEEPBOX_ALLOWED_ORIGINS=https://server-name.example-tailnet.ts.net
DEEPBOX_COOKIE_SECURE=true
DEEPBOX_COOKIE_SAMESITE=lax
DEEPBOX_HOST=127.0.0.1
DEEPBOX_PORT=8077
DEEPBOX_PLATFORM=local
DEEPBOX_REGISTRATION_ENABLED=false
```

Notes:

- `.env` is gitignored and must not be committed.
- The URL has no trailing `/`.
- Changing `DEEPBOX_SECRET` invalidates existing browser sign-in cookies. This
  is expected.
- Keep SQLite and the DVR under `C:\deepbox-data`, never inside the Git
  repository.

### 3.4 Start the server

```bat
cd /d C:\Code\deepbox
scripts\start-server.cmd
```

Equivalent command:

```bat
.venv\Scripts\python -m server
```

In production mode the server fails closed on startup unless:

- the secret is not a development default,
- allowed origins are non-empty,
- secure cookies are enabled, and
- the port is valid.

### 3.5 Verify

From computer B or any device in the tailnet:

```bat
curl https://server-name.example-tailnet.ts.net/api/health
curl https://server-name.example-tailnet.ts.net/api/ready
```

Expected:

```json
{"status":"ok","protocol_version":3}
{"status":"ready","protocol_version":3}
```

`health` confirms the process is alive; `ready` also checks the database and
the recording directory.

---

## 4. Computer A: browser sign-in

Computer A only needs Tailscale and a browser; it does not clone deepbox.

Open:

```text
https://server-name.example-tailnet.ts.net
```

First use:

1. Create the first account through the setup panel.
2. Sign in.
3. Create a devbox (this represents computer C).
4. Copy the full `hpc_box_...` token. The complete token is shown only once.
5. Create an agent under that devbox:
   - handle: `claude`
   - runtime: `claude-code`
   - cwd: a working directory that really exists on computer C

Do not use this devbox token to start a connector on computer A or B; it belongs
to computer C.

---

## 5. Computer C: run the agent devbox

### 5.1 Verify the local agent

```bat
where claude
claude --version
claude
```

Complete the Claude Code sign-in locally on computer C first. AgentBridge never
touches Claude credentials.

### 5.2 Install the local `agentbridge` command once

Run once in PowerShell:

```powershell
irm https://raw.githubusercontent.com/yusx-swapp/AgentBridge/main/scripts/install.ps1 | iex
```

The installer maintains the connector source, an isolated venv, and a stable
command under the current user's `~\.agentbridge` for a fresh install, and adds
that installation's `bin` directory to the user PATH. Existing `.deepbox` or
custom roots can be reused; `.deepbox` in their output is compatibility, not the
fresh-install name. The payload comes from canonical `yusx-swapp/AgentBridge`,
not a mirror or fork. Later connections never clone, download, or refresh that
directory; only an explicit `agentbridge upgrade` reruns the installer. Legacy
`deepbox` aliases remain supported. See [install.md](install.md) for home selection,
publication checks and [`SOURCE_ZIP` 404 troubleshooting](install.md#source_zip-http-404).

### 5.3 Verify the server is reachable

```bat
curl https://server-name.example-tailnet.ts.net/api/health
```

It must return `status=ok`. If the name does not resolve, check
`tailscale status` and MagicDNS first.

### 5.4 Run connection diagnostics

Set the token copied from the UI and run doctor:

```powershell
$env:AGENTBRIDGE_SERVER_URL = 'https://server-name.example-tailnet.ts.net'
$env:AGENTBRIDGE_TOKEN = 'hpc_box_...'
agentbridge doctor
```

It checks URL/TLS, `/api/health`, protocol version, and token authentication in
turn, and never prints the token. Start the connector only after every check
reports `[OK]`.

### 5.5 Start the connector

Keeping the same environment variables, run:

```powershell
agentbridge connect
```

This starts the already-installed connector from the current working directory.
It never invokes the installer or refreshes the selected installation's `app`
directory (`~\.agentbridge\app` for a fresh install).

The connector:

1. Verifies the token over HTTPS `GET /api/me`.
2. Checks the server protocol version.
3. Probes local runtimes.
4. Establishes `/ws/devbox` over WSS.
5. Reports live sessions.
6. Starts or resumes a PTY locally when the user chooses New/Resume.

The token is sent only through the `Authorization: Bearer` header. The server
does not accept a WS query-string token, keeping tokens out of URLs, proxy logs,
and browser history.

### 5.6 Optional: split supervisor / transport processes

The default command runs the compatible all-in-one mode. To let the network
transport restart independently while the local PTYs keep running, use the same
`AGENTBRIDGE_SERVER_URL` and `AGENTBRIDGE_TOKEN` in two terminals. Start the long-lived
session supervisor first:

```powershell
agentbridge connect --mode supervisor
```

Then start the restartable network transport:

```powershell
agentbridge connect --mode transport
```

They communicate over a per-user Windows named pipe (a `0600` Unix socket on
POSIX) and perform a mutual HMAC handshake with a 5-second timeout using a
user-local key; frames are newline-delimited JSON up to 1 MiB and never use
pickle. Only one transport is accepted at a time. Stopping or restarting the
transport does not close the supervisor's PTYs; only stopping the supervisor
closes them. Pending PTY output is held in a durable on-disk spool
(`connector/spool.py`) backed by SQLite WAL with `synchronous=FULL`: output is
committed to a local outbox keyed by `(session_id, pty_instance_id, seq)` before
the transport sends it. A successful `ws.send()` does not delete the record; the
supervisor advances `ack_state` and drops the head only after the server commits
the same output to `recording_frames` and returns an exactly matching protocol
v3 ACK. A disconnect after persistence but before ACK triggers a resend that the
server idempotently re-ACKs; a gap yields a precise resend and a conflict fails
closed. The spool is named deterministically from the server URL plus a token
hash without storing the token, and its directory is made user-private on a
best-effort basis. Input is deduplicated through a durable `client_input_id`
receipt, and the browser ACK is returned only after the connector confirms PTY
delivery.

---

## 6. End-to-end acceptance

On computer A:

1. Refresh deepbox.
2. Confirm the devbox for computer C shows green/online.
3. Confirm `@claude` is online.
4. Open or create a session.
5. See the real Claude Code TUI running on computer C.
6. Type a test message and confirm the reply.

Then verify the platform value:

1. Close the browser tab on computer A.
2. Let Claude keep running on computer C.
3. Reopen the page and resume the same live session.
4. Confirm the screen and context are still present.
5. Restart the server on computer B without stopping the connector on
   computer C.
6. Confirm the connector reconnects automatically and the session recovers.

---

## 7. Tailscale ACL recommendation

By default other tailnet members may be able to reach the Serve URL. deepbox
still requires sign-in, but add network-layer least privilege:

- Computer A may reach computer B's HTTPS service.
- Computer C may reach computer B's HTTPS service.
- Other devices may not reach computer B's deepbox service.
- No device needs direct access to agent ports on computer C; the connector
  makes only outbound connections.

The exact ACL/Grants syntax depends on your tailnet policy; consult current
Tailscale documentation before applying. Do not substitute Funnel for ACLs out
of convenience.

---

## 8. Troubleshooting

### The browser cannot open the URL

```bat
tailscale status
tailscale serve status
curl https://<server>/api/health
```

Check that the server command window on computer B is still running.

### `/api/ready` returns 503

Check:

- The `DEEPBOX_DATABASE_URL` directory exists or can be created.
- `DEEPBOX_DATA_DIR` is writable.
- The Windows user running the server has directory permissions.

### The browser signs in, but the terminal WebSocket is rejected

Check `.env`:

```text
DEEPBOX_PUBLIC_URL
DEEPBOX_ALLOWED_ORIGINS
```

They must match the origin in the browser address bar exactly (scheme, host,
and port). Restart the server after changing `.env`.

### The connector reports a protocol mismatch

The server and computer C are on incompatible versions. Plan an explicit update
to compatible reviewed builds from canonical `yusx-swapp/AgentBridge`: update the
server deployment checkout and dependencies, then rerun the canonical installer
or `agentbridge upgrade` on computer C when the matching payload is published.
The installed connector uses an archive, not a Git checkout; `git pull` on it is
not an upgrade. Do not rewrite a feature branch or live data to fix a mismatch.

### The installer reports SOURCE_ZIP HTTP 404

Check the archive URL and both `AGENTBRIDGE_SOURCE_ZIP` / `DEEPBOX_SOURCE_ZIP`
overrides. A missing/inaccessible archive or unpublished ref is not an install-home
or token-migration problem. See the [step-by-step diagnosis and optional reviewed
archive override](install.md#source_zip-http-404). A payload override cannot fix a
404 for the raw installer itself; never switch production to a fork as a workaround.

### The connector returns 401 / 4001

- The token was pasted incorrectly.
- The token was revoked.
- The token belongs to a different devbox.
- The environment variable carries extra quotes or spaces.

Rotate a new token in the UI for the devbox that represents computer C; do not
reuse another devbox's token.

### The devbox is online, but the agent fails to start

Check locally on computer C:

```bat
where claude
claude --version
```

Confirm that the agent's configured `cwd` exists on computer C.

### HTTPS certificate problems

Use only the HTTPS domain produced by Tailscale Serve. Do not put `https://` in
front of a bare Tailscale IP. Make sure computer B has HTTPS/Serve enabled in
the tailnet.

---

## 9. Stop and roll back

Stop the server: press `Ctrl+C` in the server window on computer B.

Stop the connector: press `Ctrl+C` in the connector window on computer C. In
all-in-one mode this also terminates the PTY sessions it hosts. To keep PTYs
alive across a transport restart, run the split supervisor/transport mode in
[section 5.6](#56-optional-split-supervisor--transport-processes).

Turn off Tailscale Serve, using the form for your current CLI:

```bat
tailscale serve reset
```

This does not delete AgentBridge data.

---

## 10. Current limitations

- The server and connector are not yet installed as Windows services; a command
  window must stay open.
- In all-in-one mode, a connector exit drops its PTYs. Use the split
  supervisor/transport mode to survive transport restarts.
- This is not a public-internet deployment configuration.
- Tailscale addresses network encryption and device reachability; it does not
  replace AgentBridge application-layer authentication, permissions, and recording
  privacy.
