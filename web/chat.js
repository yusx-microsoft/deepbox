/* Agentbridge structured conversation renderer.
 *
 * Renders canonical structured events emitted by headless runtime adapters.
 * Loaded before app.js alongside the other local UI helpers.
 *
 * `applyEvent(state, ev)` updates the caller-owned view model in place,
 * so reducer tests do not need a DOM. `renderChat(...)` is the DOM layer.
 */
(function (global) {
  'use strict';
  const Runtime = typeof module === 'object' && module.exports
    ? require('./integrations/deeporca/runtime.js') : global.AgentBridgeDeepOrcaRuntime;
  const standardRuntime = Object.freeze({interactiveApproval:true});
  function runtimeContract(agent, capability, renderer) {
    return Runtime?.matches(agent, capability, renderer) ? Runtime : standardRuntime;
  }

  function parseEventPayload(data) {
    const events = [];
    for (const line of String(data || '').split('\n')) {
      if (!line.trim()) continue;
      try {
        const event = JSON.parse(line);
        if (event && typeof event === 'object' && !Array.isArray(event))
          events.push(event);
      } catch (_) { /* one bad durable row must not hide later valid rows */ }
    }
    return events;
  }

  function initialChatState() {
    return {
      items: [],          // ordered: {kind, ...} render items
      pendingPermission: null, // {request_id, tool, input} or null
      status: null,       // last status note
      config: {},          // connector-confirmed model/reasoning values
      configured: false,   // true once a session.config event is observed
      renderer: null,
      run: null,
      _openAssistant: null, // index of the assistant bubble accreting deltas
    };
  }

  // Fold one canonical event into state. Returns the SAME state object mutated
  // (callers treat it as owned) — cheap and enough for our append-only UI.
  function applyEvent(state, ev) {
    if (!ev || typeof ev !== 'object') return state;
    if (Runtime?.foldEvent(state, ev)) return state;
    switch (ev.ev) {
      case 'turn.start':
        state._openAssistant = null;
        state.run = {state:'running', turn_id:ev.turn_id};
        break;
      case 'thinking.delta': {
        let item = state.items[state.items.length - 1];
        if (!item || item.kind !== 'thinking' || item.turn_id !== ev.turn_id) {
          item = {kind:'thinking', text:'', turn_id:ev.turn_id}; state.items.push(item);
        }
        item.text += ev.text || '';
        break;
      }
      case 'status':
        state.status = ev.subtype || ev.note || ev.model || 'status';
        break;
      case 'message.delta': {
        let idx = state._openAssistant;
        if (ev.message_id) {
          const found = state.items.findIndex(item => item.kind === 'assistant' &&
            item.message_id === ev.message_id && item.turn_id === ev.turn_id);
          idx = found < 0 ? null : found;
        }
        if (idx == null) {
          state.items.push({ kind: 'assistant', text: '', ...(ev.message_id ? {message_id:ev.message_id, turn_id:ev.turn_id} : {}) });
          idx = state.items.length - 1;
          state._openAssistant = idx;
        }
        state.items[idx].text += (ev.text || '');
        break;
      }
      case 'message': {
        // A complete assistant message. If we were streaming and it carries no
        // text (a message_stop marker), just close the open bubble.
        if (ev.text) {
          if (state._openAssistant != null) {
            // Prefer the streamed text already accreted; only replace if empty.
            const cur = state.items[state._openAssistant];
            if (!cur.text) cur.text = ev.text;
          } else {
            state.items.push({ kind: 'assistant', text: ev.text });
          }
        }
        if (ev.final) state._openAssistant = null;
        break;
      }
      case 'tool.call': {
        state._openAssistant = null;
        // A streaming tool start is followed by the provider's complete tool
        // snapshot. Treat the shared tool_id as an update, not a second card.
        let pending = null;
        if (ev.tool_id) {
          for (let i = state.items.length - 1; i >= 0; i--) {
            const it = state.items[i];
            if (it.kind === 'tool' && it.tool_id === ev.tool_id && it.result == null &&
                (!ev.turn_id || it.turn_id === ev.turn_id)) {
              pending = it; break;
            }
          }
        }
        if (pending) {
          if (ev.tool) pending.tool = ev.tool;
          if (ev.input !== undefined) pending.input = ev.input;
          pending.streaming = !!ev.streaming;
        } else {
          state.items.push({
            kind: 'tool', tool: ev.tool, tool_id: ev.tool_id,
            ...(ev.turn_id ? {turn_id:ev.turn_id} : {}),
            input: ev.input, streaming: !!ev.streaming, result: null,
            is_error: false,
          });
        }
        break;
      }
      case 'tool.result': {
        // Attach to the matching tool card if present, else append a card.
        let matched = null;
        for (let i = state.items.length - 1; i >= 0; i--) {
          const it = state.items[i];
          if (it.kind === 'tool' && it.tool_id === ev.tool_id && it.result == null &&
              (!ev.turn_id || it.turn_id === ev.turn_id)) {
            matched = it; break;
          }
        }
        if (matched) {
          matched.result = ev.content || '';
          matched.is_error = !!ev.is_error;
          if (ev.code) matched.code = ev.code;
          if (ev.status) matched.status = ev.status;
          if (ev.truncated) matched.truncated = true;
          if (Number.isFinite(ev.original_bytes)) matched.original_bytes = ev.original_bytes;
        } else {
          state.items.push({
            kind: 'tool', tool: null, tool_id: ev.tool_id,
            ...(ev.turn_id ? {turn_id:ev.turn_id} : {}),
            input: null, result: ev.content || '', is_error: !!ev.is_error,
            ...(ev.code ? {code:ev.code} : {}), ...(ev.status ? {status:ev.status} : {}),
            ...(ev.truncated ? {truncated:true, original_bytes:ev.original_bytes} : {}),
          });
        }
        break;
      }
      case 'permission.ask':
        state.pendingPermission = {
          request_id: ev.request_id, tool: ev.tool, input: ev.input,
        };
        break;
      case 'turn.end': {
        state._openAssistant = null;
        if (ev.turn_id || state.run) state.run = {state:ev.status || ev.subtype || 'completed', turn_id:ev.turn_id};
        if (ev.turn_id && state.items.some(item => item.kind === 'turn' && item.turn_id === ev.turn_id)) break;
        if (state.items.length && state.items[state.items.length - 1].kind === 'turn') break;
        let hasAssistant = false;
        for (let i = state.items.length - 1; i >= 0; i--) {
          if (state.items[i].kind === 'turn' || state.items[i].kind === 'user') break;
          if (state.items[i].kind === 'assistant' && state.items[i].text) {
            hasAssistant = true;
            break;
          }
        }
        state.items.push({
          kind: 'turn', is_error: !!ev.is_error, cost_usd: ev.cost_usd,
          ...(ev.usage ? {usage:ev.usage} : {}),
          ...(ev.native ? {native:ev.native} : {}),
          ...(ev.turn_id ? {turn_id:ev.turn_id} : {}),
          ...(ev.status ? {status:ev.status} : {}), ...(ev.subtype ? {subtype:ev.subtype} : {}),
          // Some native protocols repeat the completed assistant text in the
          // lifecycle result. Keep it only as a fallback when no message arrived.
          result: hasAssistant ? null : (ev.result || null),
        });
        break;
      }
      case 'session.config':
        if (typeof ev.renderer === 'string') state.renderer = ev.renderer;
        if (!runtimeContract(null, null, state.renderer).interactiveApproval) state.pendingPermission = null;
        // Each event is the connector-confirmed effective scalar set for this
        // turn. Replace rather than merge so returning to a runtime default can
        // clear a previously selected option.
        state.config = ev.options && typeof ev.options === 'object'
          ? Object.assign({}, ev.options) : {};
        state.configured = true;
        break;
      case 'user.echo': {
        const text = ev.text || '';
        // A live browser renders its own turn immediately. Reconcile the
        // connector echo with that optimistic row; a restore has no local row
        // and therefore appends the durable user event.
        let local = null;
        if (ev.client_input_id) local = state.items.find(item => item.kind === 'user' && item.client_input_id === ev.client_input_id);
        for (let i = state.items.length - 1; i >= 0; i--) {
          if (local) break;
          const item = state.items[i];
          if (item.kind === 'user' && item.local && item.text === text && !ev.client_input_id && !item.client_input_id) {
            local = item;
            break;
          }
        }
        if (local) {
          local.local = false;
          local.attachments = Array.isArray(ev.attachments) ? ev.attachments : local.attachments;
        } else {
          state.items.push({
            kind: 'user', text,
            attachments: Array.isArray(ev.attachments) ? ev.attachments : [],
            local: false,
            ...(ev.client_input_id ? {client_input_id:ev.client_input_id} : {}),
          });
        }
        break;
      }
      case 'error':
        state.items.push({ kind: 'error', text: ev.message || 'error' });
        break;
      default:
        break;
    }
    return state;
  }

  // A restore frame is an authoritative snapshot of the bounded durable event
  // window. Rebuild from an empty state instead of appending it to live rows.
  function foldEventPayload(state, payload, replace) {
    const next = replace ? initialChatState() : (state || initialChatState());
    const events = parseEventPayload(payload);
    for (const event of events) applyEvent(next, event);
    return { state: next, events };
  }

  // Append a local user turn immediately (0-RTT echo) before the agent replies.
  function appendUserTurn(state, text, attachments, clientInputId) {
    state._openAssistant = null;
    state.items.push({
      kind: 'user', text: text,
      attachments: Array.isArray(attachments) ? attachments : [],
      local: true,
      ...(clientInputId ? {client_input_id:clientInputId} : {}),
    });
    return state;
  }

  // Normalize the adapter-owned capability blob into a small generic control
  // schema. Runtime IDs never appear here: a new adapter can add these widgets
  // without a frontend code change.
  function controlsFromCapability(capability) {
    let features = capability && capability.features ? capability.features : capability;
    let models = capability && capability.models;
    if (capability && Number(capability.schema_version) >= 2 &&
        Array.isArray(capability.surfaces)) {
      const surface = capability.surfaces.find(item => item.default) ||
        capability.surfaces.find(item => item.id === 'structured') || capability.surfaces[0];
      features = surface && surface.features;
    }
    if (!features || !Array.isArray(features.controls)) return [];
    const seen = new Set();
    const controls = [];
    for (const raw of features.controls.slice(0, 16)) {
      if (!raw || typeof raw !== 'object') continue;
      const key = typeof raw.key === 'string' ? raw.key.trim() : '';
      const kind = raw.kind;
      if (!/^[a-z][a-z0-9_]{0,63}$/.test(key) || seen.has(key) ||
          (kind !== 'select' && kind !== 'file')) continue;
      const control = {
        key,
        kind,
        label: typeof raw.label === 'string' && raw.label.trim()
          ? raw.label.trim().slice(0, 80) : key,
        scope: raw.scope === 'session' ? 'session' : 'turn',
      };
      if (kind === 'select') {
        control.choices = Array.isArray(raw.choices)
          ? raw.choices.filter((value) => typeof value === 'string' && value.length <= 200)
            .slice(0, 128) : [];
        if (key === 'model' && models && Array.isArray(models.items)) {
          const discovered = models.items.map(item => item && item.id)
            .filter(value => typeof value === 'string' && value.length <= 200)
            .slice(0, 128);
          if (discovered.length) control.choices = discovered;
          control.allow_custom = models.allow_custom === true;
        } else {
          control.allow_custom = raw.allow_custom === true;
        }
        if (!control.choices.length && !control.allow_custom) continue;
      } else {
        control.accept = typeof raw.accept === 'string' ? raw.accept.slice(0, 500) : '';
        control.max_files = Math.max(1, Math.min(8, Number(raw.max_files) || 1));
        control.max_total_bytes = Math.max(1, Math.min(8 * 1024 * 1024,
          Number(raw.max_total_bytes) || 1024 * 1024));
      }
      seen.add(key);
      controls.push(control);
    }
    return controls;
  }

  function reconcileControlValues(controls, values, confirmed) {
    const next = Object.assign({}, values && typeof values === 'object' ? values : {});
    confirmed = confirmed && typeof confirmed === 'object' ? confirmed : {};
    for (const control of controls || []) {
      if (control.kind !== 'select') continue;
      const value = confirmed[control.key];
      const custom = control.allow_custom && typeof value === 'string' &&
        value.length > 0 && value.length <= 200 && !/[\x00-\x1f]/.test(value);
      if (control.choices.includes(value) || custom) next[control.key] = value;
      else delete next[control.key];
    }
    return next;
  }

  function buildTurnOptions(controls, values, attachments) {
    const result = {};
    values = values && typeof values === 'object' ? values : {};
    attachments = attachments && typeof attachments === 'object' ? attachments : {};
    for (const control of controls || []) {
      if (control.kind === 'select') {
        const value = values[control.key];
        const custom = control.allow_custom && typeof value === 'string' &&
          value.length > 0 && value.length <= 200 && !/[\x00-\x1f]/.test(value);
        if (control.choices.includes(value) || custom) result[control.key] = value;
      } else if (control.kind === 'file') {
        const files = attachments[control.key];
        if (Array.isArray(files) && files.length) result[control.key] = files;
      }
    }
    return result;
  }

  // --- DOM rendering (browser only) ---------------------------------------

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function renderChat(container, state, handlers) {
    if (!container) return;
    handlers = handlers || {};
    container.textContent = '';
    const log = el('div', 'chat-log');
    for (const it of state.items) {
      if (it.kind === 'user') {
        log.appendChild(messageLine('user', it.text, it.attachments));
      } else if (it.kind === 'assistant') {
        log.appendChild(messageLine('assistant', it.text));
      } else if (it.kind === 'tool') {
        log.appendChild(toolCard(it));
      } else if (it.kind === 'turn') {
        // Keep canonical turn/cost history; only failed turns need a visible boundary.
        if (it.is_error) log.appendChild(messageLine('error', 'Turn failed' + (it.result ? ': ' + it.result : '')));
        else if (it.result) log.appendChild(messageLine('assistant', it.result));
      } else if (it.kind === 'error') {
        log.appendChild(messageLine('error', it.text));
      } else if (it.kind === 'thinking') {
        const card = el('details', 'chat-thinking');
        card.appendChild(el('summary', '', 'Thinking'));
        card.appendChild(el('div', 'chat-text', it.text)); log.appendChild(card);
      }
    }
    container.appendChild(log);
    if (state.pendingPermission) {
      container.appendChild(permissionModal(state.pendingPermission, handlers));
    }
    // Auto-scroll to newest.
    log.scrollTop = log.scrollHeight;
  }

  function messageLine(role, text, attachments) {
    const b = el('div', 'chat-msg chat-' + role);
    b.setAttribute('role', role === 'error' ? 'alert' : 'group');
    b.setAttribute('aria-label', role === 'user' ? 'User message' : role === 'assistant' ? 'Agent output' : 'Error');
    b.appendChild(el('div', 'chat-role', role === 'user' ? 'You' : role === 'assistant' ? 'Agent' : 'Error'));
    b.appendChild(el('div', 'chat-text', text || ''));
    if (Array.isArray(attachments) && attachments.length) {
      const row = el('div', 'chat-message-files');
      row.setAttribute('role', 'group');
      row.setAttribute('aria-label', 'Attachments');
      for (const file of attachments) {
        if (!file || typeof file.name !== 'string') continue;
        row.appendChild(el('span', 'chat-message-file', file.name));
      }
      b.appendChild(row);
    }
    return b;
  }

  function toolCard(it) {
    const c = el('div', 'chat-tool' + (it.is_error ? ' chat-tool-error' : ''));
    c.setAttribute('role', 'group');
    c.setAttribute('aria-label', (it.is_error ? 'Tool error: ' : 'Tool: ') + (it.tool || 'tool'));
    c.appendChild(el('div', 'chat-tool-name', (it.tool || 'tool') +
      (it.streaming && it.result == null ? ' \u2026' : '')));
    if (it.input != null) {
      c.appendChild(el('pre', 'chat-tool-input',
        typeof it.input === 'string' ? it.input : JSON.stringify(it.input, null, 2)));
    }
    if (it.result != null) {
      c.appendChild(el('pre', 'chat-tool-result', String(it.result)));
    }
    return c;
  }

  function permissionModal(p, handlers) {
    const m = el('div', 'chat-perm');
    m.setAttribute('role', 'group');
    m.setAttribute('aria-label', 'Permission request');
    m.appendChild(el('div', 'chat-perm-title',
      'Allow ' + (p.tool || 'tool') + '?'));
    if (p.input != null) {
      m.appendChild(el('pre', 'chat-perm-input',
        typeof p.input === 'string' ? p.input : JSON.stringify(p.input, null, 2)));
    }
    const row = el('div', 'chat-perm-actions');
    const allow = el('button', 'chat-perm-allow', 'Allow');
    const deny = el('button', 'chat-perm-deny', 'Deny');
    allow.type = deny.type = 'button';
    allow.addEventListener('click', () => handlers.onPermission &&
      handlers.onPermission(p.request_id, true));
    deny.addEventListener('click', () => handlers.onPermission &&
      handlers.onPermission(p.request_id, false));
    row.appendChild(allow); row.appendChild(deny);
    m.appendChild(row);
    return m;
  }

  // Fixed local allowlist: descriptors never determine script URLs. Lazy loading
  // avoids introducing boot dependencies for CLI-only workspaces.
  const localModules = {
    'deeporca-chat-v1': {file:'integrations/deeporca/chat.js', global:'DeepOrcaChat', renderer:true},
    'runtime-catalog': {file:'integrations/deeporca/agent-ui.js', global:'AgentBridgeRuntimeCatalog'},
  };
  function supportsRenderer(id) { return Object.hasOwn(localModules, id || '') && !!localModules[id].renderer; }
  const loadingModules = new Map();
  function loadLocalModule(id) {
    const entry = Object.prototype.hasOwnProperty.call(localModules, id) ? localModules[id] : null;
    if (!entry) return Promise.reject(new Error('Unsupported renderer'));
    if (typeof module !== 'undefined' && module.exports) return Promise.resolve(require('./' + entry.file));
    if (global[entry.global]) return Promise.resolve(global[entry.global]);
    if (!loadingModules.has(id)) loadingModules.set(id, new Promise((resolve, reject) => {
      const script = global.document.createElement('script');
      script.src = '/static/' + entry.file + '?v=deeporca-2';
      script.onload = () => global[entry.global] ? resolve(global[entry.global]) : reject(new Error('Local renderer unavailable'));
      script.onerror = () => reject(new Error('Local renderer unavailable. Reload to retry.'));
      global.document.head.appendChild(script);
    }).catch(error => { loadingModules.delete(id); throw error; }));
    return loadingModules.get(id);
  }
  function rendererId(agent, capability) {
    const surface = capability?.surfaces?.find(item => item.id === 'structured');
    return agent?.renderer || surface?.features?.renderer || capability?.features?.renderer || null;
  }
  const api = {
    loadLocalModule, rendererId, supportsRenderer, runtimeContract,
    parseEventPayload, initialChatState, applyEvent, foldEventPayload,
    appendUserTurn, renderChat,
    controlsFromCapability, reconcileControlValues, buildTurnOptions,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  global.AgentBridgeChat = global.DeepboxChat = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
