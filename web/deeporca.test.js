// DeepOrca core runtime tests
{
const test = require('node:test');
const assert = require('node:assert/strict');
const {createDocument} = require('./test-dom.js');
const Chat = require('./chat.js');
const Renderer = require('./integrations/deeporca/chat.js');
const Runtime = require('./integrations/deeporca/runtime.js');
const AgentUI = require('./integrations/deeporca/agent-ui.js');

test('DeepOrca contracts stay local; unknown renderers use standard chat and CLI approval', () => {
  assert.equal(Chat.supportsRenderer('deeporca-chat-v1'), true);
  assert.equal(Chat.supportsRenderer('runtime-catalog'), false);
  assert.equal(Chat.supportsRenderer('https://evil.invalid/renderer.js'), false);
  assert.equal(Chat.supportsRenderer('__proto__'), false);
  assert.equal(Chat.runtimeContract({runtime:'cli'}, {}, 'unrecognized').interactiveApproval, true);
  assert.equal(Chat.runtimeContract({runtime:'deeporca'}, {}, 'unrecognized'), Runtime);
  assert.equal(Runtime.serialTurns, true);
  assert.equal(Runtime.explicitContinuation, true);
});

test('native usage folds outside tool transcript and does not alter CLI status tools', () => {
  const native = Chat.initialChatState(); native.renderer = 'deeporca-chat-v1';
  const call = {ev:'tool.call', turn_id:'turn', tool_id:'__proto__', tool:'DeepOrca usage', input:{total_tokens:20}, native:{kind:'status'}};
  Chat.applyEvent(native, call);
  Chat.applyEvent(native, {ev:'tool.result',turn_id:'turn',tool_id:'__proto__',content:{total_tokens:25}});
  assert.equal(native.items.length, 0);
  assert.equal(Object.values(native.runtimeStatus)[0].content.total_tokens, 25);
  assert.equal(Object.getPrototypeOf(native.runtimeStatus), null);
  const cli = Chat.initialChatState(); Chat.applyEvent(cli, call);
  assert.equal(cli.items.length, 1);
  Chat.applyEvent(native, {ev:'turn.end',turn_id:'turn',status:'completed',usage:{total_tokens:25}});
  assert.equal(native.items[0].usage.total_tokens, 25);
  const streaming = Chat.initialChatState(); streaming.renderer = 'deeporca-chat-v1';
  Chat.applyEvent(streaming, {ev:'message.delta',text:'one'});
  Chat.applyEvent(streaming, call);
  Chat.applyEvent(streaming, {ev:'message.delta',text:' two'});
  assert.equal(streaming.items.length, 1);
  assert.equal(streaming.items[0].text, 'one two');
});

test('Markdown preserves ordered/nested structure, start values, tables and escaped code', () => {
  const doc = createDocument();
  const node = Renderer.markdown(doc, '4. **First**\n   - nested\n     1. deeper\n5. Second\n\n| Key | Value |\n| --- | ---: |\n| `a|b` | 4 |\n\n```js\n<img src=x>\n```');
  assert.equal(node.querySelector('ol').getAttribute('start'), '4');
  assert.equal(node.querySelectorAll('ol').length, 2);
  assert.equal(node.querySelectorAll('ul').length, 1);
  assert.equal(node.querySelectorAll('li').length, 4);
  assert.equal(node.querySelectorAll('td').length, 2);
  assert.equal(node.querySelector('strong').textContent, 'First');
  assert.equal(node.querySelector('pre').textContent, '<img src=x>\n');
  assert.equal(node.querySelectorAll('img').length, 0);
});

test('Markdown URLs and raw provider markup cannot activate scripts or remote assets', () => {
  const doc = createDocument();
  const node = Renderer.markdown(doc, '<script>alert(1)</script>\n\n[unsafe](javascript:alert(1)) [data](data:text/html,x) [good](https://example.invalid/a(b)) ![picture](https://example.invalid/track.png)');
  assert.equal(node.querySelectorAll('script,img,iframe').length, 0);
  assert.equal(node.querySelectorAll('a').length, 1);
  assert.equal(node.querySelector('a').href, 'https://example.invalid/a(b)');
  for (const value of ['javascript:alert(1)', 'data:text/html,x', 'file:///x', '//evil.invalid', 'java\nscript:alert(1)']) assert.equal(Renderer.safeUrl(value), null);
  assert.equal(Renderer.safeUrl('mailto:local@example.invalid'), 'mailto:local@example.invalid');
});

test('tool summaries describe intent and missing/interrupted/failed tools never imply success', () => {
  assert.deepEqual(Renderer.summarizeTool({tool:'run_command',input:{command:'node --test web/*.test.js'}}), {name:'run_command',detail:'node --test web/*.test.js'});
  assert.equal(Renderer.toolStatus({result:null}, {run:{state:'running'}}), 'Running');
  assert.equal(Renderer.toolStatus({result:null}, {}, {live:false}), 'Result not recorded');
  assert.match(Renderer.toolStatus({result:null,status:'interrupted'}, {}), /Interrupted/);
  assert.equal(Renderer.toolStatus({result:null,status:'incomplete'}, {}), 'Missing result');
  assert.equal(Renderer.toolStatus({result:'',is_error:true}, {}), 'Failed');
  assert.equal(Renderer.toolStatus({result:'',code:'missing_result'}, {}), 'Missing result');
  assert.equal(Renderer.toolStatus({result:'{"exit_code":2}'}, {}), 'Failed');
  assert.equal(Renderer.toolStatus({result:{status:'timed_out'}}, {}), 'Timed out');
  assert.equal(Renderer.toolStatus({result:{status:'running',task_id:'bg'}}, {}), 'Started · background');
  assert.equal(Renderer.toolStatus({result:{result_file:'local-output.txt'}}, {}), 'Outcome uncertain');
  assert.equal(Renderer.summarizeTool({tool:'patch',input:{operation:{type:'update_file',path:'web/chat.js'}}}).detail, 'web/chat.js');
});

test('agent UI explains automatic profile mode without rendering diagnostic paths', () => {
  assert.equal(AgentUI.runtimeStatus({state:'C:/private/state'}).state, 'pending');
  const fields = AgentUI.creationFields({templates:[]});
  assert.deepEqual(fields.map(field=>field.name), ['profile_mode', 'profile_ref', 'native_stopped', 'base_url', 'model', 'auth_mode', 'api_key', 'context_window', 'reasoning_effort']);
  assert.equal(fields.find(field=>field.name === 'api_key').type, 'password');
  assert.ok(fields.every(field=>!['profile_path','template','command'].includes(field.name)));
  assert.equal(AgentUI.settingsHtml.includes('password'), false);
});

test('access-only disconnect redraws unfinished tools without waiting for another event', () => {
  const doc = createDocument(), container = doc.createElement('div'); doc.body.appendChild(container);
  const view = Renderer.createView(container);
  view.setAccess({live:true, readOnly:false, pending:true});
  view.restore([
    {ev:'turn.start',turn_id:'active'},
    {ev:'tool.call',turn_id:'active',tool_id:'call',tool:'run_command',input:{command:'test'}},
  ]);
  assert.equal(container.querySelector('.do-tool-state').textContent, 'Running');
  assert.ok(container.querySelector('.do-run-note'));
  view.setAccess({live:false});
  assert.equal(container.querySelector('.do-tool-state').textContent, 'Result not recorded');
  assert.equal(container.querySelector('.do-run-note'), null);
  assert.equal(container.querySelector('.do-access').textContent, 'Read-only transcript');
  view.setAccess({live:true, pending:false});
  assert.equal(container.querySelector('.do-access').textContent, 'Managed runtime');
  view.destroy();
});

test('actual canonical native status and terminal usage survive replay without pseudo-tools', () => {
  const state = Chat.initialChatState();
  const status = {ev:'status',subtype:'usage',turn_id:'sdk',native:{runtime:'deeporca',type:'usage',usage:{total_tokens:7}}};
  const terminal = {ev:'turn.end',turn_id:'sdk',status:'completed',native:{runtime:'deeporca',type:'done',usage:{total_tokens:9}}};
  const source = JSON.stringify([status, terminal]);
  Chat.applyEvent(state, status); Chat.applyEvent(state, terminal);
  assert.equal(state.items.length, 1);
  assert.equal(Object.values(state.runtimeStatus)[0].content.total_tokens, 7);
  assert.equal(state.items[0].native.usage.total_tokens, 9);
  assert.equal(JSON.stringify([status, terminal]), source);
  const doc = createDocument(), container = doc.createElement('div'); doc.body.appendChild(container);
  const view = Renderer.createView(container);
  view.renderState(state, {readOnly:true,live:false});
  assert.equal(container.querySelectorAll('.do-tool').length, 0);
  assert.match(container.querySelector('.do-usage').textContent, /total_tokens/);
  assert.match(container.querySelector('.do-runtime-meta').textContent, /total_tokens/);
  view.destroy();
});
}

// DeepOrca history merge and ordering tests
{
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

}

// DeepOrca agent configuration tests
{
const test = require('node:test');
const assert = require('node:assert/strict');
const {webcrypto, createHash} = require('node:crypto');
const UI = require('./integrations/deeporca/agent-ui.js');
const values = changes=>({base_url:'http://localhost:11434/v1', model:'vendor/model:latest',
  context_window:'32768', reasoning_effort:'', auth_mode:'none', ...changes});

test('all native reasoning efforts and provider model routing are supported', ()=>{
  assert.deepEqual(UI.EFFORTS, ['', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']);
  for(const reasoning_effort of UI.EFFORTS){
    const config = UI.modelConfig(values({reasoning_effort, model:'provider/model@revision+variant'}));
    assert.equal(config.reasoning_effort, reasoning_effort);
    assert.equal(config.context_window, 32768);
  }
});

test('browser endpoint validation agrees with shared and native SDK validation', ()=>{
  for(const base_url of ['https://my_llm.invalid/v1', 'https://bad..invalid/v1',
    'https://-invalid/v1', 'https://host/v1/%', 'https://host/v1/{bad}',
    'https://host/v1/<bad>', 'https://éxample.invalid/v1', 'https://host:0/v1',
    'https://host/v1?', 'https://user:secret@host/v1', 'https://host/v1#']){
    assert.throws(()=>UI.modelConfig(values({base_url})), /Endpoint/);
  }
  for(const base_url of ['http://[::1]:11434/v1/', 'https://api.example.invalid/v1',
    'https://local-model/v1/%20', 'http://localhost:1234/v1']){
    assert.equal(UI.modelConfig(values({base_url})).base_url, base_url);
  }
});

test('context windows are not guessed and must be exact positive token integers', ()=>{
  for(const context_window of ['', '0', '-1', '1.2', '128,000', '1e5', '9007199254740992'])
    assert.throws(()=>UI.modelConfig(values({context_window})), /Context window/);
});

test('legacy fixed model survives settings serialization instead of changing profile identity', async()=>{
  const agent = {runtime_config:{integration_version:1,model:'legacy-model'}};
  const result = await UI.configuredPayload(values({model:'legacy-model'}), {}, agent);
  assert.equal(result.model, 'legacy-model');
  assert.equal(UI.settingsFields(agent).find(field=>field.name === 'model').value, 'legacy-model');
  await assert.rejects(UI.configuredPayload(values({model:'other-model'}), {}, agent), /fixed model override/);
});

test('no-auth needs no WebCrypto, keeping secrets never resends the stored envelope', async()=>{
  const descriptor = {agent_config:{profile_modes:['create'],configuration_templates:[{id:'connector-default'}]}};
  assert.deepEqual((await UI.configuredPayload(values(), descriptor)).credential, {mode:'none'});
  const agent = {runtime_config:{llm:UI.modelConfig(values()),credential:{mode:'sealed',ciphertext:'stored'}}};
  assert.equal((await UI.configuredPayload(values({auth_mode:'keep'}), {}, agent)).credential, undefined);
  await assert.rejects(UI.configuredPayload(values({auth_mode:'keep',base_url:'https://other.invalid/v1'}), {}, agent), /Endpoint changed/);
});

test('browser sealing is randomized, endpoint authenticated and rejects unsafe key text', async()=>{
  const pair = await webcrypto.subtle.generateKey({name:'RSA-OAEP',modulusLength:2048,
    publicExponent:new Uint8Array([1,0,1]),hash:'SHA-256'}, true, ['encrypt','decrypt']);
  const public_key = await webcrypto.subtle.exportKey('jwk', pair.publicKey);
  const spki = await webcrypto.subtle.exportKey('spki', pair.publicKey);
  const key_id = createHash('sha256').update(Buffer.from(spki)).digest('hex');
  const descriptor = {agent_config:{credential_key:{version:1,algorithm:'RSA-OAEP-256+A256GCM',key_id,public_key}}};
  const endpoint = values().base_url, secret = 'fake-browser-node-test-key';
  const a = await UI.sealCredential(secret, endpoint, descriptor, webcrypto);
  const b = await UI.sealCredential(secret, endpoint, descriptor, webcrypto);
  assert.notEqual(a.ciphertext, b.ciphertext);
  assert.ok(!JSON.stringify(a).includes(secret));
  const raw = await webcrypto.subtle.decrypt({name:'RSA-OAEP'}, pair.privateKey, Buffer.from(a.wrapped_key,'base64'));
  const aes = await webcrypto.subtle.importKey('raw', raw, {name:'AES-GCM'}, false, ['decrypt']);
  const decrypt = url=>webcrypto.subtle.decrypt({name:'AES-GCM',iv:Buffer.from(a.iv,'base64'),
    additionalData:Buffer.from('agentbridge/deeporca/credential/v1\0'+key_id+'\0'+url)}, aes, Buffer.from(a.ciphertext,'base64'));
  assert.equal(Buffer.from(await decrypt(endpoint)).toString(), secret);
  await assert.rejects(decrypt('https://other.invalid/v1'));
  for(const key of ['fake key', 'fake\nkey', 'fake\u0085key', '${ENV_KEY}', ' fake-key'])
    await assert.rejects(UI.sealCredential(key, endpoint, descriptor, webcrypto), /whitespace|control|placeholders/);
  await assert.rejects(UI.sealCredential(secret, endpoint, descriptor, {}), /encryption is unavailable/);
});

test('rename-only settings do not require configuring a legacy profile', async()=>{
  const input = {display_name:'New name',base_url:'',model:'',context_window:'',reasoning_effort:'',api_key:'',auth_mode:'keep'};
  assert.deepEqual(await UI.settingsPayload(input, {runtime_config:{}}, {}), {display_name:'New name'});
  await assert.rejects(UI.settingsPayload({...input, base_url:'https://models.invalid/v1'}, {runtime_config:{}}, {}), /Model/);
});

test('unknown status and malformed capabilities cannot echo paths or crash presentation', ()=>{
  assert.equal(UI.runtimeStatus({state:'C:/private/profile',code:'C:/private/key'}).state,'pending');
  assert.equal(UI.runtimeStatus({state:'error',code:'constructor'}).message.includes('function'),false);
  assert.deepEqual(UI.agentConfiguration({agent_config:{profile_modes:['create'],configuration_templates:[null,{id:'../private'},{id:'connector-default'}]}}).templates,
    [{value:'connector-default',label:'connector-default'}]);
});
}

// DeepOrca profile binding tests
{
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const AgentUI = require('./integrations/deeporca/agent-ui.js');
const {createBrowser} = require('./test-dom.js');
const nativeRef = 'native-' + 'a'.repeat(32);
const secondRef = 'native-' + 'b'.repeat(32);
const descriptor = {runtime:'delegated-orca',installed:true,scope:'project',default_project_mode:'required',
  execution:{environment:'managed',permission_mode:'runtime_managed',configuration_ui:'deeporca_managed_profile'},
  agent_config:{profile_modes:['create','bind'],existing_profiles:[{id:nativeRef,label:'Personal native profile'}]}};
const values = {base_url:'http://localhost:11434/v1',model:'local-model',auth_mode:'none',api_key:'',context_window:'32768',reasoning_effort:''};
const flush = async()=>{for(let i=0;i<20;i++) await Promise.resolve();};

test('profile controls default to create; binding requires explicit capability and opaque inventory', () => {
  for (const item of [undefined,{}, {agent_config:{}}, {agent_config:{existing_profiles:[{id:nativeRef,label:'Local'}]}},
    {agent_config:{profile_modes:['create']}}, {agent_config:{profile_modes:'bind'}}]) {
    const config = AgentUI.agentConfiguration(item), fields = AgentUI.creationFields(config);
    assert.equal(config.canBind,false);
    assert.equal(fields[0].value,'create');
    assert.deepEqual(fields[0].options.map(item=>item.value),['create']);
    assert.equal(fields.find(item=>item.name === 'native_stopped').type,'checkbox');
  }
  const config = AgentUI.agentConfiguration({...descriptor,agent_config:{...descriptor.agent_config,
    existing_profiles:[...descriptor.agent_config.existing_profiles,{id:'/private/home',label:'Invalid'},{id:'native-'+'A'.repeat(32),label:'Invalid'}]}});
  const fields = AgentUI.creationFields(config);
  assert.equal(config.canBind,true);
  assert.deepEqual(config.profiles,[{value:nativeRef,label:'Personal native profile'}]);
  assert.deepEqual(fields[0].options.map(item=>item.label),['Create a new profile','Bind an existing profile']);
  assert.equal(fields[1].value,''); // Never select the first profile implicitly.
  assert.equal(fields[2].value,'');
  assert.equal(fields.find(item=>item.name === 'api_key').value,'');
  assert.ok(fields.every(item=>item.type !== 'hidden'));
});

test('binding serializes only consent and advertised ref without inspecting model or secret drafts', async () => {
  const draft = {profile_mode:'bind',profile_ref:nativeRef,native_stopped:true};
  for (const key of Object.keys(values)) Object.defineProperty(draft,key,{get(){throw new Error('Managed draft read: '+key);}});
  assert.deepEqual(await AgentUI.configuredPayload(draft,descriptor),{
    integration_version:1,profile:{mode:'bind',profile_ref:nativeRef,native_stopped:true},
  });
  const created = await AgentUI.configuredPayload({...values,profile_mode:'create',profile_ref:nativeRef,native_stopped:true},descriptor);
  assert.equal(created.profile.mode,'create'); assert.equal(created.profile.profile_ref,undefined);
  assert.equal(created.credential.mode,'none'); assert.equal(created.llm.context_window,32768);
});

test('binding requires real boolean consent, exact current inventory ref and Connector support', async () => {
  const draft = {profile_mode:'bind',profile_ref:nativeRef,native_stopped:true};
  for (const native_stopped of [undefined,false,'true','on',1])
    await assert.rejects(AgentUI.configuredPayload({...draft,native_stopped},descriptor),/Confirm that you have stopped/);
  for (const profile_ref of ['',undefined,'Personal native profile','C:/private/home','native-'+'A'.repeat(32),secondRef])
    await assert.rejects(AgentUI.configuredPayload({...draft,profile_ref},descriptor),/Select an available native profile/);
  for (const item of [undefined,{}, {agent_config:{profile_modes:['create'],existing_profiles:[{id:nativeRef,label:'Old SDK'}]}}])
    await assert.rejects(AgentUI.configuredPayload(draft,item),/does not support binding/);
  await assert.rejects(AgentUI.configuredPayload({...draft,profile_mode:'free-form'},descriptor),/supported profile mode/);
});

test('native Agent settings are immutable and rename-only even if mutable values are supplied', async () => {
  const agent = {runtime:'delegated-orca',display_name:'Before',runtime_config:{integration_version:1,
    profile:{mode:'bind',profile_ref:nativeRef,native_stopped:true}}};
  assert.deepEqual(AgentUI.settingsFields(agent),[]);
  assert.deepEqual(await AgentUI.settingsPayload({display_name:'After',...values,api_key:'never-forward-this'},agent,descriptor),{display_name:'After'});
  await assert.rejects(AgentUI.configuredPayload(values,descriptor,agent),/read-only/);
  assert.match(AgentUI.runtimeStatus({state:'ready'},true).message,/Keep native DeepOrca stopped/);
  assert.match(AgentUI.runtimeStatus({state:'error',code:'configuration_busy',message:'C:/private/profile'},true).message,/Stop native DeepOrca/);
  assert.match(AgentUI.runtimeStatus({state:'error',code:'existing_profile_unavailable'},true).message,/Restore it on the Connector/);
  assert.match(AgentUI.runtimeStatus({state:'error',code:'existing_profile_api_unavailable'},true).message,/Update its DeepOrca SDK/);
  assert.doesNotMatch(AgentUI.runtimeStatus({state:'error',code:'existing_profile_unavailable',message:'C:/private/profile'},true).message,/C:\/private/);
});

function setup() {
  const browser = createBrowser({terminal:false}), calls = [];
  const chat = browser.loadModule('chat.js'), contract = chat.runtimeContract({runtime:'deeporca'});
  chat.runtimeContract = agent=>agent?.runtime === 'delegated-orca' ? contract : {};
  const capability = JSON.parse(JSON.stringify(descriptor));
  const box = {id:'m',name:'Local Connector',workspace_id:'w',online:true,capabilities:{runtimes:[capability,{runtime:'cli',installed:true}]},
    projects:[{id:'p',name:'Registered project'}],agents:[]};
  const context = {user:{id:'u'},workspace:{id:'w',role:'owner'},epoch:1,devboxes:[box]};
  const dialogs = browser.loadModule('dialogs.js').createDialogs(browser.document);
  const state = {fail:false};
  const management = browser.loadModule('management.js').createManagement({dialogs,document:browser.document,context:()=>context,refresh:async()=>{},
    api:async(path,options)=>{
      const body = options.body ? JSON.parse(options.body) : undefined; calls.push({path,method:options.method,body});
      if(state.fail) throw new Error('Fixture Connector unavailable; retry');
      if(options.method === 'PATCH') return Object.assign(box.agents[0],body);
      if(path.endsWith('/retry')) return {state:'pending'};
      const agent = {id:'a',...body,runtime_status:{state:'ready'}}; box.agents.push(agent); return agent;
    }});
  return {browser,box,capability,dialogs,calls,management,state};
}

test('real delegated creation preserves drafts, has boolean consent, excludes hidden managed fields and retries', async t => {
  const {browser,box,capability,dialogs,calls,management,state} = setup();
  t.after(()=>dialogs.close());
  management.createAgent('m'); await flush();
  let root = browser.document.querySelector('.overlay');
  const field = name=>root.querySelector(`[data-field="${name}"]`);
  const change = (name,value)=>{field(name).value=value;field(name).dispatchEvent({type:'change'});};
  const submit = async()=>{root.querySelector('form').dispatchEvent({type:'submit'});await flush();};
  assert.equal(field('runtime').value,'delegated-orca');
  assert.equal(field('profile_mode').value,'create'); assert.equal(field('profile_ref').value,'');
  assert.match(root.textContent,/minimal security: broad tools, no approvals/);
  assert.match(root.textContent,/trusted Connector template may override/);
  field('handle').value='Bound helper'; field('local_project_id').value='p';
  for(const [key,value] of Object.entries(values)) field(key).value=value;
  field('api_key').value='draft-secret-never-send';
  change('profile_mode','bind');
  for(const name of Object.keys(values)) {assert.equal(field(name).disabled,true);assert.equal(field(name).closest('.field').hidden,true);}
  assert.equal(field('native_stopped').checked || false,false);
  assert.equal(field('native_stopped').required,true);
  assert.match(root.textContent,/Old native chats are not imported/);
  assert.equal(root.querySelector('input[type="hidden"]'),null);
  await submit(); assert.equal(calls.length,0); assert.match(root.querySelector('[data-error]').textContent,/Select an available/);
  change('profile_ref',nativeRef); await submit(); assert.equal(calls.length,0); assert.match(root.querySelector('[data-error]').textContent,/Confirm that/);
  field('native_stopped').checked=true;
  change('profile_mode','create'); assert.equal(field('api_key').value,'draft-secret-never-send');
  assert.equal(field('model').value,'local-model'); assert.equal(field('model').disabled,false);
  change('profile_mode','bind'); assert.equal(field('profile_ref').value,nativeRef); assert.equal(field('native_stopped').checked,true);
  change('runtime','cli'); assert.equal(field('profile_mode').closest('.field').hidden,true); assert.equal(field('native_stopped').disabled,true);
  assert.equal(root.querySelector('.do-native-profile').hidden,true);
  change('runtime','delegated-orca'); assert.equal(field('profile_ref').value,nativeRef);
  // Removed inventory never silently chooses the replacement or switches to create.
  capability.agent_config.existing_profiles=[{id:secondRef,label:'Another profile'}];
  root.querySelector('[data-refresh-projects]').click(); await flush();
  assert.equal(field('profile_ref').value,nativeRef);
  await submit(); assert.equal(calls.length,0); assert.match(root.querySelector('[data-error]').textContent,/Select an available/);
  capability.agent_config.profile_modes=['create']; root.querySelector('[data-refresh-projects]').click(); await flush();
  assert.equal(field('profile_mode').value,'bind'); assert.equal(field('profile_mode').closest('.field').hidden,true);
  await submit(); assert.equal(calls.length,0); assert.match(root.querySelector('[data-error]').textContent,/does not support/);
  capability.agent_config.profile_modes=['create','bind']; capability.agent_config.existing_profiles=descriptor.agent_config.existing_profiles;
  root.querySelector('[data-refresh-projects]').click(); await flush();
  state.fail=true; await submit(); assert.equal(calls.length,1);
  assert.equal(field('profile_ref').value,nativeRef); assert.equal(field('native_stopped').checked,true); assert.equal(field('api_key').value,'draft-secret-never-send');
  state.fail=false; await submit(); assert.equal(calls.length,2);
  assert.deepEqual(calls[1].body,{handle:'Bound helper',display_name:'Bound helper',runtime:'delegated-orca',local_project_id:'p',
    runtime_config:{integration_version:1,profile:{mode:'bind',profile_ref:nativeRef,native_stopped:true}}});
  assert.doesNotMatch(JSON.stringify(calls),/draft-secret|credential|llm|context_window/);
  assert.equal(box.agents.length,1);
  dialogs.close(); await flush(); management.agentSettings('a'); await flush(); root=browser.document.querySelector('.overlay');
  assert.equal(root.querySelectorAll('[data-field]').length,1);
  for(const name of Object.keys(values)) assert.equal(field(name),null);
  assert.match(root.textContent,/Personal native profile/); assert.match(root.textContent,new RegExp(nativeRef));
  assert.match(root.textContent,/Only the Agent name/); assert.match(root.textContent,/Keep native DeepOrca stopped/);
  field('display_name').value='Renamed native helper'; await submit();
  assert.deepEqual(calls.at(-1).body,{display_name:'Renamed native helper'});
});

test('bound settings safely explain missing and busy profiles, retry has no configuration body', async t => {
  const {browser,box,capability,dialogs,calls,management} = setup(); t.after(()=>dialogs.close());
  box.agents=[{id:'a',runtime:'delegated-orca',local_project_id:'p',handle:'native',display_name:'Native',
    runtime_config:{integration_version:1,profile:{mode:'bind',profile_ref:nativeRef,native_stopped:true}},
    runtime_status:{state:'error',code:'configuration_busy',message:'C:/private/profile'}}];
  capability.agent_config.existing_profiles=[];
  management.agentSettings('a');await flush();
  const root=browser.document.querySelector('.overlay');
  assert.match(root.textContent,/Profile unavailable in current inventory/);
  assert.match(root.textContent,/Stop native DeepOrca/); assert.doesNotMatch(root.textContent,/C:\/private|API key/);
  root.querySelector('[data-refresh-status]').click();await flush();assert.equal(calls.length,0);
  root.querySelector('[data-retry-runtime]').click();await flush();
  assert.equal(calls[0].path,'/api/agents/a/runtime/retry'); assert.equal(calls[0].body,undefined);
});

test('old Connector hides binding controls; empty supported inventory shows safe help without an implicit choice', async t => {
  const {browser,capability,dialogs,management,calls} = setup(); t.after(()=>dialogs.close());
  delete capability.agent_config;
  management.createAgent('m'); await flush();
  const root=browser.document.querySelector('.overlay');
  const field=name=>root.querySelector(`[data-field="${name}"]`);
  assert.equal(field('profile_mode').closest('.field').hidden,true);
  assert.equal(field('profile_mode').disabled,true);
  assert.equal(root.querySelector('.do-native-profile').hidden,true);
  assert.equal(field('profile_ref').disabled,true); assert.equal(field('native_stopped').disabled,true);
  assert.equal(field('model').disabled,false);
  capability.agent_config={profile_modes:['create','bind'],existing_profiles:[]};
  root.querySelector('[data-refresh-projects]').click(); await flush();
  assert.equal(field('profile_mode').closest('.field').hidden,false);
  field('profile_mode').value='bind';field('profile_mode').dispatchEvent({type:'change'});
  assert.equal(field('profile_ref').value,''); assert.equal(field('profile_ref').querySelectorAll('option').length,1);
  assert.match(root.querySelector('[data-native-inventory]').textContent,/No native profiles are available/);
  assert.match(root.querySelector('[data-native-inventory]').textContent,/Paths cannot be entered/);
  field('handle').value='missing';field('local_project_id').value='p';field('native_stopped').checked=true;
  root.querySelector('form').dispatchEvent({type:'submit'});await flush();assert.equal(calls.length,0);
  assert.match(root.querySelector('[data-error]').textContent,/Select an available native profile/);
});
}
