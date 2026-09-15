'use strict';
const test = require('node:test');
const assert = require('node:assert');
const C = require('./chat.js');
const { createBrowser } = require('./test-dom.js');

test('message.delta accretes into one assistant bubble', () => {
  let s = C.initialChatState();
  C.applyEvent(s, { ev: 'message.delta', text: 'Hel' });
  C.applyEvent(s, { ev: 'message.delta', text: 'lo' });
  assert.strictEqual(s.items.length, 1);
  assert.strictEqual(s.items[0].kind, 'assistant');
  assert.strictEqual(s.items[0].text, 'Hello');
});

test('message final closes the open assistant bubble', () => {
  let s = C.initialChatState();
  C.applyEvent(s, { ev: 'message.delta', text: 'Hi' });
  C.applyEvent(s, { ev: 'message', final: true, text: '' });
  assert.strictEqual(s._openAssistant, null);
  C.applyEvent(s, { ev: 'message.delta', text: 'Next' });
  assert.strictEqual(s.items.length, 2);
});

test('tool.call then tool.result attach to same card', () => {
  let s = C.initialChatState();
  C.applyEvent(s, { ev: 'tool.call', tool: 'Bash', tool_id: 't1', input: { command: 'ls' } });
  C.applyEvent(s, { ev: 'tool.result', tool_id: 't1', content: 'file1', is_error: false });
  const cards = s.items.filter(i => i.kind === 'tool');
  assert.strictEqual(cards.length, 1);
  assert.strictEqual(cards[0].result, 'file1');
  assert.strictEqual(cards[0].tool, 'Bash');
});

test('permission.ask sets pendingPermission', () => {
  let s = C.initialChatState();
  C.applyEvent(s, { ev: 'permission.ask', request_id: 'r1', tool: 'Write', input: {} });
  assert.ok(s.pendingPermission);
  assert.strictEqual(s.pendingPermission.request_id, 'r1');
});

test('turn.end appends a turn item and closes assistant', () => {
  let s = C.initialChatState();
  C.applyEvent(s, { ev: 'message.delta', text: 'x' });
  C.applyEvent(s, { ev: 'turn.end', is_error: false, cost_usd: 0.01 });
  assert.strictEqual(s._openAssistant, null);
  assert.ok(s.items.some(i => i.kind === 'turn'));
});

test('turn result does not duplicate an assistant message', () => {
  const state = C.initialChatState();
  C.appendUserTurn(state, 'question');
  C.applyEvent(state, { ev: 'message', final: true, text: 'same answer' });
  C.applyEvent(state, { ev: 'turn.end', result: 'same answer' });
  assert.equal(state.items.filter(item => item.kind === 'assistant').length, 1);
  assert.equal(state.items.find(item => item.kind === 'turn').result, null);
});

test('turn result remains a fallback when no assistant message arrives', () => {
  const state = C.initialChatState();
  C.appendUserTurn(state, 'question');
  C.applyEvent(state, { ev: 'turn.end', result: 'fallback answer' });
  assert.equal(state.items.find(item => item.kind === 'turn').result, 'fallback answer');
});


test('appendUserTurn adds a user bubble immediately', () => {
  let s = C.initialChatState();
  C.appendUserTurn(s, 'do it');
  assert.strictEqual(s.items[0].kind, 'user');
  assert.strictEqual(s.items[0].text, 'do it');
});

test('tool.call after streaming deltas closes the open bubble', () => {
  let s = C.initialChatState();
  C.applyEvent(s, { ev: 'message.delta', text: 'thinking' });
  C.applyEvent(s, { ev: 'tool.call', tool: 'Read', tool_id: 't2', input: {} });
  assert.strictEqual(s._openAssistant, null);
  assert.strictEqual(s.items.length, 2);
});


test('user.echo reconciles a local turn and restores one without duplication', () => {
  const state = C.initialChatState();
  C.appendUserTurn(state, 'hello', [{name: 'a.txt', size: 1}]);
  C.applyEvent(state, {ev: 'user.echo', text: 'hello', attachments: [{name: 'a.txt'}]});
  assert.equal(state.items.length, 1);
  assert.equal(state.items[0].local, false);

  const restored = C.initialChatState();
  C.applyEvent(restored, {ev: 'user.echo', text: 'hello', attachments: [{name: 'a.txt'}]});
  assert.equal(restored.items.length, 1);
  assert.equal(restored.items[0].attachments[0].name, 'a.txt');
});

test('session.config records only connector-confirmed scalar controls', () => {
  const state = C.initialChatState();
  C.applyEvent(state, {ev: 'session.config', options: {model: 'sonnet', reasoning_effort: 'high'}});
  assert.equal(state.configured, true);
  assert.deepEqual(state.config, {model: 'sonnet', reasoning_effort: 'high'});
});

test('normalizes generic adapter controls and builds declared turn options', () => {
  const controls = C.controlsFromCapability({features: {controls: [
    {key: 'model', label: 'Model', kind: 'select', scope: 'session', choices: ['a', 'b']},
    {key: 'attachments', label: 'Files', kind: 'file', scope: 'turn', max_files: 99,
      max_total_bytes: 999999999, accept: 'text/*'},
    {key: '../bad', kind: 'select', choices: ['x']},
  ]}});
  assert.equal(controls.length, 2);
  assert.equal(controls[1].max_files, 8);
  assert.equal(controls[1].max_total_bytes, 8 * 1024 * 1024);
  assert.deepEqual(C.buildTurnOptions(
    controls, {model: 'b', evil: 'x'}, {attachments: [{name: 'a', data: 'YQ=='}]}), {
      model: 'b', attachments: [{name: 'a', data: 'YQ=='}],
    });
  assert.deepEqual(C.buildTurnOptions(controls, {model: 'nope'}, {}), {});
});


test('parseEventPayload accepts JSONL restore tails and isolates bad rows', () => {
  assert.deepEqual(C.parseEventPayload(
    '{"ev":"user.echo","content":"one"}\nnot-json\n{"ev":"message.delta","text":"two"}'), [
    {ev: 'user.echo', content: 'one'},
    {ev: 'message.delta', text: 'two'},
  ]);
  assert.deepEqual(C.parseEventPayload(''), []);
});


test('reconciles displayed controls to connector-confirmed values', () => {
  const controls = C.controlsFromCapability({features: {controls: [
    {key: 'model', kind: 'select', scope: 'session', choices: ['a', 'b']},
    {key: 'reasoning_effort', kind: 'select', scope: 'turn', choices: ['low', 'high']},
  ]}});
  assert.deepEqual(C.reconcileControlValues(
    controls, {model: 'b', reasoning_effort: 'high', local: 'keep'},
    {model: 'a'}), {model: 'a', local: 'keep'});
});

test('session.config replaces stale per-turn configuration', () => {
  const state = C.initialChatState();
  C.applyEvent(state, {ev: 'session.config', options: {model: 'a'}});
  C.applyEvent(state, {ev: 'session.config', options: {reasoning_effort: 'low'}});
  assert.deepEqual(state.config, {reasoning_effort: 'low'});
});


test('restore payload replaces live transcript instead of duplicating it', () => {
  const live = C.initialChatState();
  C.applyEvent(live, {ev: 'user.echo', text: 'hello'});
  C.applyEvent(live, {ev: 'message.delta', text: 'world'});
  const folded = C.foldEventPayload(live, [
    '{"ev":"user.echo","text":"hello"}',
    '{"ev":"message.delta","text":"world"}',
  ].join('\n'), true);
  assert.notStrictEqual(folded.state, live);
  assert.deepEqual(folded.state.items.map(item => [item.kind, item.text]), [
    ['user', 'hello'], ['assistant', 'world'],
  ]);
});

test('capability v2 uses discovered models and permits an explicit custom model', () => {
  const controls = C.controlsFromCapability({
    schema_version: 2, models: {items: [{id: 'dynamic-model'}], allow_custom: true},
    surfaces: [{id: 'structured', default: true, features: {controls: [
      {key: 'model', label: 'Model', kind: 'select', scope: 'session', choices: ['fallback']},
    ]}}],
  });
  assert.deepEqual(controls[0].choices, ['dynamic-model']);
  assert.equal(controls[0].allow_custom, true);
  assert.deepEqual(C.buildTurnOptions(controls, {model: 'future-model'}, {}),
    {model: 'future-model'});
  assert.deepEqual(C.reconcileControlValues(controls, {}, {model: 'future-model'}),
    {model: 'future-model'});
});

test('tool snapshots update the streaming card with the same tool id', () => {
  const state = C.initialChatState();
  C.applyEvent(state, {
    ev: 'tool.call', tool: 'Read', tool_id: 'tool-1', input: {}, streaming: true,
  });
  C.applyEvent(state, {
    ev: 'tool.call', tool: 'Read', tool_id: 'tool-1',
    input: { file_path: 'README.md' }, streaming: false,
  });
  const tools = state.items.filter((item) => item.kind === 'tool');
  assert.equal(tools.length, 1);
  assert.deepEqual(tools[0].input, { file_path: 'README.md' });
  assert.equal(tools[0].streaming, false);
});

test('duplicate adjacent turn boundaries render once', () => {
  const state = C.initialChatState();
  C.applyEvent(state, { ev: 'turn.end', result: 'ok' });
  C.applyEvent(state, { ev: 'turn.end', result: 'ok' });
  assert.equal(state.items.filter((item) => item.kind === 'turn').length, 1);
});

function render(state, handlers) {
  const browser = createBrowser({ terminal: false });
  const root = browser.document.createElement('div');
  browser.document.body.appendChild(root);
  browser.loadModule('chat.js').renderChat(root, state, handlers);
  return root;
}

test('flat transcript uses semantic role captions and unprefixed plain text without changing canonical history', () => {
  const state = C.initialChatState();
  C.appendUserTurn(state, 'read <img src=x onerror=alert(1)>\nnext line', [
    { name: '<notes>.txt', data: 'PRIVATE_ATTACHMENT_BYTES' },
  ]);
  C.applyEvent(state, { ev: 'message.delta', text: '<script>unsafe()</script>\nplain output' });
  C.applyEvent(state, { ev: 'turn.end', result: 'duplicate summary', cost_usd: 0.25 });
  const before = JSON.stringify(state), root = render(state);
  assert.equal(root.querySelector('.chat-user .chat-role').textContent, 'You');
  assert.equal(root.querySelector('.chat-assistant .chat-role').textContent, 'Agent');
  assert.equal(root.querySelector('.chat-user .chat-text').textContent, 'read <img src=x onerror=alert(1)>\nnext line');
  assert.equal(root.querySelector('.chat-assistant .chat-text').textContent, '<script>unsafe()</script>\nplain output');
  assert.equal(root.querySelector('.chat-assistant').getAttribute('aria-label'), 'Agent output');
  assert.equal(root.querySelector('.chat-user').getAttribute('aria-label'), 'User message');
  assert.equal(root.querySelector('.chat-message-files').getAttribute('aria-label'), 'Attachments');
  assert.equal(root.querySelector('.chat-message-file').textContent, '<notes>.txt');
  assert.equal(root.querySelector('img, script, .chat-turn'), null);
  assert.equal(root.querySelectorAll('.chat-role').length, 2);
  assert.doesNotMatch(root.textContent, /turn complete|duplicate summary|PRIVATE_ATTACHMENT_BYTES|\$0\.2500/);
  assert.equal(JSON.stringify(state), before);
  assert.equal(state.items.at(-1).kind, 'turn');
  assert.equal(state.items.at(-1).cost_usd, 0.25);
});

test('echoes and restored turns keep authored text and captions without duplicating local messages', () => {
  const text = '> an authored quote\n\tsecond line & <plain text>';
  const state = C.initialChatState();
  C.appendUserTurn(state, text);
  C.applyEvent(state, { ev: 'user.echo', text });
  C.applyEvent(state, { ev: 'message.delta', text: 'first' });
  C.applyEvent(state, { ev: 'message.delta', text: '\nsecond' });
  C.applyEvent(state, { ev: 'turn.end', result: 'first\nsecond' });
  const restored = C.foldEventPayload(C.initialChatState(), [
    { ev: 'user.echo', text }, { ev: 'message', role: 'assistant', text: 'first\nsecond' },
    { ev: 'turn.end', result: 'first\nsecond' },
  ].map(JSON.stringify).join('\n'), true).state;
  for (const history of [state, restored]) {
    const before = JSON.stringify(history), root = render(history);
    assert.equal(root.querySelectorAll('.chat-user').length, 1);
    assert.equal(root.querySelector('.chat-user .chat-text').textContent, text, 'only generated prefixes are removed');
    assert.equal(root.querySelector('.chat-assistant .chat-text').textContent, 'first\nsecond');
    assert.deepEqual(root.querySelectorAll('.chat-role').map(node => node.textContent), ['You', 'Agent']);
    assert.equal(root.querySelector('.chat-turn'), null);
    assert.equal(JSON.stringify(history), before);
  }
});

test('failed turns, event errors and tool failures remain visibly accessible in the flat transcript', () => {
  const state = C.initialChatState();
  C.applyEvent(state, { ev: 'turn.end', is_error: true, result: 'runtime <failed>' });
  C.appendUserTurn(state, 'try again');
  C.applyEvent(state, { ev: 'message.delta', text: 'partial output' });
  C.applyEvent(state, { ev: 'turn.end', is_error: true, result: 'already streamed' });
  C.applyEvent(state, { ev: 'error', message: 'Connection <error>' });
  C.applyEvent(state, { ev: 'tool.call', tool_id: 'tool-1', tool: 'Read', input: { path: '<file>' } });
  C.applyEvent(state, { ev: 'tool.result', tool_id: 'tool-1', content: 'access <denied>', is_error: true });
  const before = JSON.stringify(state), root = render(state);
  const errors = root.querySelectorAll('.chat-error');
  assert.deepEqual(errors.map(node => node.querySelector('.chat-text').textContent), ['Turn failed: runtime <failed>', 'Turn failed', 'Connection <error>']);
  for (const node of errors) {
    assert.equal(node.hidden, false);
    assert.equal(node.getAttribute('role'), 'alert');
    assert.equal(node.querySelector('.chat-role').textContent, 'Error');
  }
  assert.match(root.querySelector('.chat-tool-error').textContent, /access <denied>/);
  assert.equal(root.querySelector('.chat-tool-error').getAttribute('aria-label'), 'Tool error: Read');
  assert.equal(root.querySelector('.chat-turn'), null);
  assert.equal(root.querySelector('file, denied, failed, error'), null);
  assert.equal(JSON.stringify(state), before);
});

test('fallback output and permission details remain text-only with operable labeled replies', () => {
  const state = C.initialChatState(), replies = [];
  C.applyEvent(state, { ev: 'turn.end', result: 'fallback <output>' });
  C.applyEvent(state, { ev: 'permission.ask', request_id: 'request-1', tool: '<Read>', input: { path: '<img src=x>' } });
  const root = render(state, { onPermission: (...args) => replies.push(args) });
  assert.equal(root.querySelector('.chat-assistant .chat-text').textContent, 'fallback <output>');
  assert.equal(root.querySelector('.chat-assistant .chat-role').textContent, 'Agent');
  assert.equal(root.querySelector('.chat-perm').getAttribute('aria-label'), 'Permission request');
  assert.match(root.querySelector('.chat-perm').textContent, /Allow <Read>\?/);
  assert.match(root.querySelector('.chat-perm pre').textContent, /<img src=x>/);
  assert.equal(root.querySelector('img'), null);
  root.querySelector('.chat-perm-allow').click(); root.querySelector('.chat-perm-deny').click();
  assert.deepEqual(replies, [['request-1', true], ['request-1', false]]);
});
