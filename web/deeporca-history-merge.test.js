// Compatibility coverage for native DeepOrca panes and generic history actions.
const test = require('node:test');
const assert = require('node:assert/strict');
const { createBrowser, deferred } = require('./test-dom.js');

const ui = (root, name) => root.querySelector('[data-ui="' + name + '"]');
const flush = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };
const inputs = socket => socket.frames.filter(frame => frame.type === 'input');
const submit = root => ui(root, 'chat-form').dispatchEvent({ type: 'submit' });
const nativeSession = (id = 'native-session', extra = {}) => ({
  id, agent_id: 'native', state: 'inactive', surface: 'structured', available: true,
  renderer: 'deeporca-chat-v1', title: 'Native conversation', can_rename: true, can_resume: true, ...extra,
});
const recorded = session => ({ session, renderer: session.renderer, surface: session.surface, events: [
  { time: 1, kind: 'event', data: { ev: 'message', role: 'assistant', text: 'Native recorded answer' } },
] });

function harness({ role = 'operator', rendererOnly = false } = {}) {
  const browser = createBrowser();
  const Pane = browser.loadModule('./pane.js');
  const workspace = { id: 'workspace', role };
  const sessions = new Map(), recordings = new Map(), requests = [];
  const runtime = native => ({ schema_version: 2, runtime: native ? 'deeporca' : 'claude',
    default_surface: 'structured', surfaces: [{ id: 'structured', available: true, attachable: true,
      features: { structured: true, ...(native ? { renderer: 'deeporca-chat-v1' } : {}),
        controls: [{ key: 'permission_mode', kind: 'select', label: 'Permission mode', choices: ['default', 'allow'] }] } }] });
  const h = { ...browser, workspace, sessions, recordings, requests };
  h.create = id => {
    const root = browser.document.createElement('article'); browser.document.body.appendChild(root);
    const pane = Pane.createPane({ id, root, services: {
      getWorkspace: () => workspace, getUser: () => ({ id: 'user' }),
      findAgent: agentId => { const native = agentId === 'native' && !rendererOnly; return {
        agent: { id: agentId, runtime: native ? 'deeporca' : 'claude' },
        box: { online: true, capabilities: [runtime(native)] },
      }; },
      confirm: async () => true,
      api: async (path, options = {}) => {
        const method = options.method || 'GET'; requests.push({ path, method, body: options.body });
        if (h.apiOverride) { const value = h.apiOverride(path, options); if (value !== undefined) return value; }
        const list = path.match(/\/agents\/([^/]+)\/sessions$/);
        if (list) { assert.equal(method, 'GET', 'history must not create a replacement session'); return sessions.get(list[1]) || []; }
        const match = path.match(/\/sessions\/([^/]+)(\/replay)?$/);
        if (match) {
          const session = [...sessions.values()].flat().find(item => item.id === match[1]);
          if (match[2]) return recordings.get(match[1]) || recorded(session);
          if (method === 'PATCH') { session.title = JSON.parse(options.body).title; return { ...session }; }
          return session;
        }
        throw new Error('Unexpected API request: ' + path);
      },
    } });
    return { pane, root };
  };
  h.attach = async (view, session) => {
    sessions.set(session.agent_id, [session]);
    await view.pane.open({ kind: 'live', agentId: session.agent_id, sessionId: session.id, surface: session.surface });
    const socket = browser.sockets.at(-1); socket.open();
    socket.receive({ type: 'ready', session_id: session.id, launch_id: session.launch_id });
    socket.receive({ type: 'collaboration', session_id: session.id, role: workspace.role, participants: [], keyboard: {} });
    await flush(); return socket;
  };
  return h;
}

test('DeepOrca history combines rename with explicit continuation, never generic native adoption', async () => {
  const h = harness({ rendererOnly: true });
  const session = nativeSession('native-session', { resume_supported: true, native_context_state: 'available', launch_id: 'native-launch' });
  h.sessions.set('native', [session]);
  const a = h.create('history'), b = h.create('replay');
  await a.pane.open({ kind: 'history', agentId: 'native' });
  await b.pane.open({ kind: 'replay', agentId: 'native', sessionId: session.id });
  await flush();
  assert.equal(h.sockets.length, 0);
  assert.ok(b.root.querySelector('.deeporca-chat'), 'persisted renderer works with stale capability metadata');
  assert.equal(ui(a.root, 'session-resume').disabled, true);
  assert.match(ui(a.root, 'session-action-reason').textContent, /generic native resume is unavailable/);
  ui(a.root, 'session-rename').click();
  ui(a.root, 'session-rename-input').value = 'Renamed native conversation';
  ui(a.root, 'session-rename-form').dispatchEvent({ type: 'submit' }); await flush();
  assert.equal(ui(b.root, 'session-title').textContent, 'Renamed native conversation');
  assert.equal(b.pane.getState().title, 'Renamed native conversation');
  ui(a.root, 'session-resume').click(); await flush();
  assert.equal(h.sockets.length, 0);
  // Continuation must use fresh lifecycle metadata, not the rendered history card.
  session.launch_id = 'fresh-native-launch';
  ui(a.root, 'history-continue-native').click(); await flush();
  assert.equal(h.sockets.length, 1);
  const socket = h.sockets[0]; socket.open(); socket.open();
  assert.deepEqual(socket.frames.map(frame => frame.type), ['resume']);
  assert.equal(socket.frames[0].launch_id, 'fresh-native-launch');
  assert.equal(socket.frames[0].session_id, session.id);
  socket.receive({ type: 'collaboration', session_id: session.id, role: 'operator', keyboard: {}, participants: [] });
  assert.equal(ui(a.root, 'chat-input').disabled, true);
  socket.receive({ type: 'ready', session_id: session.id, launch_id: 'resumed-native-launch' });
  assert.equal(ui(a.root, 'chat-input').disabled, true, 'ready alone cannot advance the launch generation');
  socket.receive({ type: 'status', session_id: session.id, state: 'starting', launch_id: 'resumed-native-launch' });
  assert.equal(a.pane.getState().readOnly, true);
  socket.receive({ type: 'ready', session_id: session.id, launch_id: 'fresh-native-launch' });
  assert.equal(ui(a.root, 'chat-input').disabled, true, 'the prior generation cannot make the new launch writable');
  socket.receive({ type: 'ready', session_id: session.id, launch_id: 'resumed-native-launch' });
  assert.equal(ui(a.root, 'chat-input').disabled, false);
  ui(a.root, 'chat-input').value = 'Continue the native context'; submit(a.root);
  assert.equal(inputs(socket).length, 1);
  assert.equal(inputs(socket)[0].launch_id, 'resumed-native-launch');
  a.pane.reconnect(); h.sockets.at(-1).open();
  assert.equal(h.sockets.at(-1).frames[0].type, 'attach', 'reconnect never repeats continuation consent');
  assert.equal(h.sockets.flatMap(socket => socket.frames).filter(frame => frame.type === 'resume').length, 1);
  assert.equal(h.requests.filter(request => request.method === 'POST').length, 0);
  assert.ok(!JSON.stringify(a.pane.snapshot()).includes('continueNative'));
  a.pane.close(); b.pane.close();
});

test('saved native continuation and generic resume intent cannot activate a restored historical pane', async () => {
  const h = harness(); h.sessions.set('native', [nativeSession()]);
  const view = h.create('restore');
  await view.pane.open({ kind: 'live', agentId: 'native', sessionId: 'native-session', surface: 'structured',
    restore: true, continueNative: true, resume: true });
  assert.equal(view.pane.getState().kind, 'replay');
  assert.equal(h.sockets.length, 0);
  assert.equal(h.requests.filter(request => request.method !== 'GET').length, 0);
  view.pane.close();
});

test('fresh DeepOrca metadata gates a generic history Resume shown for a stale ordinary runtime', async () => {
  const h = harness({ rendererOnly: true }), view = h.create('stale');
  const session = nativeSession('native-session', { renderer: null, resume_supported: true, native_context_state: 'available' });
  h.sessions.set('native', [session]);
  await view.pane.open({ kind: 'history', agentId: 'native' });
  assert.equal(ui(view.root, 'session-resume').disabled, false);
  session.renderer = 'deeporca-chat-v1';
  ui(view.root, 'session-resume').click(); await flush();
  assert.equal(h.sockets.length, 0);
  assert.match(ui(view.root, 'session-action-error').textContent, /generic native resume is unavailable/);
  assert.equal(view.pane.getState().kind, 'history');
  view.pane.close();
});

test('native continuation rechecks authorization during preflight and again when its socket opens', async () => {
  const h = harness(), view = h.create('authorization');
  h.sessions.set('native', [nativeSession()]);
  await view.pane.open({ kind: 'history', agentId: 'native' });
  const pending = deferred(); h.apiOverride = path => path.endsWith('/agents/native/sessions') ? pending.promise : undefined;
  ui(view.root, 'history-continue-native').click();
  h.workspace.role = 'viewer';
  pending.resolve(h.sessions.get('native')); await flush();
  assert.equal(h.sockets.length, 0);
  h.apiOverride = null; h.workspace.role = 'operator';
  await view.pane.open({ kind: 'history', agentId: 'native' });
  ui(view.root, 'history-continue-native').click(); await flush();
  assert.equal(h.sockets.length, 1);
  h.workspace.role = 'viewer'; h.sockets[0].open();
  assert.equal(h.sockets[0].frames.length, 0);
  assert.match(ui(view.root, 'error').textContent, /permission to continue was revoked/);
  assert.equal(ui(view.root, 'chat-send').disabled, true);
  view.pane.close();
});

test('native receipts retain launch tags, serial turns and pane-local drafts alongside ordinary Chat', async () => {
  const h = harness(), native = h.create('native'), ordinary = h.create('ordinary');
  const socket = await h.attach(native, nativeSession('native-session', { state: 'live', launch_id: 'native-launch' }));
  const cli = await h.attach(ordinary, { id: 'cli-session', agent_id: 'cli', state: 'live', surface: 'structured', available: true, launch_id: 'cli-launch' });
  assert.equal(native.root.querySelector('[data-chat-control="permission_mode"]'), null);
  assert.ok(ordinary.root.querySelector('[data-chat-control="permission_mode"]'));
  ui(ordinary.root, 'chat-input').value = 'ordinary unsent draft';
  const field = ui(native.root, 'chat-input'); field.value = 'first native input'; submit(native.root);
  const first = inputs(socket)[0];
  assert.equal(first.launch_id, 'native-launch');
  assert.match(first.client_input_id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.equal(first.options.client_input_id, undefined);
  field.value = 'newer native draft'; submit(native.root);
  assert.equal(inputs(socket).length, 1);
  socket.receive({ type: 'input_ack', session_id: 'other-session', client_input_id: first.client_input_id, status: 'rejected' });
  socket.receive({ type: 'input_ack', session_id: 'native-session', launch_id: 'old-launch', client_input_id: first.client_input_id, status: 'rejected' });
  assert.equal(ui(native.root, 'chat-send').disabled, true);
  socket.receive({ type: 'input_ack', session_id: 'native-session', launch_id: 'native-launch', client_input_id: first.client_input_id,
    status: 'rejected', reason: 'agent_busy' });
  assert.equal(field.value, 'newer native draft');
  assert.equal(ui(native.root, 'chat-send').disabled, false);
  assert.equal(native.root.querySelectorAll('.do-user').length, 0);
  assert.equal(ui(ordinary.root, 'chat-input').value, 'ordinary unsent draft');
  assert.equal(inputs(cli).length, 0);
  // Live title updates are shared, but do not overwrite either composer.
  socket.receive({ type: 'session.updated', session_id: 'native-session', title: 'Native title update' });
  assert.equal(native.pane.getState().title, 'Native title update');
  assert.equal(field.value, 'newer native draft');
  h.workspace.role = 'viewer'; native.pane.refreshAccess(); submit(native.root);
  assert.equal(inputs(socket).length, 1);
  assert.equal(field.value, 'newer native draft');
  native.pane.close(); ordinary.pane.close();
});

test('unconfirmed native input is never auto-resent through reconnect or a later receipt', async () => {
  const h = harness(), view = h.create('unconfirmed');
  const socket = await h.attach(view, nativeSession('native-session', { state: 'live', launch_id: 'native-launch' }));
  ui(view.root, 'chat-input').value = 'potentially side-effecting input'; submit(view.root);
  const first = inputs(socket)[0];
  socket.close();
  assert.equal(ui(view.root, 'chat-input').value, 'potentially side-effecting input');
  assert.match(ui(view.root, 'chat-composer-error').textContent, /unconfirmed.*nothing was automatically resent/i);
  await view.pane.reconnect();
  const replacement = h.sockets.at(-1); replacement.open();
  replacement.receive({ type: 'ready', session_id: 'native-session', launch_id: 'native-launch' });
  replacement.receive({ type: 'collaboration', role: 'operator', keyboard: {}, participants: [] });
  socket.receive({ type: 'input_ack', session_id: 'native-session', client_input_id: first.client_input_id, status: 'delivered' });
  await flush();
  assert.equal(h.sockets.flatMap(inputs).length, 1);
  assert.equal(ui(view.root, 'chat-input').value, 'potentially side-effecting input');
  assert.ok(!replacement.frames.some(frame => frame.type === 'resume'));
  view.pane.close();
});

test('uncertain native rejection restores a draft without resending and context loss stays fail-closed', async () => {
  const h = harness(), view = h.create('uncertain');
  const socket = await h.attach(view, nativeSession('native-session', { state: 'live', launch_id: 'native-launch' }));
  ui(view.root, 'chat-input').value = 'inspect tool effects first'; submit(view.root);
  socket.receive({ type: 'input_ack', session_id: 'native-session', launch_id: 'native-launch', client_input_id: inputs(socket)[0].client_input_id,
    status: 'rejected', reason: 'execution_uncertain' });
  assert.equal(ui(view.root, 'chat-input').value, 'inspect tool effects first');
  assert.match(ui(view.root, 'chat-composer-error').textContent, /not resent.*side effects/);
  assert.equal(inputs(socket).length, 1);
  socket.receive({ type: 'runtime.unavailable', session_id: 'native-session', launch_id: 'native-launch',
    code: 'native_context_missing', message: 'Known native context is missing; recovery is required.' });
  assert.equal(view.pane.getState().status, 'unavailable');
  assert.equal(ui(view.root, 'chat-send').disabled, true);
  submit(view.root);
  assert.equal(inputs(socket).length, 1);
  assert.equal(ui(view.root, 'chat-input').value, 'inspect tool effects first');
  assert.equal(h.timers.size, 0, 'context loss cannot auto-reconnect into a new native session');
  assert.ok(!socket.frames.some(frame => frame.type === 'resume'));
  view.pane.close();
});
