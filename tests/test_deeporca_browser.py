"""Optional real Chromium checks of the shipped workbench (not the prototype).

Uses only installed Playwright/Chromium, a read-only ephemeral loopback static
server, and in-memory API/WebSocket fixtures. No connector/model E2E is claimed.
Run: python -m pytest tests/test_deeporca_browser.py -q
Set DEEPORCA_BROWSER_ARTIFACTS to retain screenshots outside the repository.
"""
from __future__ import annotations

import json
import copy
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"

NATIVE = {
    "schema_version": 2, "runtime": "deeporca", "installation": {"status": "installed"},
    "family": "deeporca", "backend": "python-library", "compatibility": {"status": "compatible"},
    "features": {"renderer": "deeporca-chat-v1", "interactive_approval": False},
    "agent_config": {"profile_modes": ["create"], "configuration_templates": [
        {"id": "connector-default", "label": "Connector default"}], "execution_policy": "non_interactive"},
    "surfaces": [{"id": "structured", "default": True, "available": True,
                  "features": {"renderer": "deeporca-chat-v1", "interactive_approval": False, "controls": [
                      {"key": "permission_mode", "kind": "select", "label": "Permission mode", "choices": ["ask", "allow"]}
                  ]}}]
}
CLI = {"schema_version": 2, "runtime": "cli", "installation": {"status": "installed"},
       "surfaces": [{"id": "structured", "default": True, "available": True,
                     "features": {"structured": True, "controls": [
                         {"key": "permission_mode", "kind": "select", "label": "Permission mode", "choices": ["ask", "allow"]}
                     ]}}]}
PROJECT = {"id": "project-local-1", "name": "Registered browser fixture project"}

SOCKET_FIXTURE = r"""
window.__sockets = []; window.__copied = []; window.__xss = 0;
Object.defineProperty(navigator, 'clipboard', {configurable:true, value:{
  writeText: async text => { window.__copied.push(text); }
}});
window.WebSocket = class FixtureSocket {
  constructor(url) {
    this.url=url; this.readyState=0; this.frames=[]; window.__sockets.push(this);
    setTimeout(()=>{this.readyState=1; this.onopen?.({});},0);
  }
  send(raw) {
    const frame=JSON.parse(raw); this.frames.push(frame);
    if(frame.type==='attach' || frame.type==='resume') {
      this.sessionId=frame.session_id;
      setTimeout(()=>{
        this.emit({type:'collaboration',role:'owner',can_operate:true,can_send_messages:true});
        const launchId=frame.type==='resume' ? 'fixture-resumed-'+this.sessionId
          : frame.launch_id || 'fixture-live-'+this.sessionId;
        // Resume changes generation only through starting, never an unsolicited ready.
        if(frame.type==='resume') this.emit({type:'status',state:'starting',launch_id:launchId});
        this.emit({type:'ready',launch_id:launchId});
      },0);
    }
  }
  emit(frame) { this.onmessage?.({data:JSON.stringify({session_id:this.sessionId,...frame})}); }
  close() { this.readyState=3; }
};
window.__emit = (session, frame) => {
  const socket=window.__sockets.findLast(item=>item.sessionId===session && item.readyState===1);
  if(!socket) throw new Error('Missing fixture socket '+session);
  socket.emit(frame);
};
"""


@pytest.fixture
def browser_workbench():
    playwright = pytest.importorskip("playwright.sync_api")

    class QuietHandler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/static/"):
                self.path = self.path[len("/static"):]
            super().do_GET()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(WEB)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    machine = {"id": "m", "workspace_id": "w", "name": "Fixture Machine", "online": True,
        "capabilities": {"runtimes": [copy.deepcopy(NATIVE), copy.deepcopy(CLI)]}, "projects": [dict(PROJECT)], "agents": [
            {"id": "native", "handle": "orca", "display_name": "DeepOrca fixture", "runtime": "deeporca", "presence": "idle"},
            {"id": "cli", "handle": "cli", "display_name": "CLI fixture", "runtime": "cli", "presence": "idle"}]}
    state = {"machine": machine, "requests": [], "external": [], "errors": [], "sessions": {}}

    def route(request_route):
        request = request_route.request
        url = urlsplit(request.url)
        if f"{url.scheme}://{url.netloc}" != origin:
            state["external"].append(request.url)
            request_route.abort()
            return
        path = url.path
        if not path.startswith("/api/"):
            request_route.continue_()
            return
        payload = json.loads(request.post_data) if request.post_data else None
        state["requests"].append((request.method, path, payload))
        if path == "/api/me/user":
            result = {"id": "u", "username": "fixture", "role": "owner"}
        elif path == "/api/auth/config":
            result = {"mode": "local", "password_enabled": True}
        elif path == "/api/workspaces":
            result = [{"id": "w", "name": "Browser fixture", "role": state.get("role", "owner"), "is_personal": True}]
        elif path == "/api/devboxes":
            result = [machine]
        elif path == "/api/devboxes/m/agents" and request.method == "POST":
            # Deliberately enforce the real DeepOrca server contract in the fixture.
            if state.get("create_error"):
                request_route.fulfill(status=409, json={"detail": state["create_error"]})
                return
            if payload["runtime"] == "deeporca" and payload["local_project_id"] not in {p["id"] for p in machine["projects"]}:
                request_route.fulfill(status=400, json={"detail": "registered local project required"})
                return
            result = {"id": "created-agent", **payload,
                      "runtime_status": {"state": "needs_configuration", "code": "model_not_configured"}}
            machine["agents"].append(result)
        elif path == "/api/agents/created-agent/runtime/retry" and request.method == "POST":
            result = machine["agents"][-1]
            result["runtime_status"] = {"state":"pending", "code":"retry_requested"}
        elif path == "/api/agents/created-agent" and request.method == "PATCH":
            if state.get("patch_error"):
                request_route.fulfill(status=409, json={"detail": state["patch_error"]})
                return
            result = machine["agents"][-1]
            result["display_name"] = request.post_data_json["display_name"]
            if "runtime_config" in payload:
                previous = result.get("runtime_config", {})
                result["runtime_config"] = {**previous, **payload["runtime_config"]}
        elif path.startswith("/api/agents/") and path.endswith("/sessions"):
            agent = path.split("/")[3]
            if request.method == "POST":
                result = {"id": "session-" + agent, "agent_id": agent, "surface": "structured", "state": "active"}
                state["sessions"][result["id"]] = result
            else:
                result = [s for s in state["sessions"].values() if s["agent_id"] == agent]
        elif path.endswith("/messages"):
            result = []
        elif path.startswith('/api/sessions/') and path.endswith('/replay'):
            session = state['sessions'][path.split('/')[-2]]
            result = {"session":session, "surface":session['surface'], "renderer":"deeporca-chat-v1", "events":state.get('replay_events', []), "checkpoints":[]}
        elif path.startswith("/api/sessions/") and path.split("/")[-1] in state["sessions"]:
            result = state["sessions"][path.split("/")[-1]]
        else:
            state["errors"].append("Unexpected API " + path)
            request_route.fulfill(status=404, json={"detail": "unexpected fixture API"})
            return
        request_route.fulfill(json=result)

    try:
        with playwright.sync_playwright() as p:
            if not Path(p.chromium.executable_path).is_file():
                pytest.skip("Installed Playwright Chromium is required; this test never downloads it")
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            context.route("**/*", route)
            context.add_init_script(SOCKET_FIXTURE)
            page = context.new_page()
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: state["errors"].append(str(error)))
            page.goto(origin + "/index.html")
            try:
                page.locator('[data-machine-menu="m"]').wait_for()
            except Exception:
                screenshot(page, "deeporca-workbench-startup-failure.png")
                raise AssertionError({"page": page.locator("body").inner_text(), "errors": state["errors"], "requests": state["requests"]})
            state.update(page=page, context=context, origin=origin)
            yield state
            assert state["external"] == [], "No outbound requests are permitted"
            assert state["errors"] == []
            context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def screenshot(page, name):
    directory = os.environ.get("DEEPORCA_BROWSER_ARTIFACTS")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(directory) / name), full_page=True)


def emit(page, agent, frame):
    if frame.get("kind") == "event" and not isinstance(frame.get("data"), str):
        events = frame["data"] if isinstance(frame["data"], list) else [frame["data"]]
        frame = {**frame, "data": "\n".join(json.dumps(event) for event in events) + "\n"}
    page.evaluate("([session,frame])=>window.__emit(session,frame)", ["session-" + agent, frame])


def test_real_workbench_add_agent_project_and_status(browser_workbench):
    h = browser_workbench
    page, machine = h["page"], h["machine"]
    machine["projects"] = []
    page.locator('[data-machine-menu="m"]').click()
    page.get_by_role("menuitem", name="Add agent", exact=True).click()
    form = page.get_by_role("dialog", name="Add agent", exact=True)
    project = form.locator('[data-field="local_project_id"]')
    handle = form.locator('[data-field="handle"]')
    handle.fill("Browser helper")
    form.get_by_label('Endpoint', exact=True).fill('http://localhost:11434/v1')
    form.get_by_label('Model', exact=True).fill('provider/model-name')
    form.get_by_label('Context window · tokens').fill('32768')
    form.get_by_label('Authentication', exact=True).select_option('none')
    assert form.get_by_label('Reasoning effort', exact=True).input_value() == ''
    assert project.evaluate("el=>el.required")
    assert "DeepOrca requires a registered local project" in form.inner_text()
    form.get_by_role("button", name="Add agent", exact=True).click()
    assert project.evaluate("el=>el.validity.valueMissing")
    assert not [r for r in h["requests"] if r[0] == "POST"]
    runtime = form.locator('[data-field="runtime"]')
    runtime.select_option("cli")
    assert not form.get_by_role('group', name='Model connection').is_visible()
    assert not project.evaluate("el=>el.required")
    assert "No project (runtime default)" in project.inner_text()
    runtime.select_option("deeporca")
    assert form.get_by_label('Model', exact=True).input_value() == 'provider/model-name'
    form.locator('.local-action-guide > summary').click()
    machine["projects"] = [dict(PROJECT)]
    form.get_by_role("button", name="Refresh projects").click()
    project.locator('option[value="project-local-1"]').wait_for(state="attached")
    project.select_option(PROJECT["id"])
    form.get_by_role("button", name="Refresh projects").click()
    page.wait_for_function("!document.querySelector('[data-refresh-projects]').disabled")
    assert project.input_value() == PROJECT["id"]
    machine["projects"] = []
    form.get_by_role("button", name="Refresh projects").click()
    page.wait_for_function("document.querySelector('[data-field=local_project_id]').value === ''")
    assert project.evaluate("el=>el.validity.valueMissing")
    machine["projects"] = [dict(PROJECT)]
    form.get_by_role("button", name="Refresh projects").click()
    project.locator('option[value="project-local-1"]').wait_for(state="attached")
    project.select_option(PROJECT["id"])
    form.get_by_role("button", name="Add agent", exact=True).click()
    status = page.get_by_role("dialog", name="Agent settings", exact=True)
    status.wait_for()
    assert "Needs configuration" in status.inner_text()
    posts = [r for r in h["requests"] if r[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][2] == {"handle": "Browser helper", "display_name": "Browser helper", "runtime": "deeporca",
        "local_project_id": PROJECT["id"], "runtime_config": {"integration_version": 1,
        "profile": {"mode": "create", "configuration_template_ref": "connector-default"},
        "llm": {"provider": "openai", "base_url": "http://localhost:11434/v1", "model": "provider/model-name",
                "context_window": 32768, "reasoning_effort": ""}, "credential": {"mode": "none"}}}
    status.get_by_role("button", name="Retry initialization", exact=True).click()
    page.wait_for_function("document.querySelector('[data-runtime-status]')?.textContent.includes('Pending')")
    retries = [r for r in h["requests"] if r[0] == "POST" and r[1].endswith('/runtime/retry')]
    assert len(retries) == 1 and retries[0][2] is None
    machine["agents"][-1]["runtime_status"] = {"state": "ready"}
    status.get_by_role("button", name="Refresh status").click()
    page.wait_for_function("document.querySelector('[data-runtime-status]')?.textContent.includes('Ready')")
    assert "not been verified" in status.inner_text()
    assert len([r for r in h["requests"] if r[0] == "POST" and r[1].endswith('/agents')]) == 1
    status.get_by_role('textbox', name='Agent name', exact=True).fill('Renamed native Agent')
    status.get_by_role('button', name='Save settings', exact=True).click()
    status.get_by_role('button', name='Saved', exact=True).wait_for()
    assert machine['agents'][-1]['display_name'] == 'Renamed native Agent'
    expected = copy.deepcopy(posts[0][2]['runtime_config'])
    expected.pop('credential')
    assert [r[2] for r in h['requests'] if r[0] == 'PATCH'] == [{'display_name':'Renamed native Agent', 'runtime_config':expected}]
    screenshot(page, "deeporca-add-agent-ready.png")
    page.keyboard.press("Escape")
    status.wait_for(state="detached")
    page.locator('[data-agent-menu="created-agent"]').click()
    page.get_by_role('menuitem', name='Agent settings', exact=True).click()
    page.get_by_role('dialog', name='Agent settings', exact=True).wait_for()
    assert page.get_by_role('textbox', name='Agent name', exact=True).input_value() == 'Renamed native Agent'


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}], ids=['desktop', 'mobile'])
def test_real_workbench_bind_existing_profile(browser_workbench, viewport):
    """Native binding uses inventory + explicit consent, never model/credential/CLI fields."""
    h = browser_workbench
    page, machine = h['page'], h['machine']
    profile_ref = 'native-' + 'a' * 32
    capability = machine['capabilities']['runtimes'][0]['agent_config']
    capability.update(profile_modes=['create', 'bind'], existing_profiles=[{'id': profile_ref, 'label': 'Research native profile'}])
    page.locator('[data-machine-menu="m"]').click()
    page.get_by_role('menuitem', name='Add agent', exact=True).click()
    form = page.get_by_role('dialog', name='Add agent', exact=True)
    page.set_viewport_size(viewport)
    form.get_by_label('Handle', exact=True).fill('Native research helper')
    form.get_by_label('Local project', exact=True).select_option(PROJECT['id'])
    mode = form.get_by_label('Profile', exact=True)
    assert mode.input_value() == 'create'
    assert 'minimal security: broad tools, no approvals' in form.inner_text()
    assert 'trusted Connector template may override' in form.inner_text()
    form.get_by_label('Endpoint', exact=True).fill('http://localhost:11434/v1')
    form.get_by_label('Model', exact=True).fill('draft/local-model')
    form.get_by_label('Context window · tokens').fill('32768')
    # A live input can retain a draft, but it is never a hidden HTML value or request value.
    form.locator('[data-field="api_key"]').evaluate("el=>{el.value='private-unsent-draft'}")
    mode.select_option('bind')
    profile = form.get_by_label('Existing native profile', exact=True)
    consent = form.get_by_role('checkbox', name='I have stopped native DeepOrca for this profile and will keep it stopped until the Connector stops', exact=True)
    assert profile.input_value() == ''
    assert not consent.is_checked()
    assert profile.evaluate('el=>el.required') and consent.evaluate('el=>el.required')
    for name in ['base_url', 'model', 'auth_mode', 'api_key', 'context_window', 'reasoning_effort']:
        assert form.locator(f'[data-field="{name}"]').is_disabled()
        assert not form.locator(f'[data-field="{name}"]').is_visible()
    for absent in ['permission_mode', 'working_dir', 'cwd', 'cli_args', 'model_flags']:
        assert form.locator(f'[data-field="{absent}"]').count() == 0
    assert form.locator('input[type=hidden]').count() == 0
    assert 'private-unsent-draft' not in form.evaluate('el=>el.outerHTML')
    assert 'model, security, persona, memory, skills and MCP' in form.inner_text()
    assert 'does not edit or copy' in form.inner_text()
    assert 'Old native chats are not imported as DeepBox chats' in form.inner_text()
    assert 'not tool approval' in form.inner_text()
    form.get_by_role('button', name='Add agent', exact=True).click()
    assert profile.evaluate('el=>el.validity.valueMissing')
    profile.select_option(profile_ref)
    form.get_by_role('button', name='Add agent', exact=True).click()
    assert consent.evaluate('el=>el.validity.valueMissing')
    assert not [r for r in h['requests'] if r[0] == 'POST']
    consent.check()
    mode.select_option('create')
    assert form.get_by_label('Model', exact=True).input_value() == 'draft/local-model'
    assert form.locator('[data-field="api_key"]').input_value() == 'private-unsent-draft'
    mode.select_option('bind')
    assert profile.input_value() == profile_ref and consent.is_checked()
    form.locator('[data-field="runtime"]').select_option('cli')
    assert not mode.is_visible() and not consent.is_visible()
    form.locator('[data-field="runtime"]').select_option('deeporca')
    assert profile.input_value() == profile_ref and consent.is_checked()
    assert form.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    suffix = 'mobile' if viewport['width'] < 600 else 'desktop'
    consent.scroll_into_view_if_needed()
    screenshot(page, f'deeporca-bind-existing-{suffix}.png')
    # A rejected request preserves the selected ref, consent, and unsubmitted model draft.
    h['create_error'] = 'Native profile is busy. Stop native DeepOrca and retry.'
    form.get_by_role('button', name='Add agent', exact=True).click()
    page.wait_for_function("document.querySelector('[data-error]')?.textContent.includes('Native profile is busy')")
    assert profile.input_value() == profile_ref and consent.is_checked()
    assert form.locator('[data-field="api_key"]').input_value() == 'private-unsent-draft'
    h.pop('create_error')
    form.get_by_role('button', name='Add agent', exact=True).click()
    settings = page.get_by_role('dialog', name='Agent settings', exact=True)
    settings.wait_for()
    posts = [r for r in h['requests'] if r[0] == 'POST' and r[1].endswith('/agents')]
    expected = {'handle': 'Native research helper', 'display_name': 'Native research helper', 'runtime': 'deeporca',
        'local_project_id': PROJECT['id'], 'runtime_config': {'integration_version': 1,
        'profile': {'mode': 'bind', 'profile_ref': profile_ref, 'native_stopped': True}}}
    assert len(posts) == 2 and all(r[2] == expected for r in posts)
    assert 'private-unsent-draft' not in json.dumps(h['requests'])
    assert 'Research native profile' in settings.inner_text() and profile_ref in settings.inner_text()
    assert 'Only the Agent name can be changed here' in settings.inner_text()
    assert settings.locator('[data-field]').count() == 1
    for name in ['base_url', 'model', 'auth_mode', 'api_key', 'context_window', 'reasoning_effort', 'profile_ref', 'profile_mode']:
        assert settings.locator(f'[data-field="{name}"]').count() == 0
    assert settings.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
    settings.get_by_label('Agent name', exact=True).fill('Renamed bound helper')
    settings.get_by_role('button', name='Save settings', exact=True).click()
    settings.get_by_role('button', name='Saved', exact=True).wait_for()
    assert [r[2] for r in h['requests'] if r[0] == 'PATCH'] == [{'display_name': 'Renamed bound helper'}]
    machine['agents'][-1]['runtime_status'] = {'state': 'error', 'code': 'configuration_busy', 'message': 'C:/private/native/home'}
    settings.get_by_role('button', name='Refresh status').click()
    page.wait_for_function("document.querySelector('[data-runtime-status]')?.textContent.includes('profile is busy')")
    assert 'C:/private/native/home' not in settings.inner_text()
    screenshot(page, f'deeporca-bound-settings-{suffix}.png')
    settings.get_by_role('button', name='Retry initialization', exact=True).click()
    page.wait_for_function("document.querySelector('[data-runtime-status]')?.textContent.includes('Pending')")
    assert [r[2] for r in h['requests'] if r[1].endswith('/runtime/retry')] == [None]
    assert not h['external']


def test_real_workbench_encrypted_model_settings_and_mobile(browser_workbench):
    """Real browser WebCrypto -> independent Python RSA-OAEP/AES-GCM decryption."""
    pytest.importorskip('cryptography')
    import base64
    import hashlib
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    h = browser_workbench
    page, machine = h['page'], h['machine']
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key()
    key_id = hashlib.sha256(public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()

    def number(value):
        return base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, 'big')).rstrip(b'=').decode()

    machine['capabilities']['runtimes'][0]['agent_config']['credential_key'] = {
        'version': 1, 'algorithm': 'RSA-OAEP-256+A256GCM', 'key_id': key_id,
        'public_key': {'kty': 'RSA', 'n': number(public.public_numbers().n), 'e': number(public.public_numbers().e)}}
    console = []
    page.on('console', lambda message: console.append(message.text))
    page.locator('[data-machine-menu="m"]').click()
    page.get_by_role('menuitem', name='Add agent', exact=True).click()
    form = page.get_by_role('dialog', name='Add agent', exact=True)
    form.get_by_label('Handle', exact=True).fill('Research assistant')
    form.get_by_label('Local project', exact=True).select_option(PROJECT['id'])
    assert form.get_by_label('Endpoint', exact=True).input_value() == ''
    assert form.get_by_label('Model', exact=True).input_value() == ''
    assert form.get_by_label('Context window · tokens').input_value() == ''
    assert form.get_by_label('Reasoning effort').input_value() == ''
    assert form.locator('[data-field="template"]').count() == 0
    form.get_by_role('button', name='Add agent', exact=True).click()
    assert 'Endpoint is required' in form.locator('[data-error]').inner_text()
    form.get_by_label('Endpoint', exact=True).fill('https://gateway.example.test/v1')
    form.get_by_label('Model', exact=True).fill('provider/research-model')
    form.get_by_label('Context window · tokens').fill('1.5')
    form.get_by_role('button', name='Add agent', exact=True).click()
    assert 'whole token count' in form.locator('[data-error]').inner_text()
    form.get_by_label('Context window · tokens').fill('131072')
    form.get_by_label('Reasoning effort').select_option('max')
    form.get_by_label('API key', exact=True).fill(' invalid-fixture-key ')
    form.get_by_role('button', name='Add agent', exact=True).click()
    assert 'whitespace' in form.locator('[data-error]').inner_text()
    assert not any(r[0] == 'POST' and r[1].endswith('/agents') for r in h['requests'])
    secret = 'browser-fixture-only-key'
    form.get_by_label('API key', exact=True).fill(secret)
    assert form.get_by_label('API key', exact=True).get_attribute('autocomplete') == 'new-password'
    form.get_by_label('Runtime adapter', exact=True).select_option('cli')
    assert not form.get_by_role('group', name='Model connection').is_visible()
    form.get_by_label('Runtime adapter', exact=True).select_option('deeporca')
    assert form.get_by_label('API key', exact=True).input_value() == secret
    page.set_viewport_size({'width': 1440, 'height': 1280})
    form.evaluate('el=>el.scrollTop=0')
    screenshot(page, 'deeporca-add-agent-model-connection.png')
    form.get_by_role('button', name='Add agent', exact=True).click()
    settings = page.get_by_role('dialog', name='Agent settings', exact=True)
    settings.wait_for()
    post = next(r[2] for r in h['requests'] if r[0] == 'POST' and r[1].endswith('/agents'))
    assert post['runtime_config']['llm'] == {
        'provider': 'openai', 'base_url': 'https://gateway.example.test/v1', 'model': 'provider/research-model',
        'context_window': 131072, 'reasoning_effort': 'max'}
    assert set(post['runtime_config']) == {'integration_version', 'profile', 'llm', 'credential'}

    def unseal(config):
        envelope = config['credential']
        assert set(envelope) == {'mode', 'key_id', 'wrapped_key', 'iv', 'ciphertext'}
        assert envelope['mode'] == 'sealed' and envelope['key_id'] == key_id
        raw = private.decrypt(base64.b64decode(envelope['wrapped_key'], validate=True),
            padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        iv = base64.b64decode(envelope['iv'], validate=True)
        assert len(raw) == 32 and len(iv) == 12
        aad = ('agentbridge/deeporca/credential/v1\0' + key_id + '\0' + config['llm']['base_url']).encode()
        return AESGCM(raw).decrypt(iv, base64.b64decode(envelope['ciphertext'], validate=True), aad).decode()

    assert unseal(post['runtime_config']) == secret
    assert settings.get_by_label('New API key', exact=True).input_value() == ''
    assert settings.get_by_label('Authentication', exact=True).input_value() == 'keep'
    assert settings.get_by_label('Endpoint', exact=True).input_value() == post['runtime_config']['llm']['base_url']
    h['patch_error'] = 'End active conversations before changing model settings.'
    settings.get_by_label('Model', exact=True).fill('provider/research-model-v2')
    settings.get_by_role('button', name='Save settings', exact=True).click()
    settings.get_by_text(h['patch_error'], exact=True).wait_for()
    assert settings.get_by_label('Model', exact=True).input_value() == 'provider/research-model-v2'
    patches = [r[2] for r in h['requests'] if r[0] == 'PATCH']
    assert len(patches) == 1 and 'credential' not in patches[0]['runtime_config']
    settings.get_by_label('Endpoint', exact=True).fill('https://replacement.example.test/v1')
    settings.get_by_role('button', name='Save settings', exact=True).click()
    assert 'Endpoint changed' in settings.locator('[data-error]').inner_text()
    assert len([r for r in h['requests'] if r[0] == 'PATCH']) == 1
    settings.get_by_label('Authentication', exact=True).select_option('api_key')
    replacement = 'replacement-fixture-only-key'
    settings.get_by_label('New API key', exact=True).fill(replacement)
    settings.get_by_role('button', name='Save settings', exact=True).click()
    settings.get_by_text(h['patch_error'], exact=True).wait_for()
    assert settings.get_by_label('New API key', exact=True).input_value() == replacement
    settings.get_by_role('button', name='Refresh status', exact=True).click()
    page.wait_for_function("!document.querySelector('[data-refresh-status]').disabled")
    assert settings.get_by_label('New API key', exact=True).input_value() == replacement
    settings.get_by_role('button', name='Retry initialization', exact=True).click()
    page.wait_for_function("document.querySelector('[data-runtime-status]').textContent.includes('Pending')")
    assert settings.get_by_label('Model', exact=True).input_value() == 'provider/research-model-v2'
    assert settings.get_by_label('New API key', exact=True).input_value() == replacement
    h.pop('patch_error')
    settings.get_by_role('button', name='Save settings', exact=True).click()
    settings.get_by_role('button', name='Saved', exact=True).wait_for()
    patch = [r[2] for r in h['requests'] if r[0] == 'PATCH'][-1]
    assert set(patch) == {'display_name', 'runtime_config'}
    assert unseal(patch['runtime_config']) == replacement
    assert settings.get_by_label('New API key', exact=True).input_value() == ''
    assert settings.get_by_label('Authentication', exact=True).input_value() == 'keep'
    settings.evaluate('el=>el.scrollTop=0')
    screenshot(page, 'deeporca-agent-settings-model.png')
    page.set_viewport_size({'width': 390, 'height': 844})
    assert settings.evaluate('el=>el.scrollWidth <= el.clientWidth + 1')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    settings.get_by_label('Context window · tokens').scroll_into_view_if_needed()
    assert settings.get_by_label('Context window · tokens').is_visible()
    settings.get_by_role('button', name='Saved', exact=True).scroll_into_view_if_needed()
    assert settings.get_by_role('button', name='Saved', exact=True).is_visible()
    screenshot(page, 'deeporca-agent-settings-mobile.png')
    requests = json.dumps(h['requests'], ensure_ascii=False)
    storage = page.evaluate('JSON.stringify(localStorage)')
    for value in (secret, replacement):
        assert value not in requests and value not in storage and value not in '\n'.join(console)
        assert value not in settings.inner_text()
    assert not h['external'] and not h['errors']


def test_real_workbench_renderer_panes_and_mobile(browser_workbench):
    page = browser_workbench["page"]
    page.locator('[data-open-agent="native"]').click()
    native = page.locator('.pane').first
    native.locator('.deeporca-chat').wait_for()
    page.wait_for_function("window.__sockets.some(s=>s.sessionId==='session-native')")
    assert native.locator('[data-chat-control="permission_mode"]').count() == 0
    assert native.get_by_role("button", name="Send message", exact=True).count() == 1
    events = [
        {"ev": "session.config", "renderer": "deeporca-chat-v1", "config": {"model": "fixture-model"}},
        {"ev": "user.echo", "text": "Browser-only fixture prompt", "client_input_id": "fixture-input"},
        {"ev": "thinking.delta", "text": "Reasoning <img src=x onerror=__xss++>"},
        {"ev": "message.delta", "message_id": "browser-message", "text":
         '# Native workbench\n**Bold** and *italic* and `inline`\n'
         '[Safe docs](https://example.invalid/docs) [unsafe](javascript:alert(1))\n'
         '<img src=x onerror="__xss++">\n- list item\n'
         '```js\nconst safe = "<script>";\n```\n' + 'long-token-' * 100},
        {"ev": "tool.call", "tool_id": "tool-1", "tool": "read_file", "input": {"path": "fixture.txt"}},
        {"ev": "tool.result", "tool_id": "tool-1", "content": "safe output <iframe>"},
        {"ev": "permission.ask", "id": "forbidden-native", "request_id": "forbidden-native", "tool": "shell"},
        {"ev": "turn.end", "turn_id": "fixture-turn", "status": "completed", "usage": {"total_tokens": 12}},
    ]
    for event in events:
        emit(page, "native", {"type": "output", "kind": "event", "data": event})
    view = native.locator('.deeporca-chat')
    assert view.locator('h1').inner_text() == "Native workbench"
    assert view.locator('strong').inner_text() == "Bold"
    assert view.locator('em').inner_text() == "italic"
    assert view.locator('li').inner_text() == "list item"
    assert view.locator('img,iframe,script').count() == 0
    assert page.evaluate("window.__xss") == 0
    link = view.get_by_role("link", name="Safe docs")
    assert link.get_attribute("href") == "https://example.invalid/docs"
    assert link.get_attribute("rel") == "noopener noreferrer"
    assert view.locator('a').count() == 1
    assert native.locator('.chat-perm,[data-permission]').count() == 0
    thinking = view.locator('details.do-thinking')
    thinking.locator('summary').click()
    page.wait_for_function("document.querySelector('details.do-thinking').open")
    tool = view.locator('details.do-tool')
    tool.locator('summary').click()
    page.wait_for_function("document.querySelector('details.do-tool').open")
    view.get_by_role("button", name="Copy code").click()
    assert page.evaluate("window.__copied") == ['const safe = "<script>";\n']
    before = view.inner_text().replace("Copied", "Copy")
    emit(page, "native", {"type": "restore", "kind": "event", "data": events})
    assert view.inner_text() == before
    assert thinking.evaluate("el=>el.open") and tool.evaluate("el=>el.open")
    # A second actual workbench pane uses the unchanged CLI renderer/approval transport.
    native.get_by_role("button", name="Split pane", exact=True).click()
    page.get_by_role("menuitem", name="Split right", exact=True).click()
    page.get_by_role("button", name="CLI fixture", exact=False).last.click()
    cli = page.locator('.pane').nth(1)
    cli.locator('.chat-log').wait_for(state="attached")
    assert cli.locator('[data-chat-control="permission_mode"]').count() == 1
    page.wait_for_function("window.__sockets.some(s=>s.sessionId==='session-cli')")
    emit(page, "cli", {"type": "output", "kind": "event", "data":
                       {"ev": "permission.ask", "request_id": "cli-approval", "tool": "shell", "input": {"command": "echo fixture"}}})
    cli.get_by_role("button", name="Allow", exact=True).click()
    frames = page.evaluate("window.__sockets.map(s=>({session:s.sessionId,frames:s.frames}))")
    assert any(f["type"] == "permission" and f["request_id"] == "cli-approval" for s in frames if s["session"] == "session-cli" for f in s["frames"])
    assert not any(f["type"] == "permission" for s in frames if s["session"] == "session-native" for f in s["frames"])
    native.locator('[data-ui="chat-input"]').fill("native draft")
    cli.locator('[data-ui="chat-input"]').fill("CLI draft")
    emit(page, "cli", {"type": "output", "kind": "event", "data": {"ev": "message.delta", "text": "CLI isolated output"}})
    assert "CLI isolated output" not in view.inner_text()
    assert native.locator('[data-ui="chat-input"]').input_value() == "native draft"
    assert cli.locator('[data-ui="chat-input"]').input_value() == "CLI draft"
    native.get_by_role("button", name="Send message", exact=True).click()
    inputs = page.evaluate("window.__sockets.filter(s=>s.sessionId==='session-native').flatMap(s=>s.frames).filter(f=>f.type==='input')")
    assert len(inputs) == 1
    assert "permission_mode" not in inputs[0].get("options", {})
    assert cli.locator('[data-ui="chat-input"]').input_value() == "CLI draft"
    screenshot(page, "deeporca-workbench-desktop.png")
    # Provider IDs can repeat across turns; a lost result is not still running
    # and the display preview must not claim to be the full native result.
    for event in [
        {"ev":"tool.call", "turn_id":"lost-turn", "tool_id":"reused/id+=", "tool":"write"},
        {"ev":"turn.end", "turn_id":"lost-turn", "status":"uncertain", "native":{"runtime":"deeporca"}},
        {"ev":"tool.call", "turn_id":"next-turn", "tool_id":"reused/id+=", "tool":"read"},
        {"ev":"tool.result", "turn_id":"next-turn", "tool_id":"reused/id+=", "content":"Preview only", "truncated":True, "original_bytes":250000},
    ]:
        emit(page, "native", {"type":"output", "kind":"event", "data":event})
    assert view.locator('details.do-tool').count() == 3
    assert "Outcome uncertain" in view.locator('details.do-tool').nth(1).inner_text()
    assert "stopping does not roll them back" in view.locator('.do-uncertain').text_content()
    assert "250000 bytes" in view.locator('.do-truncated').text_content()
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(150)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert view.evaluate("el=>el.scrollWidth <= el.clientWidth + 1")
    screenshot(page, "deeporca-workbench-mobile.png")


@pytest.mark.parametrize("random_mode", ["getRandomValues", "no_crypto"])
def test_uuid_fallback_and_rejected_native_input_keep_the_draft(browser_workbench, random_mode):
    from uuid import UUID

    h = browser_workbench
    page = h['page']
    page.locator('[data-open-agent="native"]').click()
    pane = page.locator('.pane').first
    pane.locator('.deeporca-chat').wait_for()
    page.evaluate("mode => { Object.defineProperty(window.crypto, 'randomUUID', {configurable:true,value:undefined}); if(mode==='no_crypto') Object.defineProperty(window.crypto, 'getRandomValues', {configurable:true,value:undefined}); }", random_mode)
    field = pane.locator('[data-ui="chat-input"]')
    submit = pane.locator('[data-ui="chat-send"]')
    field.fill('Keep this rejected draft')
    submit.click()
    sent = page.evaluate("window.__sockets.flatMap(s=>s.frames).filter(f=>f.type==='input')")
    assert len(sent) == 1
    assert pane.locator('.do-user').count() == 1
    first = sent[0]['client_input_id']
    assert str(UUID(first)) == first and UUID(first).version == 4
    # A keyboard shortcut cannot bypass the native single-pending-turn guard.
    field.fill('Newer unsent draft')
    field.press('Enter')
    assert page.evaluate("window.__sockets.flatMap(s=>s.frames).filter(f=>f.type==='input').length") == 1
    emit(page, 'native', {'type':'input_ack', 'client_input_id':'unrelated', 'status':'rejected', 'reason':'agent_busy'})
    assert submit.is_disabled()
    emit(page, 'native', {'type':'input_ack', 'client_input_id':first, 'status':'rejected', 'reason':'agent_busy'})
    assert field.input_value() == 'Newer unsent draft'
    assert not submit.is_disabled()
    assert pane.locator('.do-user').count() == 0
    assert 'agent busy' in pane.locator('[data-ui="chat-composer-error"]').inner_text()
    submit.click()
    sent = page.evaluate("window.__sockets.flatMap(s=>s.frames).filter(f=>f.type==='input')")
    second = sent[-1]['client_input_id']
    assert str(UUID(second)) == second and second != first
    emit(page, 'native', {'type':'input_ack', 'client_input_id':second, 'status':'rejected', 'reason':'execution_uncertain'})
    assert field.input_value() == 'Newer unsent draft'
    assert 'not resent' in pane.locator('[data-ui="chat-composer-error"]').inner_text()
    assert 'side effects' in pane.locator('[data-ui="chat-composer-error"]').inner_text()
    submit.click()
    page.evaluate("() => { const s=window.__sockets.findLast(s=>s.sessionId==='session-native'); s.close(); s.onclose?.({}); }")
    assert field.input_value() == 'Newer unsent draft'
    assert 'unconfirmed' in pane.locator('[data-ui="chat-composer-error"]').inner_text()
    assert page.evaluate("window.__sockets.flatMap(s=>s.frames).filter(f=>f.type==='input').length") == 3


def test_v2_pane_anchored_layout_markdown_and_compact_native_tools(browser_workbench):
    """Real workbench DOM regression for the 1774×1177 inset/list/tool report.

    Loopback fixtures exercise presentation only: these screenshots do not prove
    provider execution, authentication, native-tool success or backend behavior.
    """
    page = browser_workbench['page']
    page.set_viewport_size({'width':1774, 'height':1177})
    page.locator('[data-open-agent="native"]').click()
    pane = page.locator('.pane').first
    view = pane.locator('.deeporca-chat')
    view.wait_for()
    page.wait_for_function("window.__sockets.some(s=>s.sessionId==='session-native')")
    events = [
        {'ev':'session.config', 'renderer':'deeporca-chat-v1', 'options':{'model':'Local managed model'}},
        {'ev':'user.echo', 'text':'Review the integration and outline a safe rollout. Keep the changes scoped to the workbench.', 'client_input_id':'v2-demo'},
        {'ev':'turn.start', 'turn_id':'v2'},
        {'ev':'thinking.delta', 'turn_id':'v2', 'text':'I will inspect the runtime contract, compare the native presentation patterns, and separate UI evidence from execution evidence.'},
        {'ev':'tool.call', 'turn_id':'v2', 'tool_id':'read', 'tool':'read_file', 'input':{'path':'web/integrations/deeporca/runtime.js'}},
        {'ev':'tool.result', 'turn_id':'v2', 'tool_id':'read', 'content':'Runtime policy: managed profile, serial turns, explicit continuation.'},
        {'ev':'tool.call', 'turn_id':'v2', 'tool_id':'search', 'tool':'search', 'input':{'query':'renderer contract and Markdown lists'}},
        {'ev':'tool.result', 'turn_id':'v2', 'tool_id':'search', 'content':'3 local references found.'},
        {'ev':'tool.call', 'turn_id':'v2', 'tool_id':'usage', 'tool':'DeepOrca usage', 'native':{'runtime':'deeporca','kind':'status'}, 'input':{'total_tokens':1874}},
        {'ev':'tool.result', 'turn_id':'v2', 'tool_id':'usage', 'native':{'runtime':'deeporca','kind':'status'}, 'content':{'total_tokens':1874}},
        {'ev':'tool.call', 'turn_id':'v2', 'tool_id':'shell', 'tool':'run_command', 'input':{'command':'node --test web/*.test.js'}},
        {'ev':'tool.result', 'turn_id':'v2', 'tool_id':'shell', 'content':'Illustrative fixture output; no command was executed.'},
        {'ev':'message.delta', 'turn_id':'v2', 'message_id':'answer', 'text':
         '## A focused workbench integration\n\nThe renderer follows the native **conversation structure**, without embedding a second application.\n\n'
         '1. **Keep platform ownership clear.**\n'
         '   - Transport, replay and access control stay in the workbench.\n'
         '   - The runtime contract handles presentation-specific policy.\n'
         '     1. Preserve explicit continuation.\n'
         '     2. Keep drafts and input receipts pane-local.\n'
         '2. **Make activity easy to scan.**\n'
         '   - Compact tool summaries retain the exact action and target.\n'
         '   - Thinking and usage details stay available, but folded.\n'
         '3. **Validate what the UI actually shows.**\n\n'
         '| Area | Expected behavior | Evidence |\n| --- | --- | --- |\n'
         '| Layout | Anchored to each pane | Browser geometry |\n'
         '| Safety | No provider HTML execution | DOM assertions |\n'
         '| Execution | Not verified by this fixture | Separate backend tests |\n\n'
         '```js\nconst view = renderer.createView(pane, callbacks);\nview.renderState(snapshot, access);\n```\n\n'
         '> A missing result is not proof of success or rollback. Inspect tool effects before retrying.'},
        {'ev':'turn.end', 'turn_id':'v2', 'status':'completed', 'usage':{'total_tokens':1874}},
    ]
    for event in events:
        emit(page, 'native', {'type':'output','kind':'event','data':event})
    assert view.locator('.do-tool').count() == 3
    assert view.locator('.do-tool-group').count() == 1
    assert view.locator('.do-tool[open]').count() == 0
    assert view.locator('.do-thinking[open]').count() == 0
    assert 'DeepOrca usage' not in view.inner_text()
    assert view.locator('.do-runtime-meta').count() == 1
    assert view.locator('.do-markdown > ol > li').count() == 3
    assert view.locator('.do-markdown > ol > li > ul > li').count() == 4
    assert view.locator('.do-markdown ol ul ol > li').count() == 2
    assert view.locator('table tbody tr').count() == 3
    assert view.locator('.do-tool-detail').first.inner_text() == 'web/integrations/deeporca/runtime.js'
    assert view.locator('.do-tool > summary').first.bounding_box()['height'] <= 42

    def assert_geometry():
        # This deliberately measures against the local scrolling pane, not
        # the viewport, and runs again inside the real nested split flex tree.
        metrics = view.evaluate('''el => {
          const pane = el.closest('.chat-scroll');
          const a=el.getBoundingClientRect(), b=pane.getBoundingClientRect();
          return {inset:a.left-b.left,width:a.width,pane:b.width,overflow:el.scrollWidth-el.clientWidth};
        }''')
        assert 12 <= metrics['inset'] <= 32, metrics
        assert 140 <= metrics['width'] <= 881, metrics
        assert metrics['overflow'] <= 1, metrics
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        composer = pane.locator('.chat-form').bounding_box()
        column = view.bounding_box()
        assert abs(composer['x'] - column['x']) <= 1
        assert abs(composer['width'] - column['width']) <= 1

    pane.locator('.chat-scroll').evaluate('el=>el.scrollTop=0')
    assert_geometry()
    screenshot(page, 'deeporca-workbench-v2-desktop.png')
    page.set_viewport_size({'width':390,'height':844})
    page.wait_for_timeout(100)
    pane.locator('.chat-scroll').evaluate('el=>el.scrollTop=0')
    assert_geometry()
    # The platform sidebar remains user-toggleable on mobile. Verify the very
    # narrow open-sidebar pane as well as the normal full-width reading mode.
    page.get_by_role('button', name='Toggle sidebar', exact=True).click()
    assert_geometry()
    screenshot(page, 'deeporca-workbench-v2-mobile.png')
    view.locator('h2').evaluate('el=>el.scrollIntoView({block:"start"})')
    screenshot(page, 'deeporca-workbench-v2-mobile-markdown.png')

    page.set_viewport_size({'width':1774,'height':1177})
    page.get_by_role('button', name='Toggle sidebar', exact=True).click()
    pane.get_by_role('button', name='Split pane', exact=True).click()
    page.get_by_role('menuitem', name='Split right', exact=True).click()
    page.get_by_role('button', name='CLI fixture', exact=False).last.click()
    page.locator('.pane').nth(1).locator('.chat-log').wait_for(state='attached')
    assert_geometry()
    pane.locator('.chat-scroll').evaluate('el=>el.scrollTop=0')
    screenshot(page, 'deeporca-workbench-v2-split.png')


def test_v2_streaming_error_and_disclosure_state_are_accurate(browser_workbench):
    page = browser_workbench['page']
    page.locator('[data-open-agent="native"]').click()
    view = page.locator('.deeporca-chat')
    view.wait_for()
    page.wait_for_function("window.__sockets.some(s=>s.sessionId==='session-native')")
    def send(event):
        emit(page, 'native', {'type':'output', 'kind':'event', 'data':event})
    send({'ev':'turn.start', 'turn_id':'stream'})
    send({'ev':'thinking.delta','text':'A visible reason to inspect the failure.'})
    send({'ev':'tool.call','turn_id':'stream','tool_id':'missing','tool':'write_file','input':{'path':'example.txt'}})
    assert 'Running' in view.locator('.do-tool-state').inner_text()
    view.locator('.do-thinking > summary').click()
    view.locator('.do-tool > summary').click()
    send({'ev':'message.delta','message_id':'partial','text':'4. **Inspect** the result.\n   - Keep nested structure.\n\n```js\nconst x = '})
    assert view.locator('.do-markdown ol').get_attribute('start') == '4'
    assert view.locator('.do-markdown ol ul li').count() == 1
    send({'ev':'message.delta','message_id':'partial','text':'"safe";\n```'})
    assert view.locator('.do-code code').inner_text() == 'const x = "safe";\n'
    assert view.locator('.do-code').count() == 1
    assert view.locator('.do-thinking').evaluate('el=>el.open')
    assert view.locator('.do-tool').evaluate('el=>el.open')
    send({'ev':'turn.end','turn_id':'stream','status':'interrupted'})
    assert 'Interrupted' in view.locator('.do-tool-state').inner_text()
    assert 'Side effects may have occurred' in view.locator('.do-uncertain').inner_text()
    send({'ev':'tool.call','turn_id':'failure','tool_id':'failed','tool':'read_file','input':{'path':'missing.txt'}})
    send({'ev':'tool.result','turn_id':'failure','tool_id':'failed','is_error':True,'content':'File not found'})
    assert 'Failed' in view.locator('.do-tool-state').last.inner_text()
    send({'ev':'error','message':'Worker disconnected; execution outcome is uncertain.'})
    assert 'Worker disconnected' in view.locator('.do-error-banner').inner_text()


def test_native_canonical_usage_and_disconnected_tool_state(browser_workbench):
    page = browser_workbench['page']
    page.locator('[data-open-agent="native"]').click()
    view = page.locator('.deeporca-chat')
    view.wait_for()
    page.wait_for_function("window.__sockets.some(s=>s.sessionId==='session-native')")

    def send(event):
        emit(page, 'native', {'type':'output', 'kind':'event', 'data':event})

    # These are the actual Connector status/terminal shapes, not pseudo-tools.
    send({'ev':'status','subtype':'usage','turn_id':'done','native':{
        'runtime':'deeporca','type':'usage','usage':{'total_tokens':42}}})
    send({'ev':'turn.end','turn_id':'done','status':'completed','native':{
        'runtime':'deeporca','type':'done','usage':{'total_tokens':43}}})
    assert view.locator('.do-tool').count() == 0
    assert '42' in view.locator('.do-runtime-meta pre').text_content()
    assert '43' in view.locator('.do-usage pre').text_content()
    send({'ev':'turn.start','turn_id':'active'})
    send({'ev':'tool.call','turn_id':'active','tool_id':'pending',
          'tool':'run_command','input':{'command':'example'}})
    assert view.locator('.do-tool-state').inner_text() == 'Running'
    page.evaluate("() => { const s=window.__sockets.findLast(s=>s.sessionId==='session-native'); s.close(); s.onclose?.({}); }")
    # No subsequent model event should be needed to make the view honest.
    assert view.locator('.do-tool-state').inner_text() == 'Result not recorded'
    assert view.locator('.do-run-note').count() == 0
    assert view.locator('.do-access').inner_text() == 'Read-only transcript'


def test_continue_native_is_explicit_operator_action_not_layout_restore(browser_workbench):
    h = browser_workbench
    page = h['page']
    for ident, state, agent in [('saved-native', 'inactive', 'native'), ('ended-native', 'ended', 'native'), ('saved-cli', 'inactive', 'cli')]:
        h['sessions'][ident] = {'id':ident, 'agent_id':agent, 'surface':'structured', 'state':state,
                                'available':True, 'launch_id':'prior-'+ident}
    page.locator('[data-agent-menu="native"]').click()
    page.get_by_role('menuitem', name='Session history', exact=True).click()
    proceed = page.get_by_role('button', name='Continue native conversation', exact=True)
    proceed.wait_for()
    assert proceed.count() == 1  # Not offered for ended sessions.
    assert page.evaluate('window.__sockets.length') == 0
    proceed.click()
    page.locator('.deeporca-chat').wait_for()
    assert page.evaluate('window.__sockets[0].sessionId') == 'saved-native'
    intent = page.evaluate('window.__sockets[0].frames[0]')
    assert intent['type'] == 'resume' and intent['launch_id'] == 'prior-saved-native'
    page.wait_for_function("!document.querySelector('[data-ui=\"chat-input\"]').disabled")
    assert page.evaluate("window.__sockets[0].frames.filter(f=>['resume','attach'].includes(f.type)).map(f=>f.type)") == ['resume']
    assert not [r for r in h['requests'] if r[0] == 'POST' and r[1].endswith('/sessions')]
    # The runtime is inactive on restore; layout never persists one-shot consent.
    page.wait_for_function("JSON.stringify(localStorage).includes('saved-native')")
    page.reload()
    page.locator('.deeporca-chat').wait_for()
    assert page.evaluate('window.__sockets.length') == 0
    assert page.locator('[data-ui="replay-controls"]').count() == 1
    page.locator('[data-agent-menu="cli"]').click()
    page.get_by_role('menuitem', name='Session history', exact=True).click()
    page.get_by_role('button', name='View history', exact=True).wait_for()
    assert not page.get_by_role('button', name='Continue native conversation', exact=True).count()
    h['role'] = 'viewer'
    page.reload()
    page.locator('[data-agent-menu="native"]').click()
    page.get_by_role('menuitem', name='Session history', exact=True).click()
    page.get_by_role('button', name='View history', exact=True).first.wait_for()
    # History actions remain discoverable, but a viewer cannot execute them.
    continuation = page.get_by_role('button', name='Continue native conversation', exact=True)
    assert continuation.is_disabled()
    continuation.evaluate('button=>button.click()')
    assert page.evaluate('window.__sockets.length') == 0
    assert not [r for r in h['requests'] if r[0] == 'POST' and r[1].endswith('/sessions')]


def test_unknown_renderer_notice_and_unfinished_native_replay(browser_workbench):
    h = browser_workbench
    page = h['page']
    page.locator('[data-open-agent="native"]').click()
    pane = page.locator('.pane').first
    pane.locator('.deeporca-chat').wait_for()
    unknown = 'https://untrusted.invalid/renderer-v99.js'
    emit(page, 'native', {'type':'output', 'kind':'event', 'data':{'ev':'session.config', 'renderer':unknown}})
    emit(page, 'native', {'type':'output', 'kind':'event', 'data':{'ev':'message.delta', 'text':'Generic fallback remains readable'}})
    assert pane.locator('[data-ui="chat-renderer-notice"]').is_visible()
    assert 'Unsupported conversation renderer' in pane.locator('[data-ui="chat-renderer-notice"]').inner_text()
    assert 'Generic fallback remains readable' in pane.locator('.chat-log').inner_text()
    assert not page.locator('script[src*="untrusted.invalid"]').count()
    assert not pane.get_by_role('button', name='Allow', exact=True).count()
    # A recording without a final result is not evidence of a still-live tool.
    h['sessions']['session-native']['state'] = 'inactive'
    h['replay_events'] = [
        {'kind':'event', 'time':0, 'data':json.dumps({'ev':'session.config', 'renderer':'deeporca-chat-v1'}) + '\n'},
        {'kind':'event', 'time':1, 'data':json.dumps({'ev':'tool.call', 'tool':'write', 'tool_id':'lost-result', 'turn_id':'old-turn'}) + '\n'},
    ]
    sockets = page.evaluate('window.__sockets.length')
    page.locator('[data-agent-menu="native"]').click()
    page.get_by_role('menuitem', name='Session history', exact=True).click()
    page.get_by_role('button', name='View history', exact=True).click()
    card = page.locator('details.do-tool')
    card.wait_for()
    assert 'Result not recorded' in card.locator('summary').inner_text()
    assert 'Running' not in card.locator('summary').inner_text()
    assert 'Side effects may have occurred' in card.locator('.do-uncertain').text_content()
    assert not page.locator('[data-ui="chat-renderer-notice"]').is_visible()
    assert page.evaluate('window.__sockets.length') == sockets
