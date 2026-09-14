/*
 * DeepBox browser control plane.
 *
 * Structured agent sessions are the primary interface. The terminal remains a
 * fallback for runtimes that do not expose structured events.
 */
const app = document.getElementById('app');
let me = null, devboxes = [], term = null, fit = null, termWS = null, curSession = null;
let workspaces = [], activeWorkspaceId = null;
let authConfig = {mode:'local', password_enabled:true, microsoft_enabled:false};
let workspaceInviteOpen = false;
let fleetLoadRequest = 0;
let viewEpoch = 0;       // invalidates requests and callbacks from a previous page
let replayMode = false;
let curAgentId = null;   // currently open agent, for active-row highlighting
let currentSurface = null;
let fleetQuery = '';     // fleet search text
const ui = window.DeepboxUI;
const Chat = window.DeepboxChat;
const {nearestCheckpointIndex, eventsBetween, normalizeReplay, formatClock} = window.DeepboxReplay || {};
const {deriveCollaborationState, canSendInput, canSendMessage, collabHeaderView} = window.DeepboxCollaboration || {};
let collabState = null;  // normalized collaboration view model for curSession
let keyboardRequester = null;
let termInputSender = null;
let chatState = null;          // current structured session view model
let structuredMode = false;    // is the open session a structured (chat) agent?
let chatControls = [];         // normalized adapter-owned control descriptors
let chatControlValues = {};    // select values, keyed by generic control key
let chatAttachments = {};      // in-memory file payloads; never persisted by browser

function resetStructuredChat(){
  structuredMode = false;
  chatState = null;
  chatControls = [];
  chatControlValues = {};
  chatAttachments = {};
}

const hashParams = new URLSearchParams(location.hash.replace(/^#/, ''));
const queryParams = new URLSearchParams(location.search);
let pendingInvite = hashParams.get('invite') || queryParams.get('invite') || '';
let pendingWorkspaceInvite = hashParams.get('workspace-invite') || '';
try {
  if(pendingWorkspaceInvite) sessionStorage.setItem('deepbox.workspaceInvite', pendingWorkspaceInvite);
  else pendingWorkspaceInvite = sessionStorage.getItem('deepbox.workspaceInvite') || '';
} catch {}
if (pendingInvite || hashParams.get('workspace-invite')) history.replaceState(null, '', location.pathname);

function clearPendingWorkspaceInvite(){
  pendingWorkspaceInvite = '';
  try { sessionStorage.removeItem('deepbox.workspaceInvite'); } catch {}
}

async function api(path, opts={}) {
  const r = await fetch(path, {credentials:'same-origin',
    headers:{'Content-Type':'application/json'}, ...opts});
  if (!r.ok) {
    const body = await r.text();
    const message = ui
      ? ui.apiErrorMessage(r.status, r.statusText, body)
      : body.trim() || `Request failed (${[r.status, r.statusText].filter(Boolean).join(' ')})`;
    throw new Error(message);
  }
  return r.status === 204 ? null : r.json();
}

// esc(): always available HTML escaper. Delegates to ui.js once loaded, but has
// a self-contained fallback so early render paths never throw.
function esc(s){
  if(ui) return ui.escapeHtml(s);
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
const escapeHtml = esc;

// ---------------- auth ----------------
async function renderLogin() {
  closeOverlay();
  const view = clearAgentView();
  // First-owner bootstrap form when available.
  let status = {available:false};
  try { status = await api('/api/auth/bootstrap-status'); } catch {}
  if(view !== viewEpoch) return;
  if (status.available) return renderBootstrap();
  try { authConfig = await api('/api/auth/config'); } catch {}
  if(view !== viewEpoch) return;

  const inviteFromUrl = pendingInvite;
  const passwordEnabled = authConfig.password_enabled !== false;
  const microsoftEnabled = authConfig.microsoft_enabled === true;
  const psCmd = ui.windowsInstallCommand();
  const shCmd = ui.unixInstallCommand();
  const microsoftHtml = microsoftEnabled ? `<button type="button" id="microsoft-login" class="microsoft-login" aria-label="Continue with Microsoft">
      <span class="microsoft-mark" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
      Continue with Microsoft</button>` : '';
  const passwordHtml = passwordEnabled ? `${microsoftEnabled?'<div class="auth-divider">or use a local account</div>':''}
      <input id="u" placeholder="username" autocomplete="username"/>
      <input id="p" type="password" placeholder="password" autocomplete="current-password"/>
      <div class="row"><button id="login" style="flex:1">Sign in</button></div>
      <div class="auth-divider">or use an invite</div>
      <input id="inv" placeholder="invite code" value="${escapeHtml(inviteFromUrl)}"/>
      <input id="rd" placeholder="display name (optional)"/>
      <div class="row"><button class="ghost" id="reg" style="flex:1">Register with invite</button></div>` : '';
  app.innerHTML = `<div class="auth auth-split">
    <section class="auth-hero">
      <div class="auth-brand"><span class="glyph">_</span><b>deepbox</b></div>
      <h1 class="hero-title">Your agent switchboard.</h1>
      <p class="hero-lede">deepbox connects the AI coding agents already running on your
        own machines &mdash; Claude Code, GitHub Copilot CLI, Codex &mdash; to one browser
        control plane. Drive them, watch structured output live, and hand off between
        machines. The server never runs a model or holds a key: your agents stay on your
        hardware, we just route the wires.</p>
      <ul class="hero-points">
        <li><b>Bring your own agents.</b> Terminal (PTY) or structured chat mode, per agent.</li>
        <li><b>Nothing to trust us with.</b> No model keys, no code leaves your box unasked.</li>
        <li><b>Install once, connect anytime.</b> No git clone, no dependency wrangling.</li>
      </ul>
      <div class="hero-install">
        <div class="hero-install-h">Install the local command once</div>
        <div class="install-block">
          <div class="install-os">Windows &middot; PowerShell</div>
          <div class="install-cmd"><code id="cmd-ps">${escapeHtml(psCmd)}</code><button class="ghost compact" id="copy-ps">Copy installer</button></div>
        </div>
        <div class="install-block">
          <div class="install-os">macOS / Linux</div>
          <div class="install-cmd"><code id="cmd-sh">${escapeHtml(shCmd)}</code><button class="ghost compact" id="copy-sh">Copy installer</button></div>
        </div>
        <p class="install-note">The installer creates an isolated environment and a
          <code>deepbox</code> command under <code>~/.deepbox</code>. Sign in, mint a
          devbox token, then run the generated <code>deepbox connect</code> command.
          Reconnecting does not reinstall or refresh the app directory.</p>
      </div>
    </section>
    <div class="auth-card stack">
      <div class="auth-sub">${pendingWorkspaceInvite?'Sign in with the invited Microsoft account.':'Sign in to reach your workspaces.'}</div>
      ${microsoftHtml}${passwordHtml}
      <div id="err" class="auth-err"></div>
    </div></div>`;
  const copyBtn = (btnId, value) => {
    const b = document.getElementById(btnId);
    if (!b) return;
    b.onclick = async () => {
      try { await navigator.clipboard.writeText(value); const o=b.textContent; b.textContent='Copied'; setTimeout(()=>{b.textContent=o;},1200); } catch {}
    };
  };
  copyBtn('copy-ps', psCmd);
  copyBtn('copy-sh', shCmd);
  const microsoftLogin = document.getElementById('microsoft-login');
  if(microsoftLogin) microsoftLogin.onclick = () => { location.assign(authConfig.microsoft_login_url || '/api/auth/microsoft/start'); };
  const loginButton = document.getElementById('login');
  const registerButton = document.getElementById('reg');
  const password = document.getElementById('p');
  if(loginButton) loginButton.onclick = async () => {
    try {
      me = await api('/api/auth/login', {method:'POST', body: JSON.stringify({
        username:document.getElementById('u').value, password:password.value})});
      boot();
    } catch(e){ document.getElementById('err').textContent = e.message; }
  };
  if(registerButton) registerButton.onclick = async () => {
    try {
      me = await api('/api/auth/register', {method:'POST', body: JSON.stringify({
        username:document.getElementById('u').value, password:password.value,
        display_name:document.getElementById('rd').value||undefined,
        invite_code:document.getElementById('inv').value||undefined})});
      pendingInvite = '';
      boot();
    } catch(e){ document.getElementById('err').textContent = e.message; }
  };
  if(password && loginButton) password.addEventListener('keydown', e=>{ if(e.key==='Enter') loginButton.click(); });
}

function renderBootstrap() {
  closeOverlay();
  const view = clearAgentView();
  app.innerHTML = `<div class="auth"><div class="auth-card stack">
    <div class="auth-brand"><span class="glyph">_</span><b>deepbox</b></div>
    <div class="auth-sub">First-owner setup. Create the owner account with the bootstrap token.</div>
    <input id="bt" type="password" placeholder="bootstrap token"/>
    <input id="bu" placeholder="username"/>
    <input id="bp" type="password" placeholder="password"/>
    <input id="bd" placeholder="display name (optional)"/>
    <div class="row"><button id="bgo" style="flex:1">Create owner</button></div>
    <div id="err" class="auth-err"></div></div></div>`;
  const button = document.getElementById('bgo');
  const token = document.getElementById('bt'), username = document.getElementById('bu');
  const password = document.getElementById('bp'), displayName = document.getElementById('bd');
  const error = document.getElementById('err');
  button.onclick = async () => {
    if(button.disabled || view !== viewEpoch) return;
    button.disabled = true;
    try {
      const user = await api('/api/auth/bootstrap', {method:'POST', body: JSON.stringify({
        token:token.value, username:username.value, password:password.value,
        display_name:displayName.value||undefined})});
      if(view === viewEpoch){ me = user; await boot(); }
    } catch(e){ if(view === viewEpoch) error.textContent = e.message || 'Setup failed.'; }
    finally { button.disabled = false; }
  };
}

// ---------------- main shell ----------------
async function boot() {
  if(!ui || !Chat || !normalizeReplay || !canSendMessage)
    throw new Error('Browser helpers could not load. Reload the page and check that /static/*.js files are accessible.');
  const view = viewEpoch;
  try { me = me || await api('/api/me/user'); }
  catch { if(view === viewEpoch) return renderLogin(); return; }
  if(view !== viewEpoch) return;
  if (await loadDevboxes() && view === viewEpoch) {
    renderShell();
    if(pendingWorkspaceInvite) setTimeout(presentWorkspaceInvitation, 0);
  }
}

async function loadDevboxes(){
  const request = ++fleetLoadRequest;
  const view = viewEpoch;
  try {
    const [nextWorkspaces, nextDevboxes] = await Promise.all([
      api('/api/workspaces'), api('/api/devboxes')]);
    if(request !== fleetLoadRequest || view !== viewEpoch) return false;
    workspaces = nextWorkspaces;
    devboxes = nextDevboxes;
    let preferred = activeWorkspaceId;
    try { preferred = preferred || localStorage.getItem('deepbox.workspace'); } catch {}
    const selected = ui.selectWorkspace(workspaces, preferred);
    activeWorkspaceId = selected ? selected.id : null;
    return true;
  } catch(e) {
    if(request === fleetLoadRequest && view === viewEpoch) showAlert('Could not load workspaces', e.message);
    return false;
  }
}

function renderShell() {
  closeOverlay();
  clearAgentView();
  app.innerHTML = `
  <header class="topbar">
    <div class="brand"><span class="glyph">_</span><b>deepbox</b><span class="tag">switchboard</span></div>
    <span class="spacer"></span>
    <button class="cmd-hint" id="cmdk" title="Command palette">
      <span class="label">Search</span><span class="kbd">${cmdKeyLabel()} K</span></button>
    ${me.role==='owner'?'<button class="ghost" id="owner">Owner</button>':''}
    <div class="who"><span class="avatar">${esc(ui.initials(me.display_name||me.username))}</span>
      <span>${esc(me.display_name)}</span></div>
    <button class="ghost" id="logout">Sign out</button>
  </header>
  <main class="shell">
    <aside class="fleet" id="fleet"></aside>
    <section class="stage">
      <div class="stage-head" id="termhead"></div>
      <div class="stage-body" id="stagebody"></div>
    </section>
  </main>`;
  document.getElementById('logout').onclick = async()=>{
    if(me.auth_provider === 'microsoft') {
      location.assign(authConfig.microsoft_logout_url || '/api/auth/microsoft/logout');
      return;
    }
    await api('/api/auth/logout',{method:'POST'}); me=null; renderLogin();
  };
  document.getElementById('cmdk').onclick = openCommandPalette;
  if(me.role==='owner') document.getElementById('owner').onclick = renderOwner;
  renderFleet();
  renderStageEmpty();
}

// Cmd on macOS, Ctrl elsewhere — for the palette hint label only.
function cmdKeyLabel(){
  return /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent) ? '\u2318' : 'Ctrl';
}

// ---------------- fleet panel ----------------
function renderFleet() {
  const fleet = document.getElementById('fleet');
  if(!fleet) return;
  const workspace = ui.selectWorkspace(workspaces, activeWorkspaceId);
  if(!workspace){
    fleet.innerHTML = '<div class="fleet-empty"><h4>No workspace</h4><p class="muted">Create a workspace to organize your devboxes.</p><button id="workspace-create-empty">Create workspace</button></div>';
    const create = document.getElementById('workspace-create-empty'); if(create) create.onclick = createWorkspace;
    return;
  }
  activeWorkspaceId = workspace.id;
  const canAdmin = ui.canAdminWorkspace(workspace.role);
  const workspaceBoxes = ui.devboxesForWorkspace(devboxes, workspace.id);
  const summary = ui.fleetSummary(workspaceBoxes);
  const filtered = ui.filterDevboxes(workspaceBoxes, fleetQuery);

  const boxesHtml = filtered.map(d => {
    const boxStatus = ui.devboxStatus(d);
    const agents = (d.agents||[]).map(a => {
      const st = ui.agentStatus(a);
      const active = a.id === curAgentId ? ' is-active' : '';
      const capability = ui.findRuntimeCapability(d.capabilities, a.runtime);
      const hasTerminalFallback = ui.isCapabilityV2(capability) &&
        capability.surfaces.some(surface=>surface.id === 'terminal' && surface.available);
      return `<div class="agent-row${active}" data-open="${esc(a.id)}" data-name="${esc(a.display_name)}">
        <span class="agent-mono">${esc(ui.initials(a.handle||a.display_name))}</span>
        <div class="agent-main">
          <div class="agent-handle">@${esc(a.handle)}<span class="rt">${esc(ui.runtimeLabel(a.runtime))}</span></div>
          <div class="agent-meta">
            <span class="status is-${st.state}"><span class="status-dot"></span><span class="status-label">${esc(st.label)}</span></span>
          </div>
        </div>
        <div class="agent-actions">
          ${hasTerminalFallback ? `<button class="ghost" data-open-terminal="${esc(a.id)}" data-terminal-name="${esc(a.display_name)}">Terminal</button>` : ''}
          <button class="ghost" data-hist="${esc(a.id)}" data-histname="${esc(a.display_name)}">History</button>
          ${canAdmin?`<button class="danger" data-agent-del="${esc(a.id)}" data-agent-delname="${esc(a.display_name||a.handle)}">Delete</button>`:''}
        </div>
      </div>`;
    }).join('') || '<div class="box-empty">No agents on this devbox yet.</div>';
    return `<div class="box">
      <div class="box-head">
        <span class="status is-${boxStatus.state}"><span class="status-dot"></span></span>
        <span class="box-title">${esc(d.name)}</span>
        <span class="spacer"></span>
        <span class="muted">${esc(boxStatus.label)}</span>
      </div>
      <div class="agent-list">${agents}</div>
      <div class="box-foot">
        ${canAdmin?`<button class="ghost" data-agent="${esc(d.id)}">+ Agent</button>`:''}
        <button class="ghost" data-runtimes="${esc(d.id)}">Runtimes</button>
        <button class="ghost" data-skills="${esc(d.id)}">Skills <span class="button-count">${Array.isArray(d.skills) ? d.skills.length : 0}</span></button>
        <span class="spacer"></span>
        ${canAdmin?`<button class="ghost" data-token="${esc(d.id)}">Rotate token</button>
        <button class="danger" data-del="${esc(d.id)}">Delete</button>`:''}
      </div>
    </div>`;
  }).join('');

  const listHtml = filtered.length ? boxesHtml : (workspaceBoxes.length
    ? '<div class="fleet-empty"><div class="muted">No matches for your search.</div></div>'
    : `<div class="fleet-empty"><h4>No devboxes here</h4>
        <p class="muted">${canAdmin?'Create a devbox, then connect its token from a machine.':'A workspace admin has not added a devbox yet.'}</p>
        ${canAdmin?'<button id="fleet-create">Create devbox</button>':''}</div>`);
  const workspaceRows = workspaces.map(item=>`<button class="workspace-row${item.id===workspace.id?' is-active':''}" data-workspace="${esc(item.id)}">
      <span class="workspace-name">${esc(item.name)}</span>
      <span class="workspace-role">${esc(item.role)}</span>
    </button>`).join('');

  fleet.innerHTML = `
    <div class="workspace-nav">
      <div class="workspace-nav-head"><span>Workspaces</span><button class="ghost compact" id="new-workspace" title="Create workspace">+</button></div>
      <div class="workspace-list">${workspaceRows}</div>
      <button class="workspace-manage ghost" id="workspace-manage">Members &amp; invitations</button>
    </div>
    <div class="fleet-head">
      <div class="fleet-title"><h3>Devboxes</h3>
        ${canAdmin?'<button class="ghost" id="newbox" style="padding:4px 9px;font-size:12px">+ Devbox</button>':''}</div>
      <div class="fleet-summary">
        <span class="metric"><span class="status-dot" style="background:var(--ok)"></span>
          <b>${summary.devboxOnline}</b>/${summary.devboxTotal} online</span>
        <span class="metric"><b>${summary.agentOnline}</b>/${summary.agentTotal} agents</span>
      </div>
      <div class="fleet-search">
        <span class="icon">⌕</span>
        <input id="fleet-q" placeholder="Search this workspace" value="${esc(fleetQuery)}"/>
      </div>
    </div>
    <div class="fleet-list">${listHtml}</div>`;

  fleet.querySelectorAll('[data-workspace]').forEach(button=>button.onclick=()=>selectWorkspace(button.dataset.workspace));
  const createWorkspaceButton = document.getElementById('new-workspace'); if(createWorkspaceButton) createWorkspaceButton.onclick = createWorkspace;
  const manageWorkspaceButton = document.getElementById('workspace-manage'); if(manageWorkspaceButton) manageWorkspaceButton.onclick = ()=>openWorkspaceManager(workspace.id);
  const q = document.getElementById('fleet-q');
  if(q){
    q.oninput = () => { fleetQuery = q.value; renderFleet();
      const nq = document.getElementById('fleet-q'); if(nq){ nq.focus();
        nq.setSelectionRange(nq.value.length, nq.value.length); } };
  }
  const nb = document.getElementById('newbox'); if(nb) nb.onclick = createDevbox;
  const fc = document.getElementById('fleet-create'); if(fc) fc.onclick = createDevbox;

  fleet.querySelectorAll('[data-agent]').forEach(b=>b.onclick=()=>createAgent(b.dataset.agent));
  fleet.querySelectorAll('[data-runtimes]').forEach(b=>b.onclick=()=>showRuntimeSetup(b.dataset.runtimes));
  fleet.querySelectorAll('[data-skills]').forEach(b=>b.onclick=()=>showSkills(b.dataset.skills));
  fleet.querySelectorAll('[data-open]').forEach(b=>b.onclick=(e)=>{
    if(e.target.closest('[data-hist],[data-agent-del],[data-open-terminal]')) return;
    openAgent(b.dataset.open, b.dataset.name);
  });
  fleet.querySelectorAll('[data-open-terminal]').forEach(b=>b.onclick=(e)=>{
    e.stopPropagation();
    openAgent(b.dataset.openTerminal, b.dataset.terminalName, 'terminal');
  });
  fleet.querySelectorAll('[data-hist]').forEach(b=>b.onclick=(e)=>{
    e.stopPropagation();
    openHistory(b.dataset.hist, b.dataset.histname);
  });
  fleet.querySelectorAll('[data-agent-del]').forEach(b=>b.onclick=(e)=>{
    e.stopPropagation();
    deleteAgent(b.dataset.agentDel, b.dataset.agentDelname);
  });
  fleet.querySelectorAll('[data-token]').forEach(b=>b.onclick=()=>rotateToken(b.dataset.token));
  fleet.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>delDevbox(b.dataset.del));
}

function selectWorkspace(workspaceId){
  if(!workspaces.some(workspace=>workspace.id === workspaceId)) return;
  activeWorkspaceId = workspaceId;
  try { localStorage.setItem('deepbox.workspace', workspaceId); } catch {}
  fleetQuery = '';
  clearAgentView();
  renderFleet();
}

async function createWorkspace(){
  const values = await showForm({
    title:'Create workspace',
    desc:'A workspace shares every devbox and agent with its members.',
    fields:[{name:'name', label:'Workspace name', placeholder:'Product engineering', required:true}],
    submit:'Create workspace'
  });
  if(!values) return;
  try {
    const created = await api('/api/workspaces', {method:'POST', body:JSON.stringify({name:values.name})});
    activeWorkspaceId = created.id;
    try { localStorage.setItem('deepbox.workspace', created.id); } catch {}
    if(await loadDevboxes()) { clearAgentView(); renderFleet(); }
  } catch(error) { showAlert('Could not create workspace', error.message); }
}

function workspaceRoleOptions(workspace){
  const options = [
    {value:'viewer', label:'Viewer — read and replay'},
    {value:'operator', label:'Operator — send messages and use terminals'}
  ];
  if(workspace.role === 'owner') options.push({value:'admin', label:'Admin — manage devboxes and invitations'});
  return options;
}

function workspaceRoleLabel(role){
  return {viewer:'Viewer (read-only)', operator:'Operator (send messages)',
    admin:'Admin (manage workspace and send messages)', owner:'Owner (manage workspace and send messages)'}[role] || role;
}

async function openWorkspaceManager(workspaceId){
  closeOverlay();
  const view = viewEpoch, request = overlayEpoch;
  const workspace = workspaces.find(item=>item.id === workspaceId);
  if(!workspace) return;
  const canAdmin = ui.canAdminWorkspace(workspace.role);
  try {
    const members = await api(`/api/workspaces/${encodeURIComponent(workspace.id)}/members`);
    const invitations = canAdmin
      ? await api(`/api/workspaces/${encodeURIComponent(workspace.id)}/invitations`)
      : [];
    if(view !== viewEpoch || request !== overlayEpoch) return;
    const memberRows = members.map(member=>{
      const editable = canAdmin && member.role !== 'owner' && member.user_id !== me.id &&
        (member.role !== 'admin' || workspace.role === 'owner');
      const roles = workspaceRoleOptions(workspace).map(option=>
        `<option value="${esc(option.value)}"${option.value === member.role ? ' selected' : ''}>${esc(workspaceRoleLabel(option.value))}</option>`).join('');
      return `<div class="workspace-member" data-member="${esc(member.user_id)}">
        <span class="avatar">${esc(ui.initials(member.display_name||member.username))}</span>
        <span class="workspace-member-name"><b>${esc(member.display_name||member.username)}</b><small>@${esc(member.username)}</small></span>
        <span class="workspace-member-role">${editable
          ? `<select aria-label="Role for ${esc(member.username)}" data-member-role="${esc(member.role)}">${roles}</select><button class="ghost compact" data-save-member disabled>Save</button>`
          : `<span title="${esc(workspaceRoleLabel(member.role))}">${esc(member.role)}</span>`}</span>
      </div>`;
    }).join('');
    const inviteRows = invitations.map(invitation=>{
      const state = invitation.accepted_at ? 'joined' : invitation.revoked_at ? 'revoked' : 'pending';
      return `<div class="workspace-invite-row" data-invitation-row="${esc(invitation.id)}">
        <span><b>${esc(invitation.email)}</b><small>${esc(workspaceRoleLabel(invitation.role))} · ${state}</small></span>
        ${state==='pending'?`<button class="ghost compact" data-revoke-invitation="${esc(invitation.id)}">Revoke</button>`:''}
      </div>`;
    }).join('') || '<p class="muted workspace-none">No invitations yet.</p>';
    const roleOptions = workspaceRoleOptions(workspace).map(option=>
      `<option value="${esc(option.value)}"${option.value === 'operator' ? ' selected' : ''}>${esc(workspaceRoleLabel(option.value))}</option>`).join('');
    const adminHtml = canAdmin ? `<section class="workspace-manager-section">
      <h4>Invite someone</h4>
      <form id="workspace-invite-form" class="workspace-invite-form">
        <input id="workspace-invite-email" type="email" placeholder="name@example.com" required/>
        <select id="workspace-invite-role">${roleOptions}</select>
        <button type="submit">Create invitation</button>
      </form>
      <p id="workspace-invite-error" class="modal-err" role="alert"></p>
      <div id="workspace-invite-result" class="workspace-invite-result" hidden>
        <label>Share this one-time link</label>
        <div><input id="workspace-invite-link" readonly/><button id="workspace-invite-copy" class="ghost">Copy</button></div>
      </div>
      <div class="workspace-invitations"><h4>Invitations</h4>${inviteRows}</div>
    </section>` : '';

    showModal({
      title:workspace.name,
      desc:`${workspaceRoleLabel(workspace.role)}. Operators can send chat messages; terminal typing also requires the shared keyboard. Viewers are read-only. Invitations never change an existing member’s role.`,
      bodyHtml:`<section class="workspace-manager-section"><h4>Members</h4>
          <div class="workspace-members">${memberRows}</div><p id="workspace-member-error" class="modal-err" role="alert"></p></section>${adminHtml}`,
      actions:[{label:'Close', primary:true, value:true}],
      onReady:overlay=>{
        overlay.querySelector('.modal').classList.add('workspace-manager');
        overlay.querySelectorAll('[data-member]').forEach(row=>{
          const role = row.querySelector('[data-member-role]'), save = row.querySelector('[data-save-member]');
          if(!role || !save) return;
          role.onchange = ()=>{ save.textContent = 'Save'; save.disabled = role.value === role.dataset.memberRole; };
          save.onclick = async()=>{
            if(save.disabled || overlay !== overlayEl || view !== viewEpoch) return;
            save.disabled = role.disabled = true;
            const error = overlay.querySelector('#workspace-member-error');
            error.textContent = '';
            try {
              const changed = await api(`/api/workspaces/${encodeURIComponent(workspace.id)}/members/${encodeURIComponent(row.dataset.member)}`, {
                method:'PATCH', body:JSON.stringify({role:role.value})});
              if(overlay !== overlayEl || view !== viewEpoch) return;
              role.value = role.dataset.memberRole = changed.role;
              save.textContent = 'Saved';
            } catch(problem) {
              if(overlay === overlayEl && view === viewEpoch) error.textContent = problem.message || 'Could not change role.';
            } finally {
              role.disabled = false;
              save.disabled = role.value === role.dataset.memberRole;
            }
          };
        });
        overlay.querySelectorAll('[data-revoke-invitation]').forEach(button=>button.onclick=async()=>{
          if(button.disabled || overlay !== overlayEl || view !== viewEpoch) return;
          button.disabled = true;
          try {
            await api(`/api/workspaces/${encodeURIComponent(workspace.id)}/invitations/${encodeURIComponent(button.dataset.revokeInvitation)}`, {method:'DELETE'});
            const row = button.closest('[data-invitation-row]');
            if(row){ row.querySelector('small').textContent = row.querySelector('small').textContent.replace('pending','revoked'); button.remove(); }
          } catch(error) { button.disabled=false; if(overlay === overlayEl && view === viewEpoch) overlay.querySelector('#workspace-invite-error').textContent = error.message; }
        });
        const form = overlay.querySelector('#workspace-invite-form');
        if(!form) return;
        form.onsubmit = async event=>{
          event.preventDefault();
          const submit = form.querySelector('button[type=submit]');
          if(submit.disabled || overlay !== overlayEl || view !== viewEpoch) return;
          submit.disabled = true;
          const error = overlay.querySelector('#workspace-invite-error');
          error.textContent = '';
          try {
            const invitation = await api(`/api/workspaces/${encodeURIComponent(workspace.id)}/invitations`, {
              method:'POST', body:JSON.stringify({
                email:overlay.querySelector('#workspace-invite-email').value,
                role:overlay.querySelector('#workspace-invite-role').value})});
            if(overlay !== overlayEl || view !== viewEpoch) return;
            const result = overlay.querySelector('#workspace-invite-result');
            const link = overlay.querySelector('#workspace-invite-link');
            link.value = invitation.join_url;
            result.hidden = false;
            overlay.querySelector('#workspace-invite-copy').onclick = async()=>{
              try { await navigator.clipboard.writeText(link.value); overlay.querySelector('#workspace-invite-copy').textContent='Copied'; }
              catch { link.select(); }
            };
          } catch(problem) { if(overlay === overlayEl && view === viewEpoch) error.textContent = problem.message || 'Could not create invitation. Your email and role have been kept.'; }
          finally { submit.disabled = false; }
        };
      }
    });
  } catch(error) { if(view === viewEpoch && request === overlayEpoch) showAlert('Could not load workspace', error.message); }
}

async function presentWorkspaceInvitation(){
  if(!pendingWorkspaceInvite || workspaceInviteOpen) return;
  workspaceInviteOpen = true;
  try {
    const token = pendingWorkspaceInvite;
    const preview = await api('/api/workspace-invitations/preview', {
      method: 'POST', body: JSON.stringify({ token }),
    });
    const copy = ui.workspaceInvitationCopy(preview);
    const accepted = await showModal({
      title:copy.title,
      desc:copy.description,
      bodyHtml:`<div class="workspace-join-note">Signed in as <b>${esc(me.email||me.username)}</b></div>`,
      actions:[
        {label:'Not now', value:false},
        {label:'Join workspace', primary:true, value:true}
      ]
    });
    if(!accepted) return;
    try {
      const result = await api('/api/workspace-invitations/accept', {
        method:'POST', body:JSON.stringify({token})});
      clearPendingWorkspaceInvite();
      activeWorkspaceId = result.workspace.id;
      try { localStorage.setItem('deepbox.workspace', activeWorkspaceId); } catch {}
      if(await loadDevboxes()) { clearAgentView(); renderFleet(); }
      const acceptance = ui.workspaceAcceptanceCopy(result);
      showAlert(acceptance.title, acceptance.description);
    } catch(error) {
      await showAlert('Could not join workspace', `${error.message} Check that you signed in with the invited Microsoft account.`);
    }
  } catch(error) {
    clearPendingWorkspaceInvite();
    await showAlert('Invitation unavailable', error.message);
  } finally {
    workspaceInviteOpen = false;
  }
}

// Empty state for the terminal stage (no agent open).
function renderStageEmpty(){
  const head = document.getElementById('termhead');
  const body = document.getElementById('stagebody');
  if(head) head.innerHTML = `<span class="title">Terminal</span>
    <span class="sep">\u2014</span><span class="muted">no session open</span>`;
  if(!body) return;
  body.innerHTML = `<div class="stage-empty"><div class="inner">
    <div class="art">_</div>
    <h3>Pick an agent to open its terminal</h3>
    <p>Sessions keep running on your devbox even after you close the browser.</p>
    <div class="tips">
      <div class="tip"><span class="kbd">${cmdKeyLabel()} K</span><span>Open the command palette to jump to any agent</span></div>
      <div class="tip"><span class="kbd">Click</span><span>Select an agent in the fleet to attach live</span></div>
      <div class="tip"><span class="kbd">History</span><span>Replay a recorded session from any agent</span></div>
    </div>
  </div></div>`;
}

// ---------------- owner admin ----------------
async function renderOwner(){
  closeOverlay();
  const view = clearAgentView();
  let invites=[], users=[];
  try { [invites, users] = await Promise.all([
    api('/api/invitations'), api('/api/users')]); } catch(e){}
  if(view !== viewEpoch) return;
  app.innerHTML = `
  <header class="topbar">
    <div class="brand"><span class="glyph">_</span><b>deepbox</b><span class="tag">owner</span></div>
    <span class="spacer"></span>
    <button class="ghost" id="back">\u2190 Back to switchboard</button>
  </header>
  <main class="owner-main">
    <div class="card">
      <h4>Invitations</h4>
      <div class="row">
        <input id="inote" placeholder="note (optional)"/>
        <input id="ittl" type="number" value="24" title="TTL hours" style="width:120px"/>
        <button id="mint" style="white-space:nowrap">Mint invite</button>
      </div>
      <div id="mintout"></div>
      <div id="invlist" style="margin-top:8px"></div>
    </div>
    <div class="card">
      <h4>Members</h4>
      <div id="userlist"></div>
    </div>
  </main>`;
  document.getElementById('back').onclick = renderShell;

  const renderInv = () => {
    document.getElementById('invlist').innerHTML = invites.map(i=>`
      <div class="list-row">
        <span>${i.note?esc(i.note):'<span class="muted">(no note)</span>'}</span>
        <span class="muted">${esc(i.status)} \u00b7 expires ${esc(i.expires_at)}</span>
        <span class="spacer"></span>
        ${i.status==='active'?`<button class="ghost" data-revoke="${esc(i.id)}">Revoke</button>`:''}
      </div>`).join('') || '<div class="muted">No invitations.</div>';
    document.querySelectorAll('[data-revoke]').forEach(b=>b.onclick=async()=>{
      await api(`/api/invitations/${b.dataset.revoke}`,{method:'DELETE'});
      invites = await api('/api/invitations'); renderInv();
    });
  };
  const renderUsers = () => {
    document.getElementById('userlist').innerHTML = users.map(u=>`
      <div class="list-row">
        <span class="avatar" style="width:26px;height:26px;border-radius:6px;background:var(--panel-2);border:1px solid var(--border);display:grid;place-items:center;font-size:11px">${esc(ui.initials(u.display_name||u.username))}</span>
        <b>${esc(u.display_name)}</b>
        <span class="muted">@${esc(u.username)} \u00b7 ${esc(u.role)}${u.disabled?' \u00b7 disabled':''}</span>
        <span class="spacer"></span>
        ${u.role==='member'?(u.disabled
          ?`<button class="ghost" data-enable="${esc(u.id)}">Enable</button>`
          :`<button class="ghost" data-disable="${esc(u.id)}">Disable</button>`):''}
      </div>`).join('') || '<div class="muted">No users.</div>';
    document.querySelectorAll('[data-disable]').forEach(b=>b.onclick=async()=>{
      try{ await api(`/api/users/${b.dataset.disable}/disable`,{method:'POST'}); }
      catch(e){ await showAlert('Cannot disable user', e.message); }
      users = await api('/api/users'); renderUsers();
    });
    document.querySelectorAll('[data-enable]').forEach(b=>b.onclick=async()=>{
      await api(`/api/users/${b.dataset.enable}/enable`,{method:'POST'});
      users = await api('/api/users'); renderUsers();
    });
  };
  renderInv(); renderUsers();

  document.getElementById('mint').onclick = async()=>{
    const res = await api('/api/invitations',{method:'POST',body:JSON.stringify({
      note:document.getElementById('inote').value||undefined, ttl_hours:Number(document.getElementById('ittl').value)||24})});
    if(view !== viewEpoch) return;
    // Show plaintext + prefilled invite URL exactly once; not retained.
    // URL fragments never reach the HTTP server or its access logs.
    const url = `${location.origin}${location.pathname}#invite=${encodeURIComponent(res.token)}`;
    document.getElementById('mintout').innerHTML =
      `<div class="token">Invite code (shown once): ${esc(res.token)}<br><br>`+
      `Invite URL: <a href="${esc(url)}">${esc(url)}</a></div>`;
    invites = await api('/api/invitations'); renderInv();
  };
}

// ---------------- devbox / agent mutations ----------------
async function createDevbox() {
  const name = await showForm({
    title:'Create devbox', desc:'Give this devbox a name. Run the connector on it afterwards.',
    fields:[{name:'name', label:'Name', value:'My Devbox', required:true}],
    submit:'Create'});
  if(!name) return;
  let res;
  try {
    res = await api('/api/devboxes',{method:'POST',body:JSON.stringify({
      name:name.name, workspace_id:activeWorkspaceId})});
  } catch(error) {
    await showAlert('Could not create devbox', error.message);
    return;
  }
  if (await loadDevboxes()) renderFleet();
  await showToken(res.token);
}
// Present a one-time devbox token. Rendered from memory into a modal only;
// never written to localStorage, cookies, the URL or the console.
async function showToken(tok){
  const windowsInstall = ui.windowsInstallCommand();
  const unixInstall = ui.unixInstallCommand();
  const command = ui.windowsConnectorCommand(location.origin, tok);
  const unixCommand = ui.unixConnectorCommand(location.origin, tok);
  const bindCopy = (overlay, selector, text)=>{
    const button = overlay.querySelector(selector);
    if(!button) return;
    button.onclick = async ()=>{
      const original = button.textContent;
      try {
        await copyText(text);
        button.textContent = 'Copied';
        setTimeout(()=>{ if(button.isConnected) button.textContent = original; }, 1400);
      } catch(e) {
        button.textContent = 'Copy failed';
        setTimeout(()=>{ if(button.isConnected) button.textContent = original; }, 1800);
      }
    };
  };
  await showModal({
    title:'Devbox token \u2014 shown once',
    desc:'Copy this token now. On a new machine, install the local command once, then connect. Future deepbox connect calls never reinstall or refresh the app directory.',
      bodyHtml:`<div class="token-head"><b>Token</b><button class="ghost compact" id="copy-token">Copy token</button></div>
      <div class="token">${esc(tok)}</div>
      <div class="token-head"><b>Windows \u00b7 one-time install</b><button class="ghost compact" id="copy-install">Copy installer</button></div>
      <div class="token token-command">${esc(windowsInstall)}</div>
      <div class="token-head"><b>Windows \u00b7 connect</b><button class="ghost compact" id="copy-command">Copy connect command</button></div>
      <div class="token token-command">${esc(command)}</div>
      <div class="token-head"><b>macOS / Linux \u00b7 one-time install</b><button class="ghost compact" id="copy-install-unix">Copy installer</button></div>
      <div class="token token-command">${esc(unixInstall)}</div>
      <div class="token-head"><b>macOS / Linux \u00b7 connect</b><button class="ghost compact" id="copy-command-unix">Copy connect command</button></div>
      <div class="token token-command">${esc(unixCommand)}</div>`,
    actions:[{label:'Done', primary:true, value:true}],
    onReady:(overlay)=>{
      bindCopy(overlay, '#copy-token', tok);
      bindCopy(overlay, '#copy-install', windowsInstall);
      bindCopy(overlay, '#copy-command', command);
      bindCopy(overlay, '#copy-install-unix', unixInstall);
      bindCopy(overlay, '#copy-command-unix', unixCommand);
    }});
}

async function copyText(text){
  if(navigator.clipboard && window.isSecureContext){
    await navigator.clipboard.writeText(String(text));
    return;
  }
  const area = document.createElement('textarea');
  area.value = String(text);
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.appendChild(area);
  area.select();
  const copied = document.execCommand('copy');
  area.remove();
  if(!copied) throw new Error('Clipboard unavailable');
}
async function rotateToken(id){
  const res = await api(`/api/devboxes/${id}/tokens`,{method:'POST'});
  await showToken(res.token);
}
async function delDevbox(id){
  const ok = await showConfirm('Delete devbox',
    'This removes the devbox and all of its agents. This cannot be undone.', 'Delete');
  if(!ok) return;
  await api(`/api/devboxes/${id}`,{method:'DELETE'});
  if (await loadDevboxes()) renderFleet();
}
function bindCommandCopies(root){
  root.querySelectorAll('[data-copy-command]').forEach(button=>{
    button.onclick=async()=>{
      const label=button.textContent;
      try{
        await copyText(button.dataset.copyCommand || '');
        button.textContent='Copied';
      }catch(_){
        button.textContent='Copy failed';
      }
      setTimeout(()=>{ if(button.isConnected) button.textContent=label; }, 1600);
    };
  });
}

async function showSkills(devboxId){
  await loadDevboxes();
  const devbox=devboxes.find(item=>item.id===devboxId);
  if(!devbox) return;
  const projects=new Map((devbox.projects || []).map(project=>[project.id, project.name]));
  const skills=Array.isArray(devbox.skills) ? devbox.skills : [];
  const rows=skills.length ? skills.map(skill=>{
    const scope=skill.scope === 'project'
      ? `Project \u00b7 ${projects.get(skill.project_id) || 'Unavailable project'}`
      : 'Personal';
    const targets=(skill.targets || []).join(', ') || 'No runtime target';
    const status=skill.status || 'missing';
    const inspectCommand=`deepbox skill inspect "${String(skill.name).replaceAll('"', '\\"')}"${skill.scope === 'project' && skill.project_id ? ` --project "${String(projects.get(skill.project_id) || skill.project_id).replaceAll('"', '\\"')}"` : ''}`;
    return `<div class="skill-row">
      <div class="skill-row-head"><div><b>${esc(skill.name)}</b><span class="skill-scope">${esc(scope)}</span></div><span class="skill-status skill-status-${esc(status)}">${esc(status)}</span></div>
      <p>${esc(skill.description)}</p>
      <div class="skill-meta"><span>${esc(targets)}</span>${skill.contains_scripts ? '<span>Contains scripts (not executed by Deepbox)</span>' : ''}</div>
      <button class="ghost compact" type="button" data-copy-command="${esc(inspectCommand)}">Copy inspect command</button>
    </div>`;
  }).join('') : '<div class="empty-inline">No skills reported by this connector yet.</div>';
  const installCommand='deepbox skill install "<skill-folder>"';
  const listCommand='deepbox skill list';
  await showModal({
    title:`Skills on ${devbox.name}`,
    desc:'Skills are installed by your local connector into runtime-discovered directories. Deepbox stores only path-free metadata on the server and never executes files from a skill package.',
    bodyHtml:`<div class="skill-list">${rows}</div>
      <div class="token-head"><b>Install a personal skill on this machine</b><button class="ghost compact" type="button" data-copy-command="${esc(installCommand)}">Copy command</button></div>
      <div class="token token-command">${esc(installCommand)}</div>
      <div class="token-head"><b>List local skills</b><button class="ghost compact" type="button" data-copy-command="${esc(listCommand)}">Copy command</button></div>
      <div class="token token-command">${esc(listCommand)}</div>`,
    actions:[{label:'Close', primary:true, value:true}],
    onReady:bindCommandCopies,
  });
}

function showRuntimeSetup(devboxId){
  const devbox = devboxes.find(item=>item.id === devboxId);
  if(!devbox) return;
  const inventory = ui.runtimeInventory(ui.runtimeCapabilities(devbox.capabilities));
  const bodyHtml = inventory.length ? inventory.map(item=>{
    const state = item.installation === 'installed'
      ? `${item.compatibility} · ${item.authentication}` : 'not installed';
    const command = item.guidance?.command
      ? `<code class="runtime-command">${esc(item.guidance.command)}</code>` : '';
    const guide = item.guidance?.url
      ? `<a href="${esc(item.guidance.url)}" target="_blank" rel="noopener noreferrer">Setup guide</a>` : '';
    return `<div class="runtime-setup-row">
      <div><b>${esc(item.label)}</b><span class="muted">${esc(state)}</span></div>
      ${command}<div>${guide}</div>
    </div>`;
  }).join('') : '<p class="muted">Connect this devbox to report runtime status.</p>';
  showModal({
    title:`Runtimes on ${devbox.name}`,
    desc:'Deepbox never installs third-party CLIs or reads their credentials. Run setup commands yourself on this devbox, authenticate in the CLI, then reconnect the connector.',
    bodyHtml,
    actions:[{label:'Close', className:'ghost', onClick:closeOverlay}],
  });
}

async function createAgent(devboxId){
  // Runtime probing and local-project reporting are asynchronous. Refresh at
  // the point of use instead of trusting the fleet snapshot loaded at boot.
  await loadDevboxes();
  let d=devboxes.find(x=>x.id===devboxId);
  if(!d) return;
  const runtimes = ui.runtimeOptions(ui.runtimeCapabilities(d.capabilities));
  if(!runtimes.length){
    await showAlert('No runtimes reported',
      'Start or reconnect this machine so the connector can report its available runtime adapters.');
    return;
  }
  const projectOptions = ()=>[{value:'', label:'No project (runtime default)'}]
    .concat(ui.localProjectOptions(d.projects).map(project=>({value:project.id, label:project.name})));
  const projectGuide = `<details class="local-action-guide">
    <summary>Add a local project</summary>
    <p>Run this on <b>${esc(d.name)}</b>. The folder path stays on that machine.</p>
    <div class="local-command-fields">
      <label>Folder path<input id="project-local-path" placeholder="C:\\path\\to\\project"></label>
      <label>Display name<input id="project-local-name" placeholder="My project"></label>
    </div>
    <div class="token-head"><b>Command</b><button class="ghost compact" type="button" id="copy-project-add">Copy command</button></div>
    <div class="token token-command" id="project-add-command"></div>
    <div class="local-action-footer"><small>After it succeeds, refresh this list. No restart is needed.</small><button class="ghost compact" type="button" id="refresh-projects">Refresh projects</button></div>
  </details>`;
  const res=await showForm({
    title:'Add agent', desc:`Register an agent runtime on ${d.name}.`,
    fields:[
      {name:'handle', label:'Handle', placeholder:'e.g. claude', required:true},
      {name:'runtime', label:'Runtime adapter', type:'select', options:runtimes, required:true},
      {name:'local_project_id', label:'Local project', type:'select', options:projectOptions(),
        helpHtml:'<small>Projects are connector-local. Add one below, then refresh.</small>'},
    ],
    extraHtml:projectGuide,
    onReady:(overlay)=>{
      const pathInput=overlay.querySelector('#project-local-path');
      const nameInput=overlay.querySelector('#project-local-name');
      const command=overlay.querySelector('#project-add-command');
      const copy=overlay.querySelector('#copy-project-add');
      const update=()=>{ command.textContent=ui.projectAddCommand(pathInput.value, nameInput.value); };
      pathInput.oninput=update;
      nameInput.oninput=update;
      update();
      copy.onclick=async()=>{
        const original=copy.textContent;
        try { await copyText(command.textContent); copy.textContent='Copied'; }
        catch(e) { copy.textContent='Copy failed'; }
        setTimeout(()=>{ if(copy.isConnected) copy.textContent=original; }, 1400);
      };
      overlay.querySelector('#refresh-projects').onclick=async event=>{
        const button=event.currentTarget;
        button.disabled=true;
        button.textContent='Refreshing...';
        try {
          await loadDevboxes();
          d=devboxes.find(x=>x.id===devboxId) || d;
          const select=overlay.querySelector('[data-field="local_project_id"]');
          const selected=select.value;
          const options=projectOptions();
          select.innerHTML=options.map(option=>`<option value="${esc(option.value)}">${esc(option.label)}</option>`).join('');
          if(options.some(option=>option.value===selected)) select.value=selected;
          button.textContent='Refreshed';
        } catch(e) {
          button.textContent='Refresh failed';
        } finally {
          setTimeout(()=>{ if(button.isConnected){ button.disabled=false; button.textContent='Refresh projects'; } }, 1200);
        }
      };
    },
    submit:'Add agent'});
  if(!res)return;
  try{
    await api(`/api/devboxes/${devboxId}/agents`,{method:'POST',
      body:JSON.stringify({handle:res.handle, display_name:res.handle,
        runtime:res.runtime, local_project_id:res.local_project_id||null,
        runtime_config:{}})});
    if (await loadDevboxes()) renderFleet();
  }catch(error){
    try{ if (await loadDevboxes()) renderFleet(); }catch(_refreshError){}
    await showAlert('Agent could not be added', error.message || 'The request failed.');
  }
}

async function deleteAgent(agentId, name){
  const ok = await showConfirm('Delete agent?',
    `Delete ${name || 'this agent'}? Saved sessions and any running session for this agent will be removed.`,
    'Delete agent');
  if(!ok) return;
  try{
    await api(ui.agentApiPath(agentId), {method:'DELETE'});
    if(curAgentId === agentId) clearAgentView();
    if (await loadDevboxes()) renderFleet();
  }catch(error){
    try{ if (await loadDevboxes()) renderFleet(); }catch(_refreshError){}
    await showAlert('Agent could not be deleted', error.message || 'The request failed.');
  }
}

// ---------------- terminal ----------------
let reconnectDelay = 500, reconnectTimer = null, wantOpen = false;

// xterm theme aligned with the dark UI tokens.
const XTERM_THEME = {
  background:'#000000', foreground:'#e6e9ee', cursor:'#3bb1a8',
  cursorAccent:'#000000', selectionBackground:'rgba(59,177,168,0.30)',
  black:'#0b0d10', red:'#e5674f', green:'#4bbf7a', yellow:'#d8a63a',
  blue:'#5aa9e6', magenta:'#b98ae0', cyan:'#3bb1a8', white:'#c9d1d9',
  brightBlack:'#6b7480', brightRed:'#f08a74', brightGreen:'#6fd598',
  brightYellow:'#e6bd5f', brightBlue:'#7fbef0', brightMagenta:'#cda6ec',
  brightCyan:'#5fc7bd', brightWhite:'#f2f5f8',
};

function termHost(){
  let host = document.getElementById('term');
  if(!host){
    const body = document.getElementById('stagebody');
    if(!body) return null;
    host = document.createElement('div');
    host.id = 'term';
    body.insertBefore(host, document.getElementById('replaybar'));
  }
  return host;
}

function focusTerminal(force){
  if(!term || replayMode || structuredMode) return;
  const target = term, view = viewEpoch;
  const active = document.activeElement;
  const terminalOwnsFocus = !!(active && active.classList
    && active.classList.contains('xterm-helper-textarea'));
  const mayFocus = ui && ui.shouldFocusTerminal
    ? ui.shouldFocusTerminal(force, active && active.tagName, terminalOwnsFocus)
    : force || terminalOwnsFocus;
  if(!mayFocus) return;
  requestAnimationFrame(()=>{
    if(term !== target || view !== viewEpoch || replayMode || structuredMode) return;
    target.focus();
    target.scrollToBottom();
  });
}

function setupTerm(){
  const host = termHost();
  if(!host) return false;
  try {
    if(typeof window.Terminal !== 'function' || typeof window.FitAddon?.FitAddon !== 'function')
      throw new Error('The xterm terminal renderer could not load. Reload the page and allow the xterm scripts from cdn.jsdelivr.net, or use Chat if available.');
    term = new window.Terminal({fontFamily:"'JetBrains Mono',Consolas,monospace",fontSize:13,
      cursorBlink:true, scrollOnUserInput:true, scrollback:5000, theme:XTERM_THEME});
    fit = new window.FitAddon.FitAddon(); term.loadAddon(fit);
    term.open(host);
    host.onpointerdown = ()=>focusTerminal(true);
    fit.fit();
    window.addEventListener('resize', resizeTerm);
    const target = term;
    term.onData(d => { if(term === target && !replayMode && !structuredMode && termInputSender && curSession
      && canSendInput(collabState)) termInputSender.push(d); });
    return true;
  } catch(error) {
    disposeTerminal();
    host.innerHTML = `<p class="muted" role="alert">${esc(error.message || 'Terminal could not start. Reload the page to retry.')}</p>`;
    return false;
  }
}

function disposeTerminal(){
  window.removeEventListener('resize', resizeTerm);
  const host = document.getElementById('term');
  if(host) host.onpointerdown = null;
  const old = term;
  term = null; fit = null;
  if(old){ try{ old.dispose(); }catch(e){} }
}

function resetTerminal(){
  disposeTerminal();
  const host = termHost();
  if(host) host.textContent = '';
  return setupTerm();
}

function resizeTerm(){
  if(!term || !fit || structuredMode) return;
  try { fit.fit(); sendResize(); } catch(e) {}
}
function sendResize(){
  if(term && !structuredMode && !replayMode && termWS && termWS.readyState===1 && curSession){
    try { termWS.send(JSON.stringify({type:'resize',session_id:curSession,cols:term.cols,rows:term.rows})); } catch(e) {}
  }
}

function setSessionSurface(surface, capability){
  const next = surface === 'terminal' || surface === 'structured' ? surface
    : currentSurface || (ui.supportsStructuredChat(capability) ? 'structured' : 'terminal');
  currentSurface = next;
  if(next === 'structured'){
    stopKeyboardHeartbeat();
    enterChatMode();
    return true;
  }
  if(structuredMode){
    resetStructuredChat();
    document.getElementById('chat-surface')?.remove();
    document.getElementById('chat-new')?.remove();
  }
  return !!term || resetTerminal();
}

// --- Structured chat surface ----------------------------------------------
function currentAgentCapability(){
  for(const devbox of devboxes){
    const agent = (devbox.agents || []).find(item => item.id === curAgentId);
    if(!agent) continue;
    const capability = ui.findRuntimeCapability(ui.runtimeCapabilities(devbox.capabilities), agent.runtime);
    return ui.capabilityForSurface(capability, currentSurface);
  }
  return null;
}

function formatFileSize(bytes){
  if(bytes < 1024) return `${bytes} B`;
  if(bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function filePayload(file){
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = ()=>reject(new Error(`Could not read ${file.name}`));
    reader.onload = ()=>{
      const result = String(reader.result || '');
      const comma = result.indexOf(',');
      if(comma < 0) return reject(new Error(`Could not encode ${file.name}`));
      resolve({name:file.name, type:file.type || '', size:file.size,
        data:result.slice(comma + 1)});
    };
    reader.readAsDataURL(file);
  });
}

function setChatComposerError(message){
  const error = document.getElementById('chat-composer-error');
  if(error) error.textContent = message || '';
}

function syncChatControls(){
  if(!chatState) return;
  const writable = !replayMode && canSendMessage(collabState);
  const input = document.getElementById('chat-input');
  if(input){
    input.disabled = !writable;
    input.placeholder = writable ? 'Message the agent\u2026  (Enter to send, Shift+Enter for newline)'
      : 'Read-only · Operator, Admin or Owner can send messages';
  }
  document.querySelectorAll('#chat-form button, #chat-controls button, #chat-controls input[type="file"], .chat-file-remove, .chat-perm button').forEach(el=>{ el.disabled = !writable; });
  const newChat = document.getElementById('chat-new');
  if(newChat) newChat.disabled = !writable;
  const sessionLocked = chatState.configured || chatState.items.length > 0;
  for(const control of chatControls){
    if(control.kind !== 'select') continue;
    const select = document.querySelector(`[data-chat-control="${control.key}"]`);
    if(!select) continue;
    const selected = chatControlValues[control.key];
    const validCustom = control.allow_custom && typeof selected === 'string';
    select.value = typeof selected === 'string' &&
      (control.choices.includes(selected) || validCustom) ? selected : '';
    select.disabled = !writable || control.scope === 'session' && sessionLocked;
    select.title = !writable ? 'Read-only' : select.disabled ? 'Fixed for this chat. Start a New chat to change it.' : '';
  }
  const note = document.getElementById('chat-session-note');
  if(note) note.hidden = !(sessionLocked && chatControls.some(control=>control.scope === 'session'));
}

function renderAttachmentTray(){
  const tray = document.getElementById('chat-file-tray');
  if(!tray) return;
  tray.textContent = '';
  for(const control of chatControls.filter(item => item.kind === 'file')){
    for(const [index, file] of (chatAttachments[control.key] || []).entries()){
      const chip = document.createElement('span');
      chip.className = 'chat-file-chip';
      const name = document.createElement('span');
      name.className = 'chat-file-name';
      name.textContent = file.name;
      const size = document.createElement('span');
      size.className = 'chat-file-size';
      size.textContent = formatFileSize(file.size);
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'chat-file-remove';
      remove.setAttribute('aria-label', `Remove ${file.name}`);
      remove.textContent = '\u00d7';
      remove.onclick = ()=>{
        chatAttachments[control.key].splice(index, 1);
        renderAttachmentTray();
        setChatComposerError('');
      };
      chip.append(name, size, remove);
      tray.appendChild(chip);
    }
  }
  tray.hidden = !tray.childElementCount;
}

async function addChatFiles(control, fileList){
  const view = viewEpoch, attachments = chatAttachments;
  if(!canSendMessage(collabState) || replayMode) return;
  setChatComposerError('');
  const current = chatAttachments[control.key] || [];
  const incoming = Array.from(fileList || []);
  if(current.length + incoming.length > control.max_files){
    setChatComposerError(`Up to ${control.max_files} files can be attached.`);
    return;
  }
  const total = current.reduce((sum, file)=>sum + file.size, 0) +
    incoming.reduce((sum, file)=>sum + file.size, 0);
  if(total > control.max_total_bytes){
    setChatComposerError(`Attachments can total up to ${formatFileSize(control.max_total_bytes)}.`);
    return;
  }
  try{
    const payloads = await Promise.all(incoming.map(filePayload));
    if(view !== viewEpoch || attachments !== chatAttachments || !canSendMessage(collabState)) return;
    chatAttachments[control.key] = current.concat(payloads);
    renderAttachmentTray();
  }catch(error){ if(view === viewEpoch && attachments === chatAttachments) setChatComposerError(error.message || 'Could not read that file.'); }
}

function setupChatControls(){
  chatControls = Chat.controlsFromCapability(currentAgentCapability());
  chatControlValues = {};
  chatAttachments = {};
  const toolbar = document.getElementById('chat-controls');
  if(!toolbar) return;
  toolbar.textContent = '';
  for(const control of chatControls){
    if(control.kind === 'select'){
      const label = document.createElement('label');
      label.className = 'chat-control';
      const caption = document.createElement('span');
      caption.textContent = control.label;
      let select;
      if(control.allow_custom){
        select = document.createElement('input');
        select.setAttribute('list', `chat-choices-${control.key}`);
        select.placeholder = control.key === 'model' ? 'Runtime default or model ID' : 'Runtime default';
        const datalist = document.createElement('datalist');
        datalist.id = `chat-choices-${control.key}`;
        for(const choice of control.choices){
          const option = document.createElement('option');
          option.value = choice;
          datalist.appendChild(option);
        }
        label.append(caption, select, datalist);
      }else{
        select = document.createElement('select');
        const fallback = document.createElement('option');
        fallback.value = '';
        fallback.textContent = 'Runtime default';
        select.appendChild(fallback);
        for(const choice of control.choices){
          const option = document.createElement('option');
          option.value = choice;
          option.textContent = choice;
          select.appendChild(option);
        }
        label.append(caption, select);
      }
      select.setAttribute('data-chat-control', control.key);
      select.oninput = ()=>{ chatControlValues[control.key] = select.value.trim(); };
      toolbar.appendChild(label);
    }else{
      chatAttachments[control.key] = [];
      const input = document.createElement('input');
      input.type = 'file';
      input.multiple = control.max_files > 1;
      input.accept = control.accept;
      input.hidden = true;
      input.onchange = async ()=>{
        await addChatFiles(control, input.files);
        input.value = '';
      };
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'chat-attach';
      button.textContent = control.label;
      button.onclick = ()=>input.click();
      toolbar.append(button, input);
    }
  }
  if(chatControls.some(control=>control.scope === 'session')){
    const note = document.createElement('span');
    note.id = 'chat-session-note';
    note.className = 'chat-session-note';
    note.textContent = 'Fixed for this chat · New chat to change';
    note.hidden = true;
    toolbar.appendChild(note);
  }
  toolbar.hidden = !chatControls.length;
  syncChatControls();
  renderAttachmentTray();
}

function enterChatMode(){
  if(structuredMode && document.getElementById('chat-surface')) return;
  const body = document.getElementById('stagebody');
  if(!body) return;
  disposeTerminal();
  document.getElementById('term')?.remove();
  structuredMode = true;
  chatState = Chat.initialChatState();
  const surface = document.createElement('div');
  surface.id = 'chat-surface';
  surface.className = 'chat-surface';
  surface.innerHTML =
      `<div id="chat-scroll" class="chat-scroll"></div>
       <div class="chat-composer">
         <div id="chat-controls" class="chat-controls" hidden></div>
         <div id="chat-file-tray" class="chat-file-tray" hidden></div>
         <form id="chat-form" class="chat-form">
           <textarea id="chat-input" class="chat-input" rows="2"
             placeholder="Message the agent\u2026  (Enter to send, Shift+Enter for newline)"></textarea>
           <button type="submit" class="chat-send">Send</button>
         </form>
         <div id="chat-composer-error" class="chat-composer-error" role="status"></div>
       </div>`;
    body.insertBefore(surface, document.getElementById('replaybar'));
    setupChatControls();
    const head = document.getElementById('termhead');
    if(head && !replayMode && !document.getElementById('chat-new')){
      const button = document.createElement('button');
      button.id = 'chat-new';
      button.type = 'button';
      button.className = 'ghost compact chat-new';
      button.textContent = 'New chat';
      button.title = 'Start a fresh session to change model or reasoning';
      button.onclick = ()=>void startNewChat(button);
      const collab = document.getElementById('collab');
      head.insertBefore(button, collab || null);
    }
    const form = document.getElementById('chat-form');
    const input = document.getElementById('chat-input');
    form.onsubmit = (e)=>{ e.preventDefault(); void sendChatMessage(); };
    input.addEventListener('keydown', (e)=>{
      if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); void sendChatMessage(); }
    });
    if(!replayMode) input.focus();
    renderChatSurface();
}

function renderChatSurface(){
  const scroll = document.getElementById('chat-scroll');
  if(scroll && Chat && chatState)
    Chat.renderChat(scroll, chatState, {onPermission: sendPermission});
  syncChatControls();
}

function sendChatMessage(){
  const input = document.getElementById('chat-input');
  if(!input || !structuredMode || replayMode || !chatState) return;
  const text = input.value;
  if(!text.trim()) return;
  if(!canSendMessage(collabState)){
    setChatComposerError('Read-only: an Operator, Admin or Owner role is required to send messages.');
    return;
  }
  const options = Chat.buildTurnOptions(
    chatControls, chatControlValues, chatAttachments);
  const attachmentMetadata = Object.values(chatAttachments).flat().map(file => ({
    name:file.name, type:file.type, size:file.size,
  }));
  try{
    if(!termInputSender || !termInputSender.push(text, options)){
      setChatComposerError('The session is reconnecting. Try again in a moment.');
      return;
    }
    Chat.appendUserTurn(chatState, text, attachmentMetadata);
    for(const key of Object.keys(chatAttachments)) chatAttachments[key] = [];
    renderAttachmentTray();
    setChatComposerError('');
    input.value = '';
    renderChatSurface();
    input.focus();
  }catch(error){ setChatComposerError(error.message || 'Message could not be sent. Your draft has been kept; try again.'); }
}

function sendPermission(requestId, allow){
  if(!structuredMode || replayMode || !canSendMessage(collabState)){
    lineError('Read-only: an Operator, Admin or Owner role is required to answer permission requests.');
    return;
  }
  if(!termWS || termWS.readyState!==1 || !curSession){
    lineError('The session is reconnecting. Try again in a moment.');
    return;
  }
  try {
    termWS.send(JSON.stringify({type:'permission',session_id:curSession,
      request_id:requestId, allow:!!allow}));
    if(chatState) chatState.pendingPermission = null;
    renderChatSurface();
  } catch(error) { lineError(error.message || 'Permission response could not be sent. Try again.'); }
}

function handleChatFrame(f){
  // Explicit terminal attachments must not be reinterpreted from event-looking output.
  if(currentSurface === 'terminal') return;
  setSessionSurface('structured');
  if(!chatState) return;
  const folded = Chat.foldEventPayload(chatState, f.data, f.type === 'restore');
  chatState = folded.state;
  if(folded.events.some(event => event.ev === 'session.config')){
    chatControlValues = Chat.reconcileControlValues(chatControls, chatControlValues, chatState.config);
  }
  renderChatSurface();
}


async function endCurrentSession(){
  const view = viewEpoch, session = curSession;
  if(!session || (!canSendInput(collabState) && !['admin', 'owner'].includes(collabState?.role))) return;
  if(!await showConfirm('End this session?', 'This stops the runtime for everyone in this session. Saved history remains available.', 'End session')) return;
  if(view !== viewEpoch || session !== curSession) return;
  try {
    if(!termWS || termWS.readyState !== 1) throw new Error('Reconnect before ending the session.');
    termWS.send(JSON.stringify({type:'terminate', session_id:session}));
  } catch(error) {
    lineError(error.message || 'Could not end the session.');
  }
}

async function startNewChat(button){
  if(!curAgentId || !curSession || !canSendMessage(collabState) || button?.disabled) return;
  const view = viewEpoch, agentId = curAgentId;
  const found = findAgent(curAgentId);
  if(!found) return;
  const agent = found.agent;
  const oldLabel = button?.textContent;
  if(button){ button.disabled = true; button.textContent = 'Starting...'; }
  try{
    await openAgent(agentId, agent.display_name || agent.handle, 'structured', true);
  }catch(error){
    if(view === viewEpoch) await showAlert('New chat could not start', error.message || 'The request failed.');
  }finally{
    if(button?.isConnected){ button.disabled = false; button.textContent = oldLabel || 'New chat'; }
  }
}

async function openAgent(agentId, name, surfaceOverride=null, forceNew=false){
  return openSession(agentId, name, {surface:surfaceOverride, forceNew});
}

function openLiveSession(session, name){
  return openSession(session.agent_id, name, {surface:session.surface, session});
}

async function openSession(agentId, name, {surface=null, forceNew=false, session=null}={}){
  closeOverlay();
  const view = clearAgentView();
  curAgentId = agentId;
  currentSurface = surface === 'terminal' || surface === 'structured' ? surface : null;
  renderFleet();
  const head = document.getElementById('termhead'), body = document.getElementById('stagebody');
  if(!head || !body) return;
  head.innerHTML =
    `<span class="title">@<span class="handle">${esc(name)}</span></span>
     <span class="sep">\u2014</span><span class="muted" id="livetag">opening session</span>
     <span class="spacer"></span>
     <button class="ghost" id="session-reconnect">Reconnect</button>
     <button class="ghost" id="session-new" disabled>New session</button>
     <button class="ghost danger" id="session-end" disabled>End session</button>
     <span id="collab" class="collab"></span>
     <span id="stat" class="status"></span>`;
  body.innerHTML = '<p class="muted">Opening session\u2026</p>';
  document.getElementById('session-reconnect').onclick = ()=>{
    if(view !== viewEpoch) return;
    if(curSession){ wantOpen = true; connectTermWS(); }
    else openSession(agentId, name, {surface:currentSurface, forceNew, session});
  };
  document.getElementById('session-new').onclick = ()=>{
    if(view === viewEpoch && canSendMessage(collabState)) openAgent(agentId, name, currentSurface, true);
  };
  document.getElementById('session-end').onclick = endCurrentSession;
  if(!await loadDevboxes() || view !== viewEpoch) return;
  const activeDevbox = devboxes.find(devbox =>
    (devbox.agents || []).some(agent => agent.id === agentId));
  const activeAgent = (activeDevbox?.agents || []).find(agent => agent.id === agentId);
  const capability = ui.findRuntimeCapability(activeDevbox?.capabilities, activeAgent?.runtime);
  currentSurface = currentSurface || ui.preferredSurface(capability);
  body.textContent = '';
  if(!setSessionSurface(currentSurface, capability)) return;
  renderCollab();
  try {
    const path = `/api/agents/${encodeURIComponent(agentId)}/sessions`;
    if(!session && !forceNew){
      const sessions = await api(path);
      if(view !== viewEpoch) return;
      session = ui.resumableSession(sessions, currentSurface);
    }
    const resumed = !!session;
    if(!session) session = await api(path, {method:'POST', body:JSON.stringify({surface:currentSurface})});
    if(view !== viewEpoch) return;
    curSession = session.id;
    if(!setSessionSurface(session.surface, capability)) return;
    const tag = document.getElementById('livetag');
    if(tag) tag.textContent = `${resumed ? 'resumed' : 'live'} ${structuredMode ? 'chat' : 'terminal'}`;
    wantOpen = true;
    connectTermWS();
    focusTerminal(true);
  } catch(error) {
    if(view === viewEpoch){ setStat('could not open session','busy'); lineError(error.message || 'Could not open session. Use Reconnect to retry.'); }
  }
}

function setStat(txt, state){
  const el = document.getElementById('stat');
  if(el){ el.className = 'status is-' + (state||'offline');
    el.innerHTML = `<span class="status-dot"></span><span class="status-label">${esc(txt)}</span>`; }
}

function connectTermWS(){
  if(!wantOpen || !curSession || replayMode) return;
  const view = viewEpoch, session = curSession;
  if(reconnectTimer){ clearTimeout(reconnectTimer); reconnectTimer = null; }
  stopKeyboardHeartbeat();
  collabState = null;
  keyboardRequester = null;
  renderCollab();
  if(termInputSender){ termInputSender.close(); termInputSender = null; }
  const old = termWS;
  termWS = null;
  if(old){ try{ old.close(); }catch(e){} }
  const proto = location.protocol==='https:'?'wss':'ws';
  const socket = new WebSocket(`${proto}://${location.host}/ws/term`);
  termWS = socket;
  const isCurrent = ()=>socket === termWS && view === viewEpoch && session === curSession;
  termInputSender = ui.createTerminalInputSender((data, options) => {
    if(!isCurrent() || !wantOpen || replayMode || socket.readyState!==1
      || !(structuredMode ? canSendMessage(collabState) : canSendInput(collabState))) return false;
    socket.send(JSON.stringify({
      type:'input', session_id:session, data:data, options:options,
    }));
    return true;
  });
  socket.onopen = ()=>{
    if(!isCurrent() || !wantOpen) return;
    reconnectDelay = 500;
    setStat('live', 'online');
    socket.send(JSON.stringify({type:'attach',session_id:session,
      cols:term?.cols || 120,rows:term?.rows || 30,surface:currentSurface}));
  };
  socket.onmessage = (ev)=>{
    if(!isCurrent() || !wantOpen) return;
    let f;
    try { f = JSON.parse(ev.data); } catch { lineError('Received an invalid session frame. Reconnect to retry.'); return; }
    if(!f || typeof f !== 'object') return;
    if(f.session_id && f.session_id!==curSession) return;
    // Structured (chat) frames carry a canonical event JSON in `data`. Render
    // them into the chat surface instead of the terminal. Everything else
    // (status/exit/collaboration/keyboard) still flows through the switch.
    if(f.kind==='event' && (f.type==='output' || f.type==='restore')){
      handleChatFrame(f);
      return;
    }
    switch(f.type){
      case 'ready':
      case 'session.ready':
        setSessionSurface(f.surface, f.capabilities);
        renderCollab();
        focusTerminal(true);
        setStat(currentSurface === 'structured' ? 'chat ready' : 'terminal ready','online');
        break;
      case 'runtime.unavailable': {
        const reason = String(f.code || 'runtime_unavailable').replaceAll('_', ' ');
        setStat(`runtime unavailable: ${reason}`,'busy');
        lineError(`Runtime unavailable: ${reason}. Check the connector runtime configuration and reconnect.`);
        break;
      }
      case 'restore':          // reconnect: instantly repaint current screen
        if(!structuredMode && term){ term.reset(); term.write(f.data || ''); } break;
      case 'output':
        if(!structuredMode && term) term.write(f.data || ''); break;
      case 'status':
        if(f.surface){ setSessionSurface(f.surface); renderCollab(); }
        if(f.state==='live') setStat('live','online');
        else if(f.state==='offline'){ setStat('devbox offline','busy');
          lineError('Devbox offline \u2014 the connector is not running. Reconnect the connector, then retry.'); }
        else if(f.state==='ended'){ setStat('ended','offline');
          wantOpen = false; stopKeyboardHeartbeat();
          lineError(`Session ended, code ${f.code}. Start a New session to continue.`); renderCollab(); }
        break;
      case 'exit':
        setStat('ended','offline');
        wantOpen = false; stopKeyboardHeartbeat();
        if(!structuredMode && term && f.data) term.write(f.data);
        lineError(`Session ended, code ${f.code}. Start a New session to continue.`); renderCollab(); break;
      case 'error':
        lineError(f.message || 'The session request failed.'); break;
      case 'snapshot':
      case 'collaboration': {
        const hadKeyboard = !!(collabState && collabState.isHolder);
        if(f.surface) setSessionSurface(f.surface);
        collabState = deriveCollaborationState(f, me ? {id: me.id, username: me.username} : null);
        if(!collabState.isHolder){
          keyboardRequester = null;
        }
        renderCollab();
        if(!hadKeyboard && collabState.isHolder) focusTerminal(true);
        break;
      }
      case 'keyboard_request':
        if(!structuredMode && collabState && collabState.isHolder){
          keyboardRequester = {id:f.requester_user_id, username:f.requester_username};
          renderCollab();
        }
        break;
    }
  };
  socket.onerror = ()=>{ if(isCurrent()) lineError('Session connection failed. Check your connection or use Reconnect.'); };
  socket.onclose = ()=>{
    if(!isCurrent()) return;
    stopKeyboardHeartbeat();
    if(termInputSender){ termInputSender.close(); termInputSender = null; }
    collabState = null;
    keyboardRequester = null;
    renderCollab();
    if(!wantOpen) return;
    setStat('reconnecting\u2026','busy');
    reconnectTimer = setTimeout(()=>{ if(isCurrent() && wantOpen) connectTermWS(); }, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay*2, 5000);  // exponential backoff
  };
}

function lineError(message){
  if(structuredMode && chatState){
    chatState.items.push({kind:'error', text:String(message)});
    renderChatSurface();
    setChatComposerError(message);
  } else if(term) term.write(`\r\n[error] ${message}\r\n`);
  else {
    const body = document.getElementById('stagebody');
    if(body){ const error = document.createElement('p'); error.className = 'muted'; error.setAttribute('role','alert'); error.textContent = message; body.appendChild(error); }
  }
}

// ---------------- collaboration (keyboard lease) ----------------
function requestKeyboard(){
  if(!structuredMode && !replayMode && collabState?.canRequest && termWS && termWS.readyState===1 && curSession)
    termWS.send(JSON.stringify({type:'keyboard_acquire',session_id:curSession}));
}
function releaseKeyboard(){
  if(!structuredMode && !replayMode && canSendInput(collabState) && termWS && termWS.readyState===1 && curSession)
    termWS.send(JSON.stringify({type:'keyboard_release',session_id:curSession}));
}
function handoffKeyboard(){
  if(!structuredMode && !replayMode && canSendInput(collabState) && termWS && termWS.readyState===1 && curSession && keyboardRequester)
    termWS.send(JSON.stringify({type:'keyboard_handoff',session_id:curSession,
      target_user_id:keyboardRequester.id}));
  keyboardRequester = null;
}
let keyboardHeartbeat = null;
function stopKeyboardHeartbeat(){
  if(keyboardHeartbeat !== null){ clearInterval(keyboardHeartbeat); keyboardHeartbeat = null; }
}
function syncKeyboardHeartbeat(){
  if(structuredMode || replayMode || !wantOpen || !canSendInput(collabState) || !termWS || termWS.readyState!==1){
    stopKeyboardHeartbeat();
    return;
  }
  if(keyboardHeartbeat !== null) return;
  const socket = termWS, session = curSession, view = viewEpoch;
  keyboardHeartbeat = setInterval(()=>{
    if(view !== viewEpoch || socket !== termWS || session !== curSession || structuredMode || !canSendInput(collabState)) return;
    try { socket.send(JSON.stringify({type:'keyboard_renew',session_id:session})); } catch(e) {}
  }, 20000);
}
// Render the compact keyboard status + action into #collab (lives in termhead).
function renderCollab(){
  syncKeyboardHeartbeat();
  syncChatControls();
  const newSession = document.getElementById('session-new');
  if(newSession) newSession.disabled = !canSendMessage(collabState);
  const endSession = document.getElementById('session-end');
  if(endSession) endSession.disabled = !canSendInput(collabState) &&
    !['admin', 'owner'].includes(collabState?.role);
  if(term) term.options.disableStdin = replayMode || !canSendInput(collabState);
  const el = document.getElementById('collab');
  if(!el) return;
  const s = collabState;
  const view = collabHeaderView(s, keyboardRequester, currentSurface);
  let btn = '';
  if(view.button === 'handoff'){
    btn = '<button class="ghost" id="collab-handoff">Hand off</button>';
  } else if(view.button === 'release'){
    btn = '<button class="ghost" id="collab-release">Release</button>';
  } else if(view.button === 'request'){
    const text = s && s.heldByOther ? 'Request' : 'Take keyboard';
    btn = `<button class="ghost" id="collab-request">${text}</button>`;
  }
  el.className = 'collab ' + view.cls;
  el.innerHTML = `<span class="collab-dot"></span><span class="collab-label">${esc(view.label)}</span>${btn}`;
  const rq = document.getElementById('collab-request'); if(rq) rq.onclick = requestKeyboard;
  const rl = document.getElementById('collab-release'); if(rl) rl.onclick = releaseKeyboard;
  const ho = document.getElementById('collab-handoff'); if(ho) ho.onclick = handoffKeyboard;
}

// Pure helpers (testable, no DOM/side-effects).

// Format seconds as m:ss.
function fmtTime(sec){ return formatClock(sec); }

// Total duration = time of last event/checkpoint (fallback 0).
function replayDuration(replay){
  let t = 0;
  for(const e of (replay.events||[])) if(typeof e.time==='number' && e.time>t) t=e.time;
  for(const c of (replay.checkpoints||[])) if(typeof c.time==='number' && c.time>t) t=c.time;
  return t;
}

// State for the active replay.
let replay = null, replayTimer = null, replayPlaying = false, replaySpeed = 1,
    replayCursor = 0, replayAgentId = null, replayAgentName = '';

function stopReplay(){
  replayMode = false;
  replayPlaying = false;
  if(replayTimer){ clearTimeout(replayTimer); replayTimer = null; }
  if(term) term.options && (term.options.disableStdin = false);
}

function clearAgentView(){
  ++viewEpoch;
  stopReplay();
  stopKeyboardHeartbeat();
  const replaybar = document.getElementById('replaybar');
  if(replaybar) replaybar.remove();
  wantOpen = false;
  if(reconnectTimer){ clearTimeout(reconnectTimer); reconnectTimer = null; }
  if(termInputSender){ termInputSender.close(); termInputSender = null; }
  const old = termWS;
  termWS = null;
  if(old){ try{ old.close(); }catch(e){} }
  disposeTerminal();
  curAgentId = null;
  curSession = null;
  collabState = null;
  keyboardRequester = null;
  currentSurface = null;
  resetStructuredChat();
  replay = null;
  replayAgentId = null;
  replayAgentName = '';
  renderStageEmpty();
  return viewEpoch;
}

async function openHistory(agentId, name){
  closeOverlay();
  const surface = currentSurface;
  const view = clearAgentView();
  curAgentId = agentId;
  renderFleet();

  replayAgentId = agentId; replayAgentName = name;
  const head = document.getElementById('termhead'), body = document.getElementById('stagebody');
  if(!head || !body) return;
  head.innerHTML =
    `<span class="title">@<span class="handle">${esc(name)}</span></span>
     <span class="sep">\u2014</span><span class="muted">session history</span>
     <span class="spacer"></span>
     <button class="ghost" id="hist-live">\u2190 Back to live</button>`;
  document.getElementById('hist-live').onclick = ()=>openAgent(agentId, name, surface);
  body.innerHTML = '<p class="muted">Loading session history\u2026</p>';
  let sessions;
  try { sessions = await api(`/api/agents/${encodeURIComponent(agentId)}/sessions`); }
  catch(e){ if(view === viewEpoch) lineError(`Could not load history: ${e.message}`); return; }
  if(view !== viewEpoch) return;

  const list = sessions.map(s=>{
    const alive = s.alive ?? s.state === 'live';
    const st = alive ? 'online' : 'offline';
    return `<div class="history-item">
      <span class="status is-${st}"><span class="status-dot"></span><span class="status-label">${esc(s.state||'')}</span></span>
      <b class="mono">${esc(s.id)}</b>
      <span class="muted">${s.surface === 'structured' ? 'Chat' : s.surface === 'terminal' ? 'Terminal' : 'Legacy surface unknown'} · started ${esc(s.created_at||'')}</span>
      <span class="spacer"></span>
      ${alive && s.available !== false ? `<button class="ghost" data-attach="${esc(s.id)}">Attach live</button>` : ''}
      <button class="ghost" data-replay="${esc(s.id)}">Replay</button>
    </div>`;
  }).join('') || '<div class="muted" style="padding:14px">No sessions recorded.</div>';
  body.innerHTML = `<div class="history-wrap">
    <div class="history-head">Session history for @${esc(name)}</div>${list}</div>`;
  body.querySelectorAll('[data-attach]').forEach(b=>b.onclick=()=>{
    const session = sessions.find(s=>s.id === b.dataset.attach);
    if(session && view === viewEpoch) openLiveSession({...session,agent_id:agentId}, name);
  });
  body.querySelectorAll('[data-replay]').forEach(b=>
    b.onclick=()=>startReplay(b.dataset.replay, sessions.find(s=>s.id === b.dataset.replay)));
}

async function startReplay(sessionId, session=null){
  closeOverlay();
  const agentId = session?.agent_id || replayAgentId || curAgentId;
  const name = replayAgentName || findAgent(agentId)?.agent.display_name || agentId || '';
  const view = clearAgentView();
  curAgentId = agentId;
  replayAgentId = agentId; replayAgentName = name;
  const body = document.getElementById('stagebody'), head = document.getElementById('termhead');
  if(!body || !head) return;
  body.innerHTML = '<p class="muted">Loading replay\u2026</p>';
  head.innerHTML = `<span class="title">@${esc(name)}</span><span class="sep">\u2014</span><span class="muted">replay (read-only)</span>
    <span class="spacer"></span><button class="ghost" id="rp-history">\u2190 History</button>`;
  document.getElementById('rp-history').onclick = ()=>openHistory(agentId, name);
  let data;
  try { data = await api(`/api/sessions/${encodeURIComponent(sessionId)}/replay`); }
  catch(e){ if(view === viewEpoch) lineError(`Replay unavailable: ${e.message}`); return; }
  if(view !== viewEpoch) return;
  replay = normalizeReplay(data);
  replayMode = true;
  replayPlaying = false;
  replaySpeed = 1;
  replayCursor = 0;
  const total = replayDuration(replay);

  const surface = data.surface || session?.surface ||
    (replay.events.some(event=>event.kind === 'event' || event.type === 'event') ? 'structured' : 'terminal');
  body.innerHTML = '<div id="term"></div>';
  const bar = document.createElement('div');
  bar.id = 'replaybar';
  bar.style.cssText = 'padding:8px 12px;border-top:1px solid var(--border);background:var(--panel)';
  const ret = replay.retention || (replay.metadata&&replay.metadata.retention) || 'permanent';
  bar.innerHTML = `
    <div class="row" style="gap:8px;flex-wrap:wrap">
      <button class="ghost" id="rp-playpause">\u25b6 play</button>
      <button class="ghost" id="rp-start">\u23ee start</button>
      <button class="ghost" id="rp-end">\u23ed end</button>
      <select id="rp-speed" style="width:auto">
        <option value="0.5">0.5x</option>
        <option value="1" selected>1x</option>
        <option value="2">2x</option>
        <option value="8">8x</option>
      </select>
      <input type="range" id="rp-seek" min="0" max="${total}" step="0.01" value="0" style="flex:1;min-width:160px"/>
      <span class="muted" id="rp-time">0:00 / ${fmtTime(total)}</span>
    </div>
    <div class="row" style="gap:8px;margin-top:6px;flex-wrap:wrap">
      <button class="ghost" id="rp-final">final screen</button>
      <a class="ghost" id="rp-download" href="/api/sessions/${encodeURIComponent(sessionId)}/recording" download style="text-decoration:none;padding:6px 12px;border:1px solid var(--border);border-radius:6px;color:var(--fg)">download asciicast</a>
      <span class="spacer"></span>
      <span class="muted">retention:</span>
      <select id="rp-retention" style="width:auto">
        <option value="none">none</option>
        <option value="7d">7 days</option>
        <option value="30d">30 days</option>
        <option value="permanent">permanent</option>
      </select>
      <span class="muted" id="rp-retmsg"></span>
      <button class="ghost danger" id="btnDeleteReplay">Delete recording</button>
    </div>`;
  body.appendChild(bar);
  const mounted = setSessionSurface(surface);
  if(term) term.options.disableStdin = true;
  if(structuredMode){
    document.getElementById('chat-form').hidden = true;
    document.getElementById('chat-controls').hidden = true;
    document.getElementById('rp-final').textContent = 'final transcript';
  }

  const selRet = document.getElementById('rp-retention');
  const retmsg = document.getElementById('rp-retmsg');
  const btnDeleteReplay = document.getElementById('btnDeleteReplay');
  const workspace = ui.selectWorkspace(workspaces, activeWorkspaceId);
  const canManage = !!workspace && ui.canAdminWorkspace(workspace.role);
  selRet.disabled = !canManage;
  btnDeleteReplay.disabled = !canManage;
  selRet.value = ret;
  selRet.onchange = async ()=>{
    if(selRet.disabled || view !== viewEpoch) return;
    selRet.disabled = true;
    try {
      await api(`/api/sessions/${encodeURIComponent(sessionId)}/retention`,
        {method:'PATCH', body: JSON.stringify({retention: selRet.value})});
      if(view === viewEpoch) retmsg.textContent = 'saved';
    } catch(e){ if(view === viewEpoch) retmsg.textContent = e.message || 'Could not save retention.'; }
    finally { if(view === viewEpoch) selRet.disabled = !canManage; }
  };
  btnDeleteReplay.onclick = async()=>{
    if(btnDeleteReplay.disabled || view !== viewEpoch) return;
    btnDeleteReplay.disabled = true;
    try {
      if(!await showConfirm('Delete recording?', 'Permanently erase this recorded transcript and checkpoints. This cannot be undone.', 'Delete recording') || view !== viewEpoch) return;
      await api(`/api/sessions/${encodeURIComponent(sessionId)}/recording`, {method:'DELETE'});
      if(view === viewEpoch) await startReplay(sessionId, session);
    } catch(error){ if(view === viewEpoch) retmsg.textContent = error.message || 'Could not delete recording.'; }
    finally { if(view === viewEpoch) btnDeleteReplay.disabled = !canManage; }
  };

  document.getElementById('rp-speed').onchange = (e)=>{ replaySpeed = Number(e.target.value)||1; };
  document.getElementById('rp-playpause').onclick = toggleReplayPlay;
  document.getElementById('rp-start').onclick = ()=>replaySeek(0);
  document.getElementById('rp-end').onclick = ()=>replaySeek(total);
  document.getElementById('rp-final').onclick = ()=>showFinalScreen();
  document.getElementById('rp-seek').oninput = (e)=>{ pauseReplay(); replaySeek(Number(e.target.value)); };

  if(mounted) replaySeek(structuredMode ? total : 0);
}

// Reset terminal, load nearest checkpoint <= target, apply subsequent events.
function replaySeek(target){
  if(!replay || !replayMode) return;
  const total = replayDuration(replay);
  target = Math.max(0, Math.min(target, total));
  replayCursor = target;
  if(structuredMode){
    chatState = Chat.initialChatState();
    for(const event of eventsBetween(replay.events, -Infinity, target)){
      if(event.kind === 'event' || event.type === 'event') chatState = Chat.foldEventPayload(chatState, event.data, false).state;
    }
    renderChatSurface();
    updateReplayUI();
    return;
  }
  if(!term) return;
  term.reset();
  const ci = nearestCheckpointIndex(replay.checkpoints, target);
  let startTime = -Infinity, startCursor = null;
  if(ci>=0){
    const cp = replay.checkpoints[ci];
    if(cp.serialized_screen) term.write(cp.serialized_screen);
    startTime = cp.time;
    startCursor = cp.cursor;
  }
  for(const e of eventsBetween(replay.events, startTime, target, startCursor)){
    if((e.type==='o' || e.type==='output') && e.data!=null) term.write(e.data);
  }
  updateReplayUI();
}

function updateReplayUI(){
  const total = replayDuration(replay);
  const seek = document.getElementById('rp-seek');
  const timeEl = document.getElementById('rp-time');
  if(seek) seek.value = replayCursor;
  if(timeEl) timeEl.textContent = fmtTime(replayCursor)+' / '+fmtTime(total);
  const pp = document.getElementById('rp-playpause');
  if(pp) pp.textContent = replayPlaying ? '\u2758\u2758 pause' : '\u25b6 play';
}

function toggleReplayPlay(){ replayPlaying ? pauseReplay() : playReplay(); }

function pauseReplay(){
  replayPlaying = false;
  if(replayTimer){ clearTimeout(replayTimer); replayTimer = null; }
  updateReplayUI();
}

function playReplay(){
  if(!replay || !replayMode || (!structuredMode && !term)) return;
  const total = replayDuration(replay);
  if(replayCursor >= total) replaySeek(0);
  replayPlaying = true;
  updateReplayUI();
  scheduleReplayStep();
}

// Advance to the next event after replayCursor, honoring speed.
function scheduleReplayStep(){
  if(!replayPlaying || !replayMode) return;
  const next = (replay.events||[]).find(e=> typeof e.time==='number' && e.time>replayCursor+1e-9);
  if(!next){ pauseReplay(); return; }
  const delayMs = Math.max(0, (next.time - replayCursor) * 1000 / replaySpeed);
  const view = viewEpoch, activeReplay = replay;
  replayTimer = setTimeout(()=>{
    if(view !== viewEpoch || replay !== activeReplay || !replayPlaying || !replayMode) return;
    if(structuredMode){
      replaySeek(next.time);
      scheduleReplayStep();
      return;
    }
    if(!term) return;
    const batch = (replay.events||[]).filter(e =>
      typeof e.time==='number' && e.time>replayCursor+1e-9 && e.time<=next.time);
    replayCursor = next.time;
    for(const event of batch)
      if((event.type==='o' || event.type==='output') && event.data!=null) term.write(event.data);
    updateReplayUI();
    scheduleReplayStep();
  }, delayMs);
}

// Static preview of the final screen (last checkpoint) and seek to end.
function showFinalScreen(){
  if(!replay || !replayMode) return;
  pauseReplay();
  const total = replayDuration(replay);
  if(structuredMode){ replaySeek(total); return; }
  if(!term) return;
  const cps = replay.checkpoints || [];
  const last = cps.length ? cps[cps.length-1] : null;
  if(last && last.serialized_screen && last.time >= (replay.events||[]).reduce((m,e)=>Math.max(m,e.time||0),0)){
    term.reset();
    term.write(last.serialized_screen);
    replayCursor = total;
    updateReplayUI();
  } else {
    replaySeek(total);
  }
}

// ---------------- command palette ----------------
let paletteState = null; // {items, filtered, index}

function openCommandPalette(){
  if(!ui || !me) return;
  const workspace = ui.selectWorkspace(workspaces, activeWorkspaceId);
  const workspaceBoxes = workspace ? ui.devboxesForWorkspace(devboxes, workspace.id) : [];
  let items = ui.commandItems({devboxes:workspaceBoxes, isOwner: me.role==='owner'});
  if(!workspace || !ui.canAdminWorkspace(workspace.role)) items = items.filter(item=>item.id !== 'devbox.create');
  return showOverlay(`
    <div class="palette" role="dialog" aria-label="Command palette">
      <div class="palette-input-wrap">
        <span class="icon">\u203a</span>
        <input class="palette-input" id="palette-q" placeholder="Search agents, history, actions\u2026" autocomplete="off"/>
      </div>
      <div class="palette-list" id="palette-list"></div>
      <div class="palette-foot">
        <span><span class="kbd">\u2191\u2193</span> navigate</span>
        <span><span class="kbd">\u21b5</span> open</span>
        <span><span class="kbd">esc</span> close</span>
      </div>
    </div>`, overlay=>{
  paletteState = {items, filtered: items, index: 0};
  const input = overlay.querySelector('#palette-q');
  input.oninput = ()=>{
    if(overlay !== overlayEl) return;
    paletteState.filtered = ui.filterCommands(paletteState.items, input.value);
    paletteState.index = 0;
    renderPaletteList();
  };
  input.onkeydown = (e)=>{
    if(overlay !== overlayEl) return;
    if(e.key==='ArrowDown'){ e.preventDefault();
      paletteState.index = ui.moveSelection(paletteState.index, 1, paletteState.filtered.length); renderPaletteList(); }
    else if(e.key==='ArrowUp'){ e.preventDefault();
      paletteState.index = ui.moveSelection(paletteState.index, -1, paletteState.filtered.length); renderPaletteList(); }
    else if(e.key==='Enter'){ e.preventDefault(); runPaletteItem(paletteState.filtered[paletteState.index]); }
    else if(e.key==='Escape'){ e.preventDefault(); closeOverlay(); }
  };
  renderPaletteList();
  input.focus();
  });
}

function renderPaletteList(){
  const list = document.getElementById('palette-list');
  if(!list || !paletteState) return;
  const badge = (kind)=> kind==='agent'?'agent':kind==='history'?'history':'action';
  list.innerHTML = paletteState.filtered.map((it,i)=>`
    <div class="palette-item${i===paletteState.index?' is-active':''}" data-i="${i}">
      <span class="p-badge">${esc(badge(it.kind))}</span>
      <div class="p-main">
        <div class="p-title">${esc(it.title)}</div>
        <div class="p-sub">${esc(it.subtitle||'')}</div>
      </div>
    </div>`).join('') || '<div class="palette-empty">No matches.</div>';
  list.querySelectorAll('[data-i]').forEach(el=>{
    el.onmousemove = ()=>{ paletteState.index = Number(el.dataset.i);
      list.querySelectorAll('.palette-item').forEach(n=>n.classList.remove('is-active'));
      el.classList.add('is-active'); };
    el.onclick = ()=> runPaletteItem(paletteState.filtered[Number(el.dataset.i)]);
  });
  const active = list.querySelector('.is-active');
  if(active) active.scrollIntoView({block:'nearest'});
}

function findAgent(agentId){
  for(const d of devboxes) for(const a of (d.agents||[]))
    if(a.id===agentId) return {agent:a, devbox:d};
  return null;
}

function runPaletteItem(item){
  if(!item) return;
  closeOverlay();
  if(item.id==='devbox.create'){ createDevbox(); return; }
  if(item.id==='owner.open'){ if(me.role==='owner') renderOwner(); return; }
  if(item.kind==='agent'){ const f = findAgent(item.agentId);
    if(f) openAgent(f.agent.id, f.agent.display_name); return; }
  if(item.kind==='history'){ const f = findAgent(item.agentId);
    if(f) openHistory(f.agent.id, f.agent.display_name); return; }
}

// Global shortcut: Ctrl/Cmd+K toggles the palette (only inside the shell).
document.addEventListener('keydown', (e)=>{
  if((e.ctrlKey||e.metaKey) && (e.key==='k'||e.key==='K')){
    if(!document.getElementById('fleet')) return; // only in the main shell
    e.preventDefault();
    if(document.getElementById('overlay')) closeOverlay();
    else openCommandPalette();
  }
});

// ---------------- overlay: palette + modals ----------------
let overlayEl = null, dismissOverlay = null, overlayEpoch = 0;
function closeOverlay(){
  ++overlayEpoch;
  if(dismissOverlay) dismissOverlay(null);
  else document.getElementById('overlay')?.remove();
  paletteState = null;
}

// One lifecycle for dialogs and forms. Replacement is cancellation, not a lost
// promise; late/double clicks cannot remove the next overlay or submit it.
function showOverlay(html, onReady){
  closeOverlay();
  return new Promise((resolve,reject)=>{
    const overlay = document.createElement('div');
    overlay.className = 'overlay'; overlay.id = 'overlay';
    overlay.innerHTML = html;
    let settled = false;
    const done = (value,error)=>{
      if(settled) return;
      settled = true;
      document.removeEventListener('keydown', onKey);
      overlay.remove();
      if(overlayEl === overlay){
        overlayEl = null; dismissOverlay = null; paletteState = null; ++overlayEpoch;
      }
      error ? reject(error) : resolve(value);
    };
    const onKey = event=>{
      if(overlayEl === overlay && event.key === 'Escape'){ event.preventDefault(); done(null); }
    };
    overlay.onclick = event=>{ if(event.target === overlay) done(null); };
    overlayEl = overlay;
    dismissOverlay = done;
    document.addEventListener('keydown', onKey);
    document.body.appendChild(overlay);
    try { if(onReady) onReady(overlay,done); } catch(error) { done(null,error); }
  });
}

// Generic modal. Returns a Promise resolving to the chosen action's value.
function showModal({title, desc, bodyHtml, actions, onReady}){
    const acts = actions || [{label:'OK', primary:true, value:true}];
    return showOverlay(`<div class="modal" role="dialog" aria-label="${esc(title||'')}">
      <div class="modal-head"><h3>${esc(title||'')}</h3>${desc?`<p>${esc(desc)}</p>`:''}</div>
      ${bodyHtml?`<div class="modal-body">${bodyHtml}</div>`:''}
      <div class="modal-actions">${acts.map((a,i)=>
        `<button class="${a.primary?'':'ghost'}${a.danger?' danger':''}" data-a="${i}">${esc(a.label)}</button>`).join('')}</div>
    </div>`, (overlay,done)=>{
    overlay.querySelectorAll('[data-a]').forEach(b=>
      b.onclick = ()=> done(acts[Number(b.dataset.a)].value));
    if(onReady) onReady(overlay);
  });
}

function showAlert(title, message){
  return showModal({title, desc:message, actions:[{label:'OK', primary:true, value:true}]});
}
function showConfirm(title, message, confirmLabel){
  return showModal({title, desc:message, actions:[
    {label:'Cancel', value:false},
    {label:confirmLabel||'Confirm', primary:true, danger:true, value:true}]});
}

// Form modal. Returns {field:value,...} on submit, or null on cancel.
function showForm({title, desc, fields, submit, extraHtml='', onReady=null}){
    const fieldHtml = fields.map((f,i)=>{
      const id = 'f_'+i;
      const control = f.type==='select'
        ? `<select id="${id}" data-field="${esc(f.name)}">${f.options.map(option=>{
            const value = typeof option==='object' ? option.value : option;
            const label = typeof option==='object' ? option.label : option;
            return `<option value="${esc(value)}"${value===f.value?' selected':''}>${esc(label)}</option>`;
          }).join('')}</select>`
        : `<input id="${id}" data-field="${esc(f.name)}" type="${esc(f.type||'text')}" placeholder="${esc(f.placeholder||'')}" value="${esc(f.value||'')}"/>`;
      return `<div class="field"><label for="${id}">${esc(f.label)}</label>${control}${f.helpHtml||''}</div>`;
    }).join('');
    return showOverlay(`<div class="modal" role="dialog" aria-label="${esc(title||'')}">
      <div class="modal-head"><h3>${esc(title||'')}</h3>${desc?`<p>${esc(desc)}</p>`:''}</div>
      <div class="modal-body">${fieldHtml}${extraHtml}</div>
      <div class="modal-err" id="form-err"></div>
      <div class="modal-actions">
        <button class="ghost" data-cancel>Cancel</button>
        <button data-submit>${esc(submit||'Save')}</button>
      </div></div>`, (overlay,done)=>{
    const collect = ()=>{
      const out = {};
      for(let i=0;i<fields.length;i++) out[fields[i].name] = overlay.querySelector('#f_'+i).value.trim();
      for(const f of fields) if(f.required && !out[f.name]){
        overlay.querySelector('#form-err').textContent = f.label+' is required.'; return null; }
      return out;
    };
    const submitForm = ()=>{ if(overlay !== overlayEl) return; const v = collect(); if(v) done(v); };
    overlay.onkeydown = (e)=>{
      if(e.key==='Enter' && !e.isComposing && !['TEXTAREA','BUTTON'].includes(e.target?.tagName)){
        e.preventDefault(); submitForm();
      }
    };
    overlay.querySelector('[data-cancel]').onclick = ()=> done(null);
    overlay.querySelector('[data-submit]').onclick = submitForm;
    if(onReady) onReady(overlay);
    const first = overlay.querySelector('input,select'); if(first && overlay === overlayEl) first.focus();
  });
}

// ---------------- start ----------------
boot().catch(error => {
  app.innerHTML = `<div class="auth"><div class="auth-card"><h2>Unable to start</h2>
    <p class="muted">${esc(error.message)}</p></div></div>`;
});
