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