const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// A small DOM subset for app orchestration, not a browser implementation. Nodes
// really detach when replaced, so replay/modal lifetime regressions are visible.
class Element {
  constructor(document, tag = 'div') {
    this.ownerDocument = document;
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    this.listeners = new Map();
    this.scrollTop = 0;
    this.scrollHeight = 100;
    this.clientHeight = 100;
    this.classList = {
      contains: name => this.className.split(/\s+/).includes(name),
      add: name => { this.className = [...new Set([...this.className.split(/\s+/), name])].join(' ').trim(); },
      remove: name => { this.className = this.className.split(/\s+/).filter(item => item !== name).join(' '); },
      toggle: (name, value) => { (value ?? !this.classList.contains(name)) ? this.classList.add(name) : this.classList.remove(name); },
    };
  }
  get id() { return this.attributes.id || ''; }
  set id(value) { this.attributes.id = value; }
  get className() { return this.attributes.class || ''; }
  set className(value) { this.attributes.class = value; }
  get isConnected() { return this === this.ownerDocument.body || !!this.parentNode?.isConnected; }
  get firstChild() { return this.children[0] || null; }
  get firstElementChild() { return this.children.find(child => child.tagName !== '#TEXT') || null; }
  get textContent() { return (this._text || '') + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  get innerHTML() { return this._html || ''; }
  set innerHTML(html) {
    this.replaceChildren();
    this._text = '';
    this._html = String(html);
    const stack = [this];
    for (const token of this._html.matchAll(/<\/?([\w-]+)\b([^>]*?)>|([^<]+)/g)) {
      if (token[3]) {
        const text = new Element(this.ownerDocument, '#text');
        text._text = token[3]; stack.at(-1).appendChild(text); continue;
      }
      const tag = token[1].toLowerCase();
      if (token[0].startsWith('</')) {
        const index = stack.findLastIndex(node => node.tagName.toLowerCase() === tag);
        if (index > 0) stack.length = index;
        continue;
      }
      const node = new Element(this.ownerDocument, tag);
      for (const attr of token[2].matchAll(/([\w:-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\s/>]+)))?/g)) {
        node.setAttribute(attr[1], attr[2] ?? attr[3] ?? attr[4] ?? '');
      }
      stack.at(-1).appendChild(node);
      if (!['input', 'img', 'br', 'hr', 'meta', 'link'].includes(tag) && !token[0].endsWith('/>')) stack.push(node);
    }
  }
  get value() {
    if (this._value !== undefined) return this._value;
    if (this.tagName === 'SELECT') {
      const options = this.querySelectorAll('option');
      return (options.find(option => 'selected' in option.attributes) || options[0])?.value || '';
    }
    return this.attributes.value || '';
  }
  set value(value) { this._value = String(value); }
  setAttribute(key, value) {
    this.attributes[key] = String(value);
    if (key.startsWith('data-')) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value);
    if (key === 'disabled' || key === 'hidden') this[key] = true;
  }
  getAttribute(key) { return this.attributes[key] ?? null; }
  appendChild(node) { node.remove(); node.parentNode = this; this.children.push(node); return node; }
  append(...nodes) { for (const node of nodes) this.appendChild(node); }
  insertBefore(node, reference) {
    if (!reference) return this.appendChild(node);
    const index = this.children.indexOf(reference);
    assert.notEqual(index, -1, 'insertBefore reference must still belong to its parent');
    node.remove(); node.parentNode = this; this.children.splice(index, 0, node); return node;
  }
  replaceChildren(...nodes) { for (const child of this.children) child.parentNode = null; this.children = []; this.append(...nodes); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this); this.parentNode = null; }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
  matches(selector) {
    const attr = selector.match(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/);
    if (attr && (!(attr[1] in this.attributes) || attr[2] !== undefined && this.attributes[attr[1]] !== attr[2])) return false;
    const simple = selector.replace(/\[[^\]]*\]/g, '');
    const tag = simple.match(/^[\w-]+/), id = simple.match(/#([\w-]+)/), cls = simple.match(/\.([\w-]+)/);
    return (!tag || this.tagName.toLowerCase() === tag[0]) && (!id || this.id === id[1]) && (!cls || this.classList.contains(cls[1]));
  }
  querySelectorAll(selector) {
    const result = [];
    const selectors = selector.split(',').map(value => value.trim().split(/\s+/));
    const visit = node => {
      for (const child of node.children) {
        if (selectors.some(parts => {
          if (!child.matches(parts.at(-1))) return false;
          let ancestor = child.parentNode;
          for (let index = parts.length - 2; index >= 0; --index) {
            while (ancestor && !ancestor.matches(parts[index])) ancestor = ancestor.parentNode;
            if (!ancestor) return false;
            ancestor = ancestor.parentNode;
          }
          return true;
        })) result.push(child);
        visit(child);
      }
    };
    visit(this); return result;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(type, callback) { if (!this.listeners.has(type)) this.listeners.set(type, new Set()); this.listeners.get(type).add(callback); }
  removeEventListener(type, callback) { this.listeners.get(type)?.delete(callback); }
  focus() { this.ownerDocument.activeElement = this; }
}

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}

test('hidden controls stay hidden even when their class sets display:flex', () => {
  const css = fs.readFileSync(path.join(__dirname, 'styles.css'), 'utf8');
  assert.match(css, /\[hidden\]\s*\{\s*display\s*:\s*none\s*!important\s*;?\s*\}/);
});

function harness({terminal = true} = {}) {
  const document = {};
  document.body = new Element(document, 'body');
  document.createElement = tag => new Element(document, tag);
  document.createTextNode = text => { const node = document.createElement('#text'); node.textContent = text; return node; };
  document.querySelector = selector => document.body.querySelector(selector);
  document.querySelectorAll = selector => document.body.querySelectorAll(selector);
  document.getElementById = id => document.querySelector('#' + id);
  document.addEventListener = document.body.addEventListener.bind(document.body);
  document.removeEventListener = document.body.removeEventListener.bind(document.body);
  document.body.innerHTML = '<div id="app"><div id="termhead"></div><div id="stagebody"></div></div>';
  const sockets = [], terminals = [], timers = new Map(), requests = [];
  class Socket {
    constructor(url) { this.url = url; this.readyState = 0; this.frames = []; sockets.push(this); }
    open() { this.readyState = 1; this.onopen?.(); }
    receive(frame) { this.onmessage?.({data: JSON.stringify(frame)}); }
    send(raw) { if (this.fail || this.readyState !== 1) throw new Error('socket unavailable'); this.frames.push(JSON.parse(raw)); }
    close() { this.readyState = 3; this.onclose?.(); }
  }
  class Terminal {
    constructor(options) { this.options = options; this.cols = 120; this.rows = 30; this.output = ''; terminals.push(this); }
    loadAddon(addon) { addon.terminal = this; }
    open(host) { this.host = host; }
    onData(callback) { this.data = callback; return {dispose() {}}; }
    write(data, callback) { this.output += data; callback?.(); }
    reset() { this.output = ''; }
    resize(cols, rows) { this.cols = cols; this.rows = rows; }
    focus() { this.focused = true; }
    scrollToBottom() {}
    dispose() { this.disposed = true; }
  }
  const storage = {getItem() { return null; }, setItem() {}, removeItem() {}};
  let timerId = 0;
  const context = vm.createContext({
    document, console, URL, URLSearchParams, TextEncoder, TextDecoder,
    location: {hash: '', search: '', pathname: '/', protocol: 'http:', host: '127.0.0.1:18789'},
    history: {replaceState() {}}, navigator: {platform: 'Win32'}, localStorage: storage, sessionStorage: storage,
    WebSocket: Socket, Terminal: terminal ? Terminal : undefined, FitAddon: {FitAddon: class { fit() {} }},
    setTimeout: fn => { timers.set(++timerId, fn); return timerId; }, clearTimeout: id => timers.delete(id),
    setInterval: fn => { timers.set(++timerId, fn); return timerId; }, clearInterval: id => timers.delete(id),
    requestAnimationFrame: fn => { timers.set(++timerId, fn); return timerId; },
    addEventListener() {}, removeEventListener() {},
    // Leave the real boot() awaiting authentication while tests drive real app
    // functions. No source rewriting, external calls, timers or live credentials.
    fetch: () => new Promise(() => {}),
  });
  context.window = context;
  for (const name of ['ui.js', 'chat.js', 'collaboration.js', 'replay.js', 'app.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, name), 'utf8'), context, {filename: name});
  }
  const fixture = {
    workspaces: [{id: 'w', name: 'work', role: 'operator'}],
    devboxes: [{id: 'box', workspace_id: 'w', name: 'machine', online: true,
      capabilities: {runtimes: [{schema_version: 2, runtime: 'claude-code', available: true, installation: {status: 'installed'},
        surfaces: [{id: 'structured', default: true, features: {structured: true}}, {id: 'terminal', features: {}}]}]},
      agents: [{id: 'a', runtime: 'claude-code', handle: 'claude', display_name: 'Claude'}]}],
    sessions: [{id: 'chat', agent_id: 'a', surface: 'structured', state: 'live'}],
  };
  let responder = null;
  context.fetch = async (url, options = {}) => {
    if (url === '/api/me/user') return new Promise(() => {});
    requests.push({url, ...options});
    let data = responder ? await responder(url, options) : undefined;
    if (data === undefined) {
      if (url === '/api/workspaces') data = fixture.workspaces;
      else if (url === '/api/devboxes') data = fixture.devboxes;
      else if (/\/agents\/[^/]+\/sessions$/.test(url)) data = options.method === 'POST'
        ? {id: `new-${requests.length}`, agent_id: 'a', surface: JSON.parse(options.body).surface}
        : fixture.sessions;
      else throw new Error('Unexpected test request: ' + url);
    }
    return {ok: true, status: 200, json: async () => data, text: async () => JSON.stringify(data)};
  };
  const run = code => vm.runInContext(code, context);
  const call = (name, ...args) => { context.testArgs = args; return run(`${name}(...testArgs)`); };
  run('me = {id:2, username:"member", role:"member"}');
  return {document, context, run, call, sockets, terminals, timers, requests, fixture,
    respond: fn => { responder = fn; }, get: id => document.getElementById(id)};
}

function collaborate(socket, session, role = 'operator', holder = false) {
  socket.receive({type: 'collaboration', session_id: session, surface: holder ? 'terminal' : 'structured',
    role, keyboard: {holder_user_id: holder ? 2 : 99, holder_username: 'other', is_holder: holder}});
}

test('explicit Terminal creates a terminal instead of reusing the existing Chat', async () => {
  const h = harness();
  await h.call('openAgent', 'a', 'Claude', 'terminal');
  assert.equal(h.run('currentSurface'), 'terminal');
  assert.equal(h.run('structuredMode'), false);
  const created = h.requests.find(request => request.method === 'POST');
  assert.deepEqual(JSON.parse(created.body), {surface: 'terminal'});
  const socket = h.sockets.at(-1); socket.open();
  assert.equal(socket.frames[0].surface, 'terminal');
  socket.receive({type: 'ready', session_id: h.run('curSession'), surface: 'terminal', capabilities: {features: {structured: true}}});
  assert.equal(h.run('structuredMode'), false, 'terminal ready overrides agent-level structured capability');
  collaborate(socket, h.run('curSession'), 'operator', true);
  h.terminals.at(-1).data('terminal input');
  assert.equal(socket.frames.at(-1).data, 'terminal input');
  await h.get('session-new').onclick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(JSON.parse(h.requests.filter(request => request.method === 'POST').at(-1).body).surface, 'terminal');
});

test('Chat opens without xterm and shared operators send without taking a keyboard', async () => {
  const h = harness({terminal: false});
  await h.call('openAgent', 'a', 'Claude');
  assert.equal(h.run('curSession'), 'chat');
  assert.equal(h.terminals.length, 0);
  const socket = h.sockets.at(-1); socket.open();
  collaborate(socket, 'chat');
  assert.equal(h.get('chat-input').disabled, false);
  assert.equal(h.get('collab-request'), null);
  h.get('chat-input').value = 'message from collaborator';
  h.call('sendChatMessage');
  assert.equal(socket.frames.at(-1).data, 'message from collaborator');
  assert.equal(h.get('chat-input').value, '');
  assert.equal(socket.frames.some(frame => frame.type.startsWith('keyboard_')), false);
  assert.equal(h.run('keyboardHeartbeat'), null);
});

test('viewers cannot send and failed sends preserve the draft', async () => {
  const h = harness(); await h.call('openAgent', 'a', 'Claude');
  const socket = h.sockets.at(-1); socket.open();
  collaborate(socket, 'chat', 'viewer');
  h.get('chat-input').value = 'keep my draft';
  h.call('sendChatMessage');
  assert.equal(socket.frames.length, 1);
  assert.equal(h.get('chat-input').value, 'keep my draft');
  assert.match(h.get('chat-composer-error').textContent, /read-only/i);
  collaborate(socket, 'chat'); socket.fail = true;
  h.call('sendChatMessage');
  assert.equal(h.get('chat-input').value, 'keep my draft');
  assert.match(h.get('chat-composer-error').textContent, /socket unavailable/i);
});

test('a collaborator can start a new chat without terminating the shared one', async () => {
  const h = harness(); await h.call('openAgent', 'a', 'Claude');
  const socket = h.sockets.at(-1); socket.open(); collaborate(socket, 'chat');
  assert.equal(h.get('session-end').disabled, true);
  await h.call('startNewChat', h.get('chat-new'));
  assert.notEqual(h.run('curSession'), 'chat');
  assert.equal(h.run('currentSurface'), 'structured');
  assert.equal(socket.frames.some(frame => frame.type === 'terminate'), false);
});

test('ending a session requires the holder or an admin and explicit confirmation', async () => {
  const h = harness(); await h.call('openAgent', 'a', 'Claude');
  const socket = h.sockets.at(-1); socket.open(); collaborate(socket, 'chat');
  h.run('showConfirm = async()=>true');
  await h.call('endCurrentSession');
  assert.equal(socket.frames.some(frame => frame.type === 'terminate'), false);
  collaborate(socket, 'chat', 'owner');
  assert.equal(h.get('session-end').disabled, false);
  h.run('showConfirm = async()=>false');
  await h.call('endCurrentSession');
  assert.equal(socket.frames.some(frame => frame.type === 'terminate'), false);
  h.run('showConfirm = async()=>true');
  await h.call('endCurrentSession');
  assert.equal(socket.frames.at(-1).type, 'terminate');
});

test('late REST results and old sockets cannot replace the current session', async () => {
  const h = harness(), waiting = deferred();
  h.respond((url, options) => url === '/api/agents/a/sessions' && !options.method ? waiting.promise : undefined);
  const first = h.call('openAgent', 'a', 'Claude', 'terminal');
  await new Promise(resolve => setImmediate(resolve));
  await h.call('openLiveSession', {id: 'chosen', agent_id: 'a', surface: 'structured'}, 'Chosen');
  waiting.resolve([{id: 'stale', agent_id: 'a', state: 'live', surface: 'terminal'}]);
  await first;
  assert.equal(h.run('curSession'), 'chosen');
  assert.equal(h.sockets.length, 1);
  const old = h.sockets[0]; old.open();
  h.call('connectTermWS');
  old.receive({type: 'exit', session_id: 'chosen', code: 0});
  assert.equal(h.run('wantOpen'), true);
  assert.notEqual(h.run('termWS'), old);
});

test('terminal replay retains its controls through terminal initialization and seeks', async () => {
  const h = harness();
  h.fixture.workspaces[0].role = 'owner'; await h.call('loadDevboxes');
  h.respond(url => url.endsWith('/replay') ? {
    header: {width: 120, height: 30}, surface: 'terminal', retention: '30d',
    events: [{time: 0, kind: 'output', data: 'recorded output'}], checkpoints: [],
  } : undefined);
  await h.call('startReplay', 'old', {id: 'old', agent_id: 'a', surface: 'terminal'});
  assert.ok(h.get('rp-retention'));
  assert.ok(h.get('btnDeleteReplay'));
  assert.equal(h.get('rp-retention').value, '30d');
  h.call('replaySeek', 0);
  assert.ok(h.get('replaybar').isConnected);
  assert.equal(h.terminals.at(-1).options.disableStdin, true);
});

test('structured replay is a read-only transcript and never needs xterm', async () => {
  const h = harness({terminal: false});
  h.respond(url => url.endsWith('/replay') ? {surface: 'structured', events: [
    {time: 0, kind: 'event', data: JSON.stringify({ev: 'message', role: 'assistant', text: 'saved answer'})},
  ], checkpoints: []} : undefined);
  await h.call('startReplay', 'old', {id: 'old', agent_id: 'a', surface: 'structured'});
  assert.equal(h.terminals.length, 0);
  assert.equal(h.get('chat-form').hidden, true);
  assert.equal(h.get('chat-input').disabled, true);
  assert.ok(h.get('rp-retention'));
  assert.equal(h.get('rp-retention').disabled, true);
  assert.match(h.get('chat-scroll').textContent, /saved answer/);
});

test('replacing a dialog resolves cancellation and leaves only the current Escape handler', async () => {
  const h = harness();
  const count = () => h.document.body.listeners.get('keydown')?.size || 0;
  const initial = count();
  const first = h.call('showConfirm', 'First', 'first question');
  assert.equal(count(), initial + 1);
  const second = h.call('showForm', {title: 'Second', fields: [{name: 'name', label: 'Name', value: 'kept'}]});
  assert.equal(await first, null);
  assert.equal(count(), initial + 1);
  h.call('closeOverlay');
  assert.equal(await second, null);
  assert.equal(count(), initial);
  assert.equal(h.get('overlay'), null);
});

test('agent creation uses the actual capability report object', async () => {
  const h = harness(); await h.call('loadDevboxes');
  const pending = h.call('createAgent', 'box');
  await new Promise(resolve => setImmediate(resolve));
  const runtime = h.document.querySelector('[data-field="runtime"]');
  assert.ok(runtime, 'available runtimes should open the agent form, not an unavailable alert');
  assert.equal(runtime.value, 'claude-code');
  h.call('closeOverlay'); await pending;
});

test('new invitations select Operator, but an existing Viewer changes only after Save', async () => {
  const h = harness(); h.fixture.workspaces[0].role = 'owner'; await h.call('loadDevboxes');
  h.respond((url, options) => {
    if (url.endsWith('/members')) return [{user_id: 'colleague', username: 'colleague', role: 'viewer'}];
    if (url.endsWith('/invitations')) return [];
    if (url.endsWith('/members/colleague') && options.method === 'PATCH') return {user_id: 'colleague', role: 'operator'};
  });
  await h.call('openWorkspaceManager', 'w');
  const role = h.document.querySelector('[data-member-role]'), save = h.document.querySelector('[data-save-member]');
  assert.equal(h.get('workspace-invite-role').value, 'operator');
  assert.equal(role.value, 'viewer');
  assert.equal(save.disabled, true);
  assert.equal(h.requests.some(request => request.method === 'PATCH'), false);
  role.value = 'operator'; role.onchange();
  assert.equal(h.requests.some(request => request.method === 'PATCH'), false);
  await save.onclick();
  const request = h.requests.find(request => request.method === 'PATCH');
  assert.equal(request.url, '/api/workspaces/w/members/colleague');
  assert.deepEqual(JSON.parse(request.body), {role: 'operator'});
  assert.equal(role.dataset.memberRole, 'operator');
  assert.equal(save.disabled, true);
});

test('a failed role change keeps the old grant and a retryable selection', async () => {
  const h = harness(); h.fixture.workspaces[0].role = 'owner'; await h.call('loadDevboxes');
  h.respond((url, options) => {
    if (url.endsWith('/members')) return [{user_id: 'colleague', username: 'colleague', role: 'viewer'}];
    if (url.endsWith('/invitations')) return [];
    if (options.method === 'PATCH') throw new Error('role change rejected');
  });
  await h.call('openWorkspaceManager', 'w');
  const role = h.document.querySelector('[data-member-role]'), save = h.document.querySelector('[data-save-member]');
  role.value = 'operator'; role.onchange(); await save.onclick();
  assert.equal(role.dataset.memberRole, 'viewer');
  assert.equal(role.value, 'operator');
  assert.equal(role.disabled, false);
  assert.equal(save.disabled, false);
  assert.equal(h.get('workspace-member-error').textContent, 'role change rejected');
});

test('missing xterm produces an actionable error instead of a blank Terminal', async () => {
  const h = harness({terminal: false});
  await h.call('openAgent', 'a', 'Claude', 'terminal');
  assert.equal(h.sockets.length, 0);
  assert.match(h.get('stagebody').textContent, /xterm.*could not load/i);
  assert.equal(h.requests.some(request => request.method === 'POST'), false);
});
