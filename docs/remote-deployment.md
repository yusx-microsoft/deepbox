# Three-machine remote deployment over a Tailscale private network

> Target topology: computer A opens the browser; computer B hosts the deepbox
> server; computer C runs the connector and the real agents. The three
> computers are not on the same LAN but join the same Tailscale tailnet.
>
> This guide targets the current private alpha. **Use Tailscale Serve, not
> Tailscale Funnel, and never expose the Uvicorn port directly to the public
> internet.**

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
  deepbox server
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
git clone https://github.com/yusx-swapp/deepbox.git
cd /d C:\Code\deepbox
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

If the repository already exists:

```bat
cd /d C:\Code\deepbox
git pull
.venv\Scripts\python -m pip install -r requirements.txt
```

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

Complete the Claude Code sign-in locally on computer C first. deepbox never
touches Claude credentials.

### 5.2 Install the local `deepbox` command once

Run once in PowerShell:

```powershell
irm https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.ps1 | iex
```

The installer maintains the connector source, an isolated venv, and a stable
command under the current user's `~\.deepbox`, and adds `~\.deepbox\bin` to the
user PATH. It downloads the connector payload from the public `yusx-swapp/deepbox`
mirror. Later connections never clone, download, or refresh that directory; only
an explicit `deepbox upgrade` reruns the installer. See
[install.md](install.md) for details.

### 5.3 Verify the server is reachable

```bat
curl https://server-name.example-tailnet.ts.net/api/health
```

It must return `status=ok`. If the name does not resolve, check
`tailscale status` and MagicDNS first.

### 5.4 Run connection diagnostics

Set the token copied from the UI and run doctor:

```powershell
$env:DEEPBOX_SERVER_URL = 'https://server-name.example-tailnet.ts.net'
$env:DEEPBOX_TOKEN = 'hpc_box_...'
deepbox doctor
```

It checks URL/TLS, `/api/health`, protocol version, and token authentication in
turn, and never prints the token. Start the connector only after every check
reports `[OK]`.

### 5.5 Start the connector

Keeping the same environment variables, run:

```powershell
deepbox connect
```

This starts the already-installed connector from the current working directory.
It never invokes the installer or refreshes `~\.deepbox\app`.

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
`DEEPBOX_SERVER_URL` and `DEEPBOX_TOKEN` in two terminals. Start the long-lived
session supervisor first:

```powershell
deepbox connect --mode supervisor
```

Then start the restartable network transport:

```powershell
deepbox connect --mode transport
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

The server and computer C are on different repository versions. Run `git pull`
on both ends and reinstall dependencies (rerun the installer or
`deepbox upgrade` on computer C).

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

This does not delete deepbox data.

---

## 10. Current limitations

- The server and connector are not yet installed as Windows services; a command
  window must stay open.
- In all-in-one mode, a connector exit drops its PTYs. Use the split
  supervisor/transport mode to survive transport restarts.
- This is not a public-internet deployment configuration.
- Tailscale addresses network encryption and device reachability; it does not
  replace deepbox application-layer authentication, permissions, and recording
  privacy.
