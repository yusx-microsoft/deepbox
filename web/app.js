// deepbox minimal SPA
const app = document.getElementById('app');
let me = null, devboxes = [], term = null, fit = null, termWS = null, curSession = null;
const hashParams = new URLSearchParams(location.hash.replace(/^#/, ''));
const queryParams = new URLSearchParams(location.search);
let pendingInvite = hashParams.get('invite') || queryParams.get('invite') || '';
if (pendingInvite) history.replaceState(null, '', location.pathname);

async function api(path, opts={}) {
  const r = await fetch(path, {credentials:'same-origin',
    headers:{'Content-Type':'application/json'}, ...opts});
  if (!r.ok) throw new Error((await r.json().catch(()=>({detail:r.statusText}))).detail);
  return r.status === 204 ? null : r.json();
}

// ---------------- auth ----------------
async function renderLogin() {
  // First-owner bootstrap form when available.
  let status = {available:false};
  try { status = await api('/api/auth/bootstrap-status'); } catch {}
  if (status.available) return renderBootstrap();

  const inviteFromUrl = pendingInvite;
  app.innerHTML = `<div class="center stack card">
    <h2>deepbox</h2>
    <div class="muted">Connect your devbox agents. Chat like you're local.</div>
    <input id="u" placeholder="username"/>
    <input id="p" type="password" placeholder="password"/>
    <div class="row"><button id="login">Login</button></div>
    <h4>Have an invite code?</h4>
    <input id="inv" placeholder="invite code" value="${escapeHtml(inviteFromUrl)}"/>
    <input id="rd" placeholder="display name (optional)"/>
    <div class="row"><button class="ghost" id="reg">Register with invite</button></div>
    <div id="err" class="muted"></div></div>`;
  login.onclick = async () => {
    try {
      me = await api('/api/auth/login', {method:'POST', body: JSON.stringify({
        username:u.value, password:p.value})});
      boot();
    } catch(e){ err.textContent = e.message; }
  };
  reg.onclick = async () => {
    try {
      me = await api('/api/auth/register', {method:'POST', body: JSON.stringify({
        username:u.value, password:p.value, display_name:rd.value||undefined,
        invite_code:inv.value||undefined})});
      pendingInvite = '';
      boot();
    } catch(e){ err.textContent = e.message; }
  };
}

function renderBootstrap() {
  app.innerHTML = `<div class="center stack card">
    <h2>deepbox — first owner setup</h2>
    <div class="muted">Create the first owner account with the bootstrap token.</div>
    <input id="bt" type="password" placeholder="bootstrap token"/>
    <input id="bu" placeholder="username"/>
    <input id="bp" type="password" placeholder="password"/>
    <input id="bd" placeholder="display name (optional)"/>
    <div class="row"><button id="bgo">Create owner</button></div>
    <div id="err" class="muted"></div></div>`;
  bgo.onclick = async () => {
    try {
      me = await api('/api/auth/bootstrap', {method:'POST', body: JSON.stringify({
        token:bt.value, username:bu.value, password:bp.value,
        display_name:bd.value||undefined})});
      boot();
    } catch(e){ err.textContent = 'Setup failed.'; }
  };
}

// ---------------- main shell ----------------
async function boot() {
  try { me = me || await api('/api/me/user'); }
  catch { return renderLogin(); }
  await loadDevboxes();
  renderShell();
}

async function loadDevboxes(){ devboxes = await api('/api/devboxes'); }

function renderShell() {
  app.innerHTML = `
  <header>
    <b>deepbox</b>
    <span class="muted">${me.display_name}</span>
    <span style="flex:1"></span>
    <button class="ghost" id="newbox">+ Devbox</button>
    ${me.role==='owner'?'<button class="ghost" id="owner">Owner</button>':''}
    <button class="ghost" id="logout">Logout</button>
  </header>
  <main>
    <div class="side" id="side"></div>
    <div class="content">
      <div id="termhead" class="row" style="padding:8px 12px;border-bottom:1px solid var(--border)">
        <span class="muted">Select an agent to open a terminal</span></div>
      <div id="term"></div>
    </div>
  </main>`;
  logout.onclick = async()=>{ await api('/api/auth/logout',{method:'POST'}); me=null; renderLogin(); };
  newbox.onclick = createDevbox;
  if(me.role==='owner') document.getElementById('owner').onclick = renderOwner;
  renderSide();
  setupTerm();
}

// ---------------- owner admin ----------------
async function renderOwner(){
  let invites=[], users=[];
  try { [invites, users] = await Promise.all([
    api('/api/invitations'), api('/api/users')]); } catch(e){}
  app.innerHTML = `
  <header>
    <b>deepbox — owner</b>
    <span style="flex:1"></span>
    <button class="ghost" id="back">← Back</button>
  </header>
  <main style="display:block;padding:16px;overflow:auto">
    <div class="card">
      <h4>Invitations</h4>
      <div class="row">
        <input id="inote" placeholder="note (optional)"/>
        <input id="ittl" type="number" value="24" title="TTL hours" style="width:120px"/>
        <button id="mint">Mint invite</button>
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
      <div class="row" style="border-top:1px solid var(--border);padding:4px 0">
        <span>${i.note?escapeHtml(i.note):'<span class="muted">(no note)</span>'}</span>
        <span class="muted">${i.status}, expires ${i.expires_at}</span>
        <span style="flex:1"></span>
        ${i.status==='active'?`<button class="ghost" data-revoke="${i.id}">revoke</button>`:''}
      </div>`).join('') || '<div class="muted">No invitations.</div>';
    document.querySelectorAll('[data-revoke]').forEach(b=>b.onclick=async()=>{
      await api(`/api/invitations/${b.dataset.revoke}`,{method:'DELETE'});
      invites = await api('/api/invitations'); renderInv();
    });
  };
  const renderUsers = () => {
    document.getElementById('userlist').innerHTML = users.map(u=>`
      <div class="row" style="border-top:1px solid var(--border);padding:4px 0">
        <b>${escapeHtml(u.display_name)}</b>
        <span class="muted">@${escapeHtml(u.username)} · ${u.role}${u.disabled?' · disabled':''}</span>
        <span style="flex:1"></span>
        ${u.role==='member'?(u.disabled
          ?`<button class="ghost" data-enable="${u.id}">enable</button>`
          :`<button class="ghost" data-disable="${u.id}">disable</button>`):''}
      </div>`).join('') || '<div class="muted">No users.</div>';
    document.querySelectorAll('[data-disable]').forEach(b=>b.onclick=async()=>{
      try{ await api(`/api/users/${b.dataset.disable}/disable`,{method:'POST'}); }
      catch(e){ alert(e.message); }
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
      note:inote.value||undefined, ttl_hours:Number(ittl.value)||24})});
    // Show plaintext + prefilled invite URL exactly once; not retained.
    // URL fragments never reach the HTTP server or its access logs.
    const url = `${location.origin}${location.pathname}#invite=${encodeURIComponent(res.token)}`;
    document.getElementById('mintout').innerHTML =
      `<div class="token">Invite code (shown once): ${escapeHtml(res.token)}<br><br>`+
      `Invite URL: <a href="${url}">${escapeHtml(url)}</a></div>`;
    invites = await api('/api/invitations'); renderInv();
  };
}

function escapeHtml(s){ return String(s).replace(/[&<>"']/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function renderSide() {
  const side = document.getElementById('side');
  side.innerHTML = devboxes.map(d => `
    <div class="card">
      <div class="row">
        <span class="dot ${d.online?'on':'off'}"></span>
        <b>${d.name}</b>
        <span style="flex:1"></span>
        <button class="ghost" data-agent="${d.id}">+agent</button>
      </div>
      <div class="muted">caps: ${(d.capabilities||[]).join(', ')||'—'}</div>
      <div style="margin-top:6px">
        ${d.agents.map(a=>`<div class="agent" data-open="${a.id}" data-name="${a.display_name}">
          <span class="dot ${a.presence==='online'?'on':'off'}"></span>
          @${a.handle} <span class="muted">(${a.runtime})</span></div>`).join('') || '<div class="muted">no agents</div>'}
      </div>
      <div class="row" style="margin-top:6px">
        <button class="ghost" data-token="${d.id}">rotate token</button>
        <button class="ghost" data-del="${d.id}">delete</button>
      </div>
    </div>`).join('') || '<div class="muted">No devboxes yet. Create one →</div>';

  side.querySelectorAll('[data-agent]').forEach(b=>b.onclick=()=>createAgent(b.dataset.agent));
  side.querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>openAgent(b.dataset.open, b.dataset.name));
  side.querySelectorAll('[data-token]').forEach(b=>b.onclick=()=>rotateToken(b.dataset.token));
  side.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>delDevbox(b.dataset.del));
}

async function createDevbox() {
  const name = prompt('Devbox name?', 'My Devbox'); if(!name) return;
  const res = await api('/api/devboxes',{method:'POST',body:JSON.stringify({name})});
  await loadDevboxes(); renderSide();
  showToken(res.token);
}
function showToken(tok){
  alert('Copy this token now — it is shown only once:\n\n'+tok+
    '\n\nRun the connector with:\nset DEEPBOX_TOKEN='+tok+'\npython -m connector');
}
async function rotateToken(id){
  const res = await api(`/api/devboxes/${id}/tokens`,{method:'POST'});
  showToken(res.token);
}
async function delDevbox(id){
  if(!confirm('Delete this devbox and its agents?')) return;
  await api(`/api/devboxes/${id}`,{method:'DELETE'});
  await loadDevboxes(); renderSide();
}
async function createAgent(devboxId){
  const handle = prompt('Agent handle? (e.g. claude)'); if(!handle) return;
  const runtime = prompt('Runtime? mock | claude-code | copilot-cli | codex-cli','mock')||'mock';
  const cwd = prompt('Working dir? (optional, blank = default)','')||null;
  await api(`/api/devboxes/${devboxId}/agents`,{method:'POST',
    body:JSON.stringify({handle,display_name:handle,runtime,cwd})});
  await loadDevboxes(); renderSide();
}

// ---------------- terminal ----------------
let reconnectDelay = 500, reconnectTimer = null, wantOpen = false;

function setupTerm(){
  term = new Terminal({fontFamily:'Consolas,monospace',fontSize:13,cursorBlink:true,
    scrollback:5000, theme:{background:'#000000'}});
  fit = new FitAddon.FitAddon(); term.loadAddon(fit);
  term.open(document.getElementById('term'));
  fit.fit();
  window.onresize = ()=>{ try{fit.fit(); sendResize();}catch(e){} };
  term.onData(d => { if(termWS && termWS.readyState===1 && curSession)
    termWS.send(JSON.stringify({type:'input',session_id:curSession,data:d})); });
}
function sendResize(){
  if(termWS && termWS.readyState===1 && curSession) termWS.send(JSON.stringify(
    {type:'resize',session_id:curSession,cols:term.cols,rows:term.rows}));
}

async function openAgent(agentId, name){
  // leaving previous session? detach it (do NOT kill its PTY)
  if(termWS && termWS.readyState===1 && curSession)
    termWS.send(JSON.stringify({type:'detach',session_id:curSession}));
  document.getElementById('termhead').innerHTML =
    `<b>${name}</b> <span class="muted">— live terminal</span>
     <span style="flex:1"></span>
     <span id="stat" class="muted"></span>`;
  term.reset();
  // Resume the newest PTY that is still alive on the devbox. Previously every
  // click silently created a new session, making persisted history invisible.
  const sessions = await api(`/api/agents/${agentId}/sessions`);
  let sess = sessions.find(s => s.state === 'live');
  const resumed = !!sess;
  if(!sess) sess = await api(`/api/agents/${agentId}/sessions`,{method:'POST'});
  curSession = sess.id;
  if(resumed) document.getElementById('termhead').querySelector('.muted').textContent =
    '— resumed live session';
  wantOpen = true;
  connectTermWS();
}

function setStat(txt, color){
  const el = document.getElementById('stat');
  if(el){ el.textContent = txt; el.style.color = color||'#8b949e'; }
}

function connectTermWS(){
  if(termWS){ try{ wantOpen && (termWS.onclose=null); termWS.close(); }catch(e){} }
  const proto = location.protocol==='https:'?'wss':'ws';
  termWS = new WebSocket(`${proto}://${location.host}/ws/term`);
  termWS.onopen = ()=>{
    reconnectDelay = 500;
    setStat('● live', '#3fb950');
    termWS.send(JSON.stringify({type:'attach',session_id:curSession,
      cols:term.cols,rows:term.rows}));
  };
  termWS.onmessage = (ev)=>{
    const f = JSON.parse(ev.data);
    if(f.session_id && f.session_id!==curSession) return;
    switch(f.type){
      case 'restore':          // reconnect: instantly repaint current screen
        term.reset(); term.write(f.data); break;
      case 'output':
        term.write(f.data); break;
      case 'status':
        if(f.state==='live') setStat('● live','#3fb950');
        else if(f.state==='offline'){ setStat('● devbox offline','#d29922');
          term.write('\r\n[devbox offline — the connector isn\'t running]\r\n'); }
        else if(f.state==='ended'){ setStat('● ended','#8b949e');
          term.write(`\r\n[session ended, code ${f.code}]\r\n`); }
        break;
      case 'exit':
        setStat('● ended','#8b949e');
        if(f.data) term.write(f.data);
        term.write(`\r\n[session ended, code ${f.code}]\r\n`); break;
      case 'error':
        term.write(`\r\n[error] ${f.message}\r\n`); break;
    }
  };
  termWS.onclose = ()=>{
    if(!wantOpen) return;
    setStat('● reconnecting…','#d29922');
    reconnectTimer = setTimeout(connectTermWS, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay*2, 5000);  // exponential backoff
  };
}

// ---------------- start ----------------
boot();
