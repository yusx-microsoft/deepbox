/* One independently attached session surface. Closing a pane never ends a session. */
(function (environment, factory) {
  'use strict';
  const api = typeof module === 'object' && module.exports
    ? factory(environment, require('./ui.js'), require('./chat.js'), require('./collaboration.js'), require('./replay.js'))
    : factory(environment, environment.AgentBridgeUI || environment.DeepboxUI,
      environment.AgentBridgeChat || environment.DeepboxChat,
      environment.AgentBridgeCollaboration || environment.DeepboxCollaboration,
      environment.AgentBridgeReplay || environment.DeepboxReplay);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else environment.AgentBridgePane = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (environment, UI, Chat, Collaboration, Replay) {
  'use strict';

  const OPERATORS = ['operator', 'admin', 'owner'];
  const ADMINS = ['admin', 'owner'];
  const MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024;
  const XTERM_THEME = {
    background: '#10120f', foreground: '#cfd7c8', cursor: '#a3c57f',
    cursorAccent: '#000000', selectionBackground: 'rgba(59,177,168,0.30)',
    black: '#0b0d10', red: '#e5674f', green: '#4bbf7a', yellow: '#d8a63a',
    blue: '#5aa9e6', magenta: '#b98ae0', cyan: '#3bb1a8', white: '#c9d1d9',
    brightBlack: '#6b7480', brightRed: '#f08a74', brightGreen: '#6fd598',
    brightYellow: '#e6bd5f', brightBlue: '#7fbef0', brightMagenta: '#cda6ec',
    brightCyan: '#5fc7bd', brightWhite: '#f2f5f8',
  };

  function validSurface(value) {
    return value === 'terminal' || value === 'structured' ? value : null;
  }

  function displayName(value) {
    return typeof value === 'string'
      ? value.replace(/[\u0000-\u001f\u007f-\u009f\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, '').trim().slice(0, 80) || 'someone'
      : 'someone';
  }

  function createPane({ id, root, services } = {}) {
    if (!root || !root.ownerDocument || !services || typeof services.api !== 'function')
      throw new TypeError('createPane needs a DOM root and services.api.');
    if (id == null || String(id) === '') throw new TypeError('A unique pane id is required.');
    if (!UI || !Chat || !Collaboration || !Replay) throw new Error('Session UI helpers could not load.');
    id = String(id);
    const document = root.ownerDocument;
    const window = document.defaultView || environment;
    const namespace = 'ab-pane-' + Array.from(id, c => c.codePointAt(0).toString(16)).join('-');
    let epoch = 0, closed = false, target = null, status = 'empty', statusText = '';
    let restoreRequested = false, lastChange = '', abortController = null;
    let terminal = null, fit = null, terminalSubscription = null, resizeObserver = null;
    let resizeSocket = null, resizeSize = '';
    let socket = null, inputSender = null, wantOpen = false, liveActive = false;
    let reconnectTimer = null, reconnectDelay = 500, heartbeat = null;
    let collaboration = null, keyboardRequester = null, endPending = false;
    let chat = null, controls = [], controlValues = {}, attachments = {}, readingFiles = false;
    let turnPending = false; // Presentation only; never persisted or folded into canonical events.
    let announcedCapability = null;
    let persistedRenderer = null, rendererView = null, rendererLoading = false, rendererFailed = false;
    let inputSequence = 0;
    let nativeSubmission = null;
    let recording = null, replayTimer = null, replayPlaying = false, replaySpeed = 1, replayCursor = 0, replayGeneration = 0;
    let nodes = {}, listeners = [];
    const readers = new Set();

    function current(view) { return !closed && view === epoch; }
    function workspace() { return services.getWorkspace ? services.getWorkspace() : null; }
    function canOperate() {
      return !closed && !!target && OPERATORS.includes(workspace()?.role)
        && (!collaboration || collaboration.canOperate);
    }
    function canManageRecording() { return !closed && ADMINS.includes(workspace()?.role); }
    function connected() {
      return !closed && target?.kind === 'live' && !!target.sessionId && wantOpen && liveActive
        && !!socket && socket.readyState === 1;
    }
    function canWriteChat() {
      return connected() && target.surface === 'structured' && canOperate()
        && Collaboration.canSendMessage(collaboration);
    }
    function canWriteTerminal() {
      return connected() && target.surface === 'terminal' && canOperate()
        && Collaboration.canSendInput(collaboration);
    }
    function canEndSession() {
      if (target?.surface === 'terminal') return canWriteTerminal();
      return connected() && canOperate() && !!collaboration
        && (Collaboration.canSendInput(collaboration)
          || ADMINS.includes(workspace()?.role) && ADMINS.includes(collaboration.role));
    }
    function getState() {
      const localRole = OPERATORS.indexOf(workspace()?.role);
      const liveRole = collaboration ? OPERATORS.indexOf(collaboration.role) : localRole;
      const keyboardLive = connected() && target.surface === 'terminal';
      const keyboardOwned = canWriteTerminal();
      const pending = !closed && target?.kind === 'live' && target.surface === 'structured' && turnPending;
      return {
        id, title: target?.title || '', agentId: target?.agentId || null,
        sessionId: target?.sessionId || null, surface: target?.surface || null,
        kind: target?.kind || null, status, statusText, canOperate: canOperate(),
        role: OPERATORS[Math.min(localRole, liveRole)] || 'viewer',
        canEndSession: canEndSession(), replay: target?.kind === 'replay',
        readOnly: !(target?.surface === 'structured' ? canWriteChat() : canWriteTerminal()),
        keyboardOwned, keyboardBusy: !!(keyboardLive && collaboration?.heldByAnyone && !keyboardOwned),
        keyboardHolder: keyboardLive && collaboration?.heldByAnyone ? displayName(collaboration.holderUsername) : '',
        keyboardPending: !!(keyboardLive && !collaboration),
        keyboardRequestPending: !!(keyboardOwned && keyboardRequester?.id != null),
        pending, turnPending: pending,
        permissionPending: !!(!closed && target?.kind === 'live' && chat?.pendingPermission),
      };
    }
    // Persist a whitelist, not this factory's mutable state. Operation flags are deliberately transient.
    function snapshot() {
      if (closed || !target) return null;
      const saved = { kind: target.kind, agentId: target.agentId, title: target.title };
      if (target.sessionId) saved.sessionId = target.sessionId;
      if (target.surface) saved.surface = target.surface;
      return saved;
    }
    function notify() {
      const state = getState(), key = JSON.stringify(state);
      if (key === lastChange) return;
      lastChange = key;
      if (services.onChange) services.onChange(state);
    }
    function setStatus(value, text) {
      status = value;
      statusText = text || value;
      syncAccess();
      notify();
    }
    function element(tag, name, className, text) {
      const node = document.createElement(tag);
      if (name) node.setAttribute('data-ui', name);
      if (className) node.className = className;
      if (text != null) node.textContent = text;
      return node;
    }
    function listen(node, type, callback) {
      const view = epoch;
      const handler = event => { if (current(view)) return callback(event); };
      node.addEventListener(type, handler);
      listeners.push(() => node.removeEventListener(type, handler));
    }
    function button(name, text, callback) {
      const node = element('button', name, 'ghost', text);
      node.type = 'button';
      // Detached, replaced rows need no global listener. Epoch guards also make retained callbacks inert.
      const view = epoch;
      node.onclick = event => { if (current(view)) return callback(event); };
      return node;
    }
    function reportError(message) {
      if (closed || !nodes.error) return;
      nodes.error.textContent = String(message || 'The session request failed.');
      nodes.error.hidden = false;
    }
    function composerError(message) {
      if (nodes.composerError) nodes.composerError.textContent = message || '';
    }
    function clearError() {
      if (nodes.error) { nodes.error.textContent = ''; nodes.error.hidden = true; }
    }
    function mountShell() {
      root.textContent = '';
      // The workbench border/statusbar owns visible status and keyboard controls.
      nodes.status = element('span', 'status', 'visually-hidden', statusText);
      nodes.status.setAttribute('role', 'status');
      nodes.status.setAttribute('aria-live', 'polite');
      nodes.status.setAttribute('aria-atomic', 'true');
      nodes.error = element('p', 'error', 'pane-error');
      nodes.error.hidden = true;
      nodes.error.setAttribute('role', 'alert');
      nodes.body = element('div', 'content', 'pane-session-content');
      root.append(nodes.status, nodes.error, nodes.body);
    }
    function stopHeartbeat() {
      if (heartbeat !== null) window.clearInterval(heartbeat);
      heartbeat = null;
    }
    function stopReconnect() {
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    function pauseReplay() {
      ++replayGeneration;
      replayPlaying = false;
      if (replayTimer !== null) window.clearTimeout(replayTimer);
      replayTimer = null;
      updateReplayUI();
    }
    function detachSocket() {
      stopHeartbeat();
      if (inputSender) inputSender.close();
      inputSender = null;
      const old = socket;
      socket = null;
      resizeSocket = null; resizeSize = '';
      if (old) {
        old.onopen = old.onmessage = old.onerror = old.onclose = null;
        try { old.close(); } catch (_) { /* detach only; NEVER terminate */ }
      }
      collaboration = null;
      keyboardRequester = null;
    }
    function disposeTerminal() {
      if (resizeObserver) resizeObserver.disconnect();
      resizeObserver = null;
      window.removeEventListener?.('resize', resize);
      if (terminalSubscription) terminalSubscription.dispose();
      terminalSubscription = null;
      const old = terminal;
      terminal = null; fit = null;
      if (old) { try { old.dispose(); } catch (_) { /* already disposed */ } }
    }
    function reset() {
      ++epoch; // Invalidate REST, dialogs, file reads, and already queued WS/DOM callbacks first.
      wantOpen = false; liveActive = false;
      stopReconnect(); pauseReplay(); detachSocket(); disposeTerminal();
      if (abortController) abortController.abort();
      abortController = null;
      for (const reader of readers) { try { reader.abort(); } catch (_) {} }
      readers.clear();
      for (const remove of listeners) remove();
      listeners = [];
      nodes = {};
      chat = null; controls = []; controlValues = {}; attachments = {}; readingFiles = false;
      turnPending = false;
      nativeSubmission = null;
      announcedCapability = null;
      rendererView?.destroy(); rendererView = null;
      persistedRenderer = null; rendererLoading = false; rendererFailed = false;
      recording = null; replaySpeed = 1; replayCursor = 0;
      reconnectDelay = 500; endPending = false;
      root.textContent = '';
    }
    function request(path, options) {
      return services.api(path, Object.assign({}, options, abortController ? { signal: abortController.signal } : {}));
    }
    function agentPath() { return UI.agentApiPath(target.agentId) + '/sessions'; }
    function capability() {
      if (announcedCapability) return announcedCapability;
      const found = services.findAgent ? services.findAgent(target?.agentId) : null;
      return UI.findRuntimeCapability(found?.box?.capabilities, found?.agent?.runtime);
    }
    function selectedRenderer() {
      const found = services.findAgent?.(target?.agentId);
      return chat?.renderer || persistedRenderer || Chat.rendererId(found?.agent, capability());
    }
    function runtimeContract() {
      return Chat.runtimeContract(services.findAgent?.(target?.agentId)?.agent, capability(), selectedRenderer());
    }
    function canContinueNative(session) {
      return runtimeContract().explicitContinuation
        && canOperate() && session?.state === 'inactive'
        && session.surface === 'structured' && session.available !== false;
    }
    function unavailable(message, allowReplay = true) {
      liveActive = false; wantOpen = false; turnPending = false;
      stopReconnect(); detachSocket();
      setStatus('unavailable', 'Session unavailable');
      reportError(message);
      const actions = element('div', 'unavailable-actions', 'pane-session-actions');
      if (allowReplay && target.sessionId)
        actions.appendChild(button('open-replay', 'View recording', () => open({ ...snapshot(), kind: 'replay' })));
      if (canOperate())
        actions.appendChild(button('open-live', 'Open live session', () => open({
          kind: 'live', agentId: target.agentId, title: target.title, surface: target.surface,
        })));
      nodes.body.appendChild(actions);
    }

    async function open(next) {
      if (closed) return getState();
      if (!next || !['live', 'history', 'replay'].includes(next.kind) || !next.agentId)
        throw new TypeError('A pane target needs kind and agentId.');
      reset();
      const view = epoch;
      const found = services.findAgent ? services.findAgent(String(next.agentId)) : null;
      target = {
        kind: next.kind, agentId: String(next.agentId),
        title: String(next.title || found?.agent?.display_name || found?.agent?.handle || next.agentId),
        surface: validSurface(next.surface),
      };
      if (next.sessionId && (next.forceNew !== true || next.restore === true)) target.sessionId = String(next.sessionId);
      restoreRequested = next.restore === true;
      if (!target.surface && target.kind !== 'replay')
        target.surface = validSurface(UI.preferredSurface(capability())) || 'terminal';
      if (typeof window.AbortController === 'function') abortController = new window.AbortController();
      status = 'opening'; statusText = target.kind === 'live' ? 'Opening session…' : 'Loading ' + target.kind + '…';
      mountShell();
      setStatus(status, statusText);
      try {
        if (target.kind === 'live') await openLive(view, next.forceNew === true, !!validSurface(next.surface), next.continueNative === true && !restoreRequested);
        else if (target.kind === 'history') await loadHistory(view);
        else await loadReplay(view);
      } catch (error) {
        if (current(view)) {
          setStatus('error', 'Could not open ' + target.kind);
          reportError(error.message || 'The request failed. Use Reconnect to retry.');
        }
      }
      return getState();
    }
    async function openLive(view, forceNew, explicitSurface, continueNative = false) {
      // Restoration is navigation, not consent to spawn a new model, even if forceNew was persisted elsewhere.
      if (restoreRequested && !target.sessionId) {
        unavailable('No saved session ID. Choose Open live session or New session to start explicitly.', false);
        return;
      }
      let session = null, created = false;
      if (!forceNew || restoreRequested || target.sessionId) {
        const sessions = await request(agentPath());
        if (!current(view)) return;
        if (!Array.isArray(sessions)) throw new Error('Invalid session list.');
        session = target.sessionId ? sessions.find(item => item.id === target.sessionId)
          : UI.resumableSession(sessions.filter(item => item.available !== false), target.surface);
        if (target.sessionId) {
          if (!session) { unavailable('The saved session is missing. It will not be recreated automatically.'); return; }
          if (session.state !== 'live' && !(continueNative && canContinueNative(session))) {
            await open({ ...snapshot(), kind: 'replay', surface: validSurface(session.surface) || target.surface });
            return;
          }
          if (session.available === false) { unavailable('The connector or runtime for this session is unavailable.'); return; }
          if (!validSurface(session.surface) || explicitSurface && session.surface !== target.surface) {
            unavailable('This session does not have the selected surface. Choose a matching session from History.');
            return;
          }
          if (!explicitSurface) target.surface = session.surface;
        }
      }
      if (!current(view)) return;
      // Preflight the renderer BEFORE any create request. Chat never touches xterm.
      if (target.surface === 'terminal' && services.ensureTerminal) {
        setStatus('opening', 'Loading terminal renderer…');
        await services.ensureTerminal();
        if (!current(view)) return;
      }
      if (!mountSurface()) return;
      if (!session) {
        if (restoreRequested) { unavailable('The saved session cannot be resumed. Start a new session explicitly.'); return; }
        if (!canOperate()) { unavailable('Read-only: an Operator, Admin or Owner must start a session.', false); return; }
        session = await request(agentPath(), { method: 'POST', body: JSON.stringify({ surface: target.surface }) });
        if (!current(view)) return;
        created = true;
      }
      if (!session || !session.id) throw new Error('The server did not return a session ID.');
      target.sessionId = String(session.id);
      if (session.renderer) persistedRenderer = session.renderer;
      if (session.surface !== target.surface) {
        unavailable('The server returned a different or unknown session surface. Use History to select it.');
        return;
      }
      // A new persisted row is inactive until this browser sends its attach.
      if (!created && session.state && session.state !== 'live' && !(continueNative && canContinueNative(session))) {
        await open({ ...snapshot(), kind: 'replay' });
        return;
      }
      wantOpen = true; liveActive = true;
      connectSocket();
    }
    function mountSurface() {
      if (target.surface === 'structured') { mountChat(); return true; }
      const host = element('div', 'terminal', 'pane-terminal');
      nodes.terminal = host;
      nodes.body.appendChild(host);
      try {
        if (typeof window.Terminal !== 'function' || typeof window.FitAddon?.FitAddon !== 'function')
          throw new Error('The xterm terminal renderer could not load. Reload and allow the xterm scripts, or explicitly open Chat if available.');
        terminal = new window.Terminal({
          fontFamily: "'Cascadia Code','SFMono-Regular',Consolas,monospace", fontSize: 13, cursorBlink: true,
          scrollOnUserInput: true, scrollback: 5000, theme: XTERM_THEME, disableStdin: true,
        });
        fit = new window.FitAddon.FitAddon();
        terminal.loadAddon(fit);
        terminal.open(host);
        fit.fit();
        const ownTerminal = terminal, view = epoch;
        terminalSubscription = terminal.onData(data => {
          // No pane-focus gate: xterm also emits protocol responses while another pane has focus.
          if (!current(view) || ownTerminal !== terminal || !canWriteTerminal()) return;
          try { inputSender?.push(data); } catch (error) { reportError(error.message); }
        });
        listen(host, 'pointerdown', focus);
        if (typeof window.ResizeObserver === 'function') {
          resizeObserver = new window.ResizeObserver(() => { if (current(view) && ownTerminal === terminal) resize(); });
          resizeObserver.observe(root);
        } else window.addEventListener?.('resize', resize);
        syncAccess();
        return true;
      } catch (error) {
        disposeTerminal();
        setStatus('error', 'Terminal renderer unavailable');
        reportError(error.message || 'Terminal could not start. Reload to retry.');
        return false;
      }
    }
    function resize() {
      if (closed) return;
      resizeChatInput();
      if (!terminal || !fit) return;
      const bounds = root.getBoundingClientRect();
      if (!bounds.width || !bounds.height) return;
      try { fit.fit(); sendResize(); } catch (_) { /* a temporarily hidden pane has no size */ }
    }
    function sendResize(force = false) {
      if (!canWriteTerminal() || !terminal) return;
      const size = terminal.cols + ':' + terminal.rows;
      if (!force && resizeSocket === socket && resizeSize === size) return;
      if (sendFrame('resize', { cols: terminal.cols, rows: terminal.rows })) {
        resizeSocket = socket; resizeSize = size;
      }
    }
    function focus() {
      if (closed) return;
      if (terminal && target?.kind === 'live') terminal.focus();
      else if (nodes.input && !nodes.composer.hidden && !nodes.input.disabled) nodes.input.focus();
      else {
        const node = nodes.scroll || nodes.body || root;
        node.tabIndex = -1;
        node.focus();
      }
    }
    function sendFrame(type, fields) {
      if (closed || !target?.sessionId || !socket || socket.readyState !== 1) return false;
      try {
        socket.send(JSON.stringify({ type, session_id: target.sessionId, ...fields }));
        return true;
      } catch (error) { reportError(error.message || 'Session connection failed.'); return false; }
    }
    function sendPrefix() {
      // Unlike xterm protocol responses, this workbench shortcut is active-pane input.
      if (!canWriteTerminal() || !terminal) return false;
      const active = services.isActive ? services.isActive() : root.contains(document.activeElement);
      return !!(active && sendFrame('input', { data: '\u0002' }));
    }
    function connectSocket() {
      if (closed || !wantOpen || !target?.sessionId || target.kind !== 'live') return;
      stopReconnect(); detachSocket();
      liveActive = true;
      setStatus('connecting', 'Connecting…');
      const view = epoch, sessionId = target.sessionId;
      let ownSocket;
      try {
        const location = window.location || environment.location;
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        ownSocket = new window.WebSocket(protocol + '//' + location.host + '/ws/term');
      } catch (error) {
        liveActive = false;
        setStatus('error', 'Connection unavailable'); reportError(error.message);
        return;
      }
      socket = ownSocket;
      const isCurrent = () => current(view) && socket === ownSocket && target.sessionId === sessionId;
      inputSender = UI.createTerminalInputSender((data, options) => {
        if (!isCurrent() || !(target.surface === 'structured' ? canWriteChat() : canWriteTerminal())) return false;
        const clientInputId = runtimeContract().inputReceipts && options?.client_input_id;
        if (clientInputId) { options = {...options}; delete options.client_input_id; }
        ownSocket.send(JSON.stringify({ type: 'input', session_id: sessionId, data, options,
          ...(clientInputId ? {client_input_id:clientInputId} : {}) }));
        return true;
      });
      ownSocket.onopen = () => {
        if (!isCurrent() || !wantOpen) return;
        if (!sendFrame('attach', { cols: terminal?.cols || 120, rows: terminal?.rows || 30, surface: target.surface })) return;
        setStatus('connected', 'Attached · waiting for runtime');
      };
      ownSocket.onmessage = event => {
        if (!isCurrent() || !wantOpen) return;
        let frame;
        try { frame = JSON.parse(event.data); } catch (_) { reportError('Received an invalid session frame. Reconnect to retry.'); return; }
        if (!frame || typeof frame !== 'object' || Array.isArray(frame)) return;
        if (frame.session_id && frame.session_id !== sessionId) return;
        if (frame.surface && frame.surface !== target.surface) {
          unavailable('The attached session surface changed. Select a matching session from History.');
          return;
        }
        if (frame.kind === 'event' && (frame.type === 'restore' || frame.type === 'output')) {
          if (target.surface === 'structured') handleChatFrame(frame);
          return;
        }
        switch (frame.type) {
          case 'ready':
          case 'session.ready':
            liveActive = true; reconnectDelay = 500;
            if (frame.capabilities && typeof frame.capabilities === 'object' && !Array.isArray(frame.capabilities)) {
              announcedCapability = frame.capabilities;
              if (target.surface === 'structured') setupChatControls();
            }
            clearError(); setStatus('live', target.surface === 'structured' ? 'Chat ready' : 'Terminal ready');
            break;
          case 'restore':
            if (terminal) { terminal.reset(); terminal.write(frame.data || ''); }
            break;
          case 'output':
            if (terminal) terminal.write(frame.data || '');
            break;
          case 'runtime.unavailable':
            liveActive = false; turnPending = false;
            setStatus('unavailable', 'Runtime unavailable');
            reportError('Runtime unavailable: ' + String(frame.code || 'runtime_unavailable').replace(/_/g, ' ') + '. Check the connector configuration and reconnect.');
            break;
          case 'status':
            if (frame.state === 'live') { liveActive = true; setStatus('live', 'Live'); }
            else if (frame.state === 'ended' || frame.state === 'inactive') finishSession(frame);
            else if (frame.state === 'offline') {
              liveActive = false; setStatus('offline', 'Connector offline');
              reportError('The connector is offline. Reconnect the connector, then retry.');
            }
            break;
          case 'exit': finishSession(frame); break;
          case 'input_ack':
            if (runtimeContract().inputReceipts && nativeSubmission && nativeSubmission.id === frame.client_input_id) {
              if (frame.status === 'rejected') {
                const submitted = nativeSubmission;
                nativeSubmission = null; turnPending = false;
                chat.items = chat.items.filter(item => !(item.kind === 'user' && item.local && item.client_input_id === submitted.id));
                chat._openAssistant = null;
                if (!nodes.input.value) { nodes.input.value = submitted.text; resizeChatInput(); }
                renderChat();
                const uncertain = ['execution_uncertain', 'input_delivery_failed'].includes(frame.reason);
                composerError(uncertain
                  ? 'Execution outcome is uncertain. It was not resent; check possible tool side effects before submitting again.'
                  : 'Input rejected: ' + String(frame.reason || frame.code || 'not accepted').replace(/_/g, ' ') + '. Your draft has been kept.');
              } else if (frame.status === 'delivered' && frame.duplicate) {
                nativeSubmission = null; turnPending = false; renderChat();
              } else if (frame.status === 'delivered') {
                nativeSubmission.delivered = true;
              }
            }
            break;
          case 'error': reportError(frame.message || 'The session request failed.'); break;
          case 'snapshot':
          case 'collaboration': {
            const hadKeyboard = canWriteTerminal();
            collaboration = Collaboration.deriveCollaborationState(frame, services.getUser ? services.getUser() : null);
            if (!collaboration.isHolder) keyboardRequester = null;
            reconnectDelay = 500;
            syncAccess(); notify();
            if (!hadKeyboard && canWriteTerminal()) sendResize(true);
            break;
          }
          case 'keyboard_request':
            if (canWriteTerminal()) {
              keyboardRequester = { id: frame.requester_user_id, username: frame.requester_username };
              syncAccess(); notify();
            }
            break;
        }
      };
      ownSocket.onerror = () => { if (isCurrent()) reportError('Session connection failed. Check your connection or use Reconnect.'); };
      ownSocket.onclose = () => {
        if (!isCurrent()) return;
        if (runtimeContract().inputReceipts && nativeSubmission && !nativeSubmission.delivered) {
          if (!nodes.input.value) { nodes.input.value = nativeSubmission.text; resizeChatInput(); }
          composerError('Delivery is unconfirmed. Nothing was automatically resent; check restored history and possible tool effects before submitting again.');
        }
        stopHeartbeat();
        if (inputSender) inputSender.close();
        inputSender = null; collaboration = null; keyboardRequester = null; liveActive = false;
        if (!wantOpen) { syncAccess(); notify(); return; }
        setStatus('reconnecting', 'Reconnecting…');
        stopReconnect();
        reconnectTimer = window.setTimeout(() => {
          if (!isCurrent() || !wantOpen) return;
          reconnectTimer = null;
          connectSocket();
        }, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, 5000);
      };
    }
    function finishSession(frame) {
      if (terminal && frame.data) terminal.write(frame.data);
      wantOpen = false; liveActive = false; turnPending = false;
      stopReconnect(); detachSocket();
      setStatus(frame.state === 'inactive' ? 'inactive' : 'ended', 'Session ended');
      reportError('Session ended' + (frame.code != null ? ', code ' + frame.code : '') + '. Start a New session to continue; history is preserved.');
    }
    function syncHeartbeat() {
      if (!canWriteTerminal()) { stopHeartbeat(); return; }
      if (heartbeat !== null) return;
      const ownSocket = socket, view = epoch, sessionId = target.sessionId;
      heartbeat = window.setInterval(() => {
        if (!current(view) || socket !== ownSocket || target.sessionId !== sessionId) return;
        if (!canWriteTerminal()) { stopHeartbeat(); return; }
        sendFrame('keyboard_renew');
      }, 20000);
    }
    function syncAccess() {
      syncHeartbeat();
      syncChatControls();
      if (terminal) terminal.options.disableStdin = !canWriteTerminal();
      if (!nodes.status) return;
      const state = getState(), parts = [statusText];
      if (connected()) {
        if (state.keyboardRequestPending) parts.push('Keyboard handoff requested');
        else if (state.keyboardOwned) parts.push('You have the keyboard');
        else if (state.keyboardBusy) parts.push('Keyboard: ' + state.keyboardHolder);
        else if (state.keyboardPending) parts.push('Waiting for keyboard state');
        else if (target.surface === 'terminal' && canOperate()) parts.push('Keyboard free');
        else if (!state.readOnly) parts.push('Shared chat');
      }
      if (state.readOnly && target?.kind === 'live') parts.push('Read-only');
      if (state.pending) parts.push('Turn pending');
      if (state.permissionPending) parts.push('Permission requested');
      const text = parts.join(' · ');
      if (nodes.status.textContent !== text) nodes.status.textContent = text;
    }
    function requestKeyboard() {
      return !!(connected() && target.surface === 'terminal' && canOperate() && collaboration?.canRequest
        && sendFrame('keyboard_acquire'));
    }
    function releaseKeyboard() { return !!(canWriteTerminal() && sendFrame('keyboard_release')); }
    function handoffKeyboard() {
      if (!canWriteTerminal() || !keyboardRequester || keyboardRequester.id == null) return false;
      if (!sendFrame('keyboard_handoff', { target_user_id: keyboardRequester.id })) return false;
      keyboardRequester = null; syncAccess(); notify(); return true;
    }

    function mountChat() {
      chat = Chat.initialChatState();
      const surface = element('div', 'chat', 'chat-surface');
      nodes.rendererNote = element('div', 'chat-renderer-notice', 'chat-renderer-notice');
      nodes.rendererNote.setAttribute('role', 'status');
      nodes.rendererNote.hidden = true;
      nodes.scroll = element('div', 'chat-scroll', 'chat-scroll');
      nodes.scroll.setAttribute('aria-label', 'Session transcript');
      nodes.composer = element('div', 'chat-composer', 'chat-composer');
      nodes.composer.hidden = target.kind !== 'live';
      nodes.controls = element('div', 'chat-controls', 'chat-controls');
      nodes.tray = element('div', 'chat-file-tray', 'chat-file-tray');
      const form = element('form', 'chat-form', 'chat-form');
      nodes.input = element('textarea', 'chat-input', 'chat-input');
      nodes.input.rows = 2;
      nodes.input.setAttribute('aria-label', 'Message the agent');
      nodes.input.title = 'Enter to send · Shift+Enter for newline';
      nodes.send = element('button', 'chat-send', 'chat-send', 'Send');
      nodes.send.type = 'submit';
      nodes.send.setAttribute('aria-label', 'Send message');
      nodes.send.title = 'Send message (Enter)';
      nodes.interrupt = button('chat-interrupt', 'Interrupt', interrupt);
      nodes.interrupt.className = 'ghost chat-interrupt';
      nodes.interrupt.title = 'Interrupt the pending turn';
      form.append(nodes.input, nodes.send, nodes.interrupt);
      nodes.composerError = element('div', 'chat-composer-error', 'chat-composer-error');
      nodes.composerError.setAttribute('role', 'status');
      nodes.composer.append(nodes.controls, nodes.tray, form, nodes.composerError);
      surface.append(nodes.rendererNote, nodes.scroll, nodes.composer);
      nodes.body.appendChild(surface);
      let composing = false;
      listen(form, 'submit', event => { event.preventDefault(); if (!composing) sendChatMessage(); });
      listen(nodes.input, 'compositionstart', () => { composing = true; });
      listen(nodes.input, 'compositionend', () => { composing = false; resizeChatInput(); });
      listen(nodes.input, 'input', resizeChatInput);
      listen(nodes.input, 'keydown', event => {
        if (event.key === 'Enter' && !event.shiftKey && !composing && !event.isComposing && event.keyCode !== 229) {
          event.preventDefault(); sendChatMessage();
        }
      });
      if (target.kind === 'live') setupChatControls();
      resizeChatInput();
      renderChat();
    }
    function resizeChatInput() {
      const input = nodes.input;
      if (!input) return;
      const style = window.getComputedStyle?.(input);
      const lineHeight = parseFloat(style?.lineHeight) || 20;
      const padding = (parseFloat(style?.paddingTop) || 0) + (parseFloat(style?.paddingBottom) || 0);
      const border = (parseFloat(style?.borderTopWidth) || 0) + (parseFloat(style?.borderBottomWidth) || 0);
      const min = Math.ceil(2 * lineHeight + padding + border), max = Math.ceil(6 * lineHeight + padding + border);
      input.style.boxSizing = 'border-box';
      input.style.minHeight = min + 'px'; input.style.maxHeight = max + 'px';
      input.style.height = '0px'; // Measure wrapped content without retaining the previous height.
      const height = input.value ? Math.max(min, input.scrollHeight + border) : min;
      input.style.height = Math.min(max, height) + 'px';
      input.style.overflowY = height > max ? 'auto' : 'hidden';
    }
    function setupChatControls() {
      const selected = UI.capabilityForSurface(capability(), 'structured');
      // Project the selected surface; the renderer also supports legacy single-surface schemas.
      const nextControls = Chat.controlsFromCapability(selected ? { features: selected.features, models: selected.models } : null)
        .filter(control => runtimeContract().interactiveApproval || !/permission|approval/i.test(control.key));
      if (nodes.controls.childElementCount && JSON.stringify(controls) === JSON.stringify(nextControls)) return;
      const savedAttachments = attachments;
      controls = nextControls;
      attachments = {};
      if (chat.configured) controlValues = Chat.reconcileControlValues(controls, controlValues, chat.config);
      if (readingFiles) {
        for (const reader of readers) { try { reader.abort(); } catch (_) {} }
        composerError('Session controls changed. Reattach any files that were still loading.');
      }
      nodes.controls.textContent = '';
      for (const control of controls) {
        if (control.kind === 'select') {
          const label = element('label', null, 'chat-control');
          label.appendChild(element('span', null, null, control.label));
          const input = element(control.allow_custom ? 'input' : 'select', 'chat-control');
          input.setAttribute('data-chat-control', control.key);
          input.setAttribute('aria-label', control.label);
          let options = input;
          if (control.allow_custom) {
            const list = element('datalist');
            list.id = namespace + '-choices-' + control.key;
            input.setAttribute('list', list.id);
            input.placeholder = 'Runtime default or custom value';
            options = list;
            label.append(input, list);
          } else {
            const fallback = element('option', null, null, 'Runtime default');
            fallback.value = ''; input.appendChild(fallback); label.appendChild(input);
          }
          for (const choice of control.choices) {
            const option = element('option', null, null, choice);
            option.value = choice; options.appendChild(option);
          }
          listen(input, 'input', () => {
            if (canWriteChat() && !input.disabled) controlValues[control.key] = input.value.trim();
          });
          nodes.controls.appendChild(label);
        } else {
          const files = savedAttachments[control.key] || [];
          const allowed = files.length <= control.max_files
            && files.reduce((total, file) => total + file.size, 0) <= control.max_total_bytes;
          attachments[control.key] = allowed ? files : [];
          if (!allowed) composerError('Attachment limits changed. Reattach files within the new limits.');
          const input = element('input', 'chat-file');
          input.setAttribute('data-chat-file', control.key);
          input.type = 'file'; input.multiple = control.max_files > 1; input.accept = control.accept; input.hidden = true;
          listen(input, 'change', async () => { await addFiles(control, input.files); input.value = ''; });
          nodes.controls.append(button('chat-attach', control.label, () => { if (canWriteChat() && !readingFiles) input.click(); }), input);
        }
      }
      nodes.controls.hidden = !controls.length;
      nodes.sessionNote = element('span', 'chat-session-note', 'chat-session-note', 'Fixed for this chat · New session to change');
      nodes.controls.appendChild(nodes.sessionNote);
      renderAttachments();
    }
    function syncChatControls() {
      if (!chat || !nodes.input) return;
      const writable = canWriteChat();
      rendererView?.setAccess({live:target.kind === 'live' && liveActive, readOnly:!writable, canSend:writable, canInterrupt:writable});
      nodes.input.disabled = !writable;
      nodes.input.placeholder = writable ? 'Message…' : 'Read-only';
      for (const node of root.querySelectorAll('[data-ui="chat-composer"] button, [data-ui="chat-file"], .chat-perm button'))
        node.disabled = !writable;
      nodes.send.disabled = !writable || readingFiles || (runtimeContract().serialTurns && turnPending);
      nodes.interrupt.hidden = target.kind !== 'live' || !turnPending;
      for (const node of root.querySelectorAll('[data-ui="chat-attach"], [data-ui="chat-file"]')) node.disabled = !writable || readingFiles;
      const locked = chat.configured || chat.items.length > 0;
      for (const control of controls) {
        if (control.kind !== 'select') continue;
        const input = nodes.controls.querySelector('[data-chat-control="' + control.key + '"]');
        input.value = controlValues[control.key] || '';
        input.disabled = !writable || control.scope === 'session' && locked;
        input.title = input.disabled && writable ? 'Fixed for this chat. Start a New session to change it.' : '';
      }
      if (nodes.sessionNote) nodes.sessionNote.hidden = !(locked && controls.some(control => control.scope === 'session'));
    }
    function renderChat() {
      if (!chat || !nodes.scroll) return;
      const view = epoch;
      const rendererId = selectedRenderer();
      const native = Chat.supportsRenderer(rendererId);
      nodes.rendererNote.hidden = !rendererId || native;
      nodes.rendererNote.textContent = rendererId && !native
        ? 'Unsupported conversation renderer. Using standard chat; some presentation features may be unavailable.' : '';
      if (!runtimeContract().interactiveApproval) chat.pendingPermission = null;
      if (!native && rendererView) { rendererView.destroy(); rendererView = null; }
      if (native && !rendererView && !rendererLoading && !rendererFailed) {
        const host = nodes.scroll;
        rendererLoading = true;
        Chat.loadLocalModule(rendererId).then(module => {
          if (!current(view) || nodes.scroll !== host) return;
          rendererLoading = false;
          if (selectedRenderer() !== rendererId) return;
          rendererView = module.createView(host, {interrupt,
            sendInput:text => { if (!nodes.input || !canWriteChat()) return false; nodes.input.value = text; return sendChatMessage(); }});
          renderChat();
        }).catch(() => {
          if (!current(view)) return;
          rendererLoading = false; rendererFailed = true;
          reportError('Conversation presentation unavailable. Using standard chat; reload to retry.');
        });
      }
      if (rendererView && native) {
        rendererView.setAccess({live:target.kind === 'live' && liveActive, readOnly:!canWriteChat(), canSend:canWriteChat(), canInterrupt:canWriteChat()});
        rendererView.renderState(chat, {live:target.kind === 'live' && liveActive, readOnly:!canWriteChat(), pending:turnPending});
      } else Chat.renderChat(nodes.scroll, chat, !runtimeContract().interactiveApproval ? {} : { onPermission: (requestId, allow) => {
        if (current(view)) sendPermission(requestId, allow);
      } });
      syncAccess(); notify();
    }
    function handleChatFrame(frame) {
      if (!chat) return;
      const folded = Chat.foldEventPayload(chat, frame.data, frame.type === 'restore');
      chat = folded.state;
      if (frame.type === 'restore') turnPending = false;
      for (const event of folded.events) {
        if (nativeSubmission && event.ev === 'user.echo' && event.client_input_id === nativeSubmission.id) nativeSubmission.delivered = true;
        if (event.ev === 'turn.end') { turnPending = false; nativeSubmission = null; }
        else if (['turn.start', 'thinking.delta', 'user.echo', 'message', 'message.delta', 'tool.call', 'tool.result', 'permission.ask'].includes(event.ev))
          turnPending = true;
      }
      if (frame.type === 'restore' || folded.events.some(event => event.ev === 'session.config'))
        controlValues = Chat.reconcileControlValues(controls, controlValues, chat.config);
      renderChat();
    }
    function newNativeInputId() {
      if (typeof window.crypto?.randomUUID === 'function') return window.crypto.randomUUID();
      // randomUUID requires a secure context; getRandomValues also works on
      // ordinary LAN HTTP. IDs correlate receipts, never authorize access.
      const bytes = new Uint8Array(16);
      if (typeof window.crypto?.getRandomValues === 'function') window.crypto.getRandomValues(bytes);
      else {
        let seed = Date.now() + (++inputSequence);
        for (let i = 0; i < bytes.length; i++) {
          bytes[i] = (seed + Math.floor(Math.random() * 256)) & 255;
          seed = Math.floor(seed / 256);
        }
      }
      bytes[6] = (bytes[6] & 15) | 64;
      bytes[8] = (bytes[8] & 63) | 128;
      const h = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
      return h.slice(0,8) + '-' + h.slice(8,12) + '-' + h.slice(12,16) + '-' + h.slice(16,20) + '-' + h.slice(20);
    }
    function sendChatMessage() {
      if (!chat || !nodes.input || target.kind !== 'live' || target.surface !== 'structured') return false;
      const text = nodes.input.value;
      if (!text.trim()) return false;
      if (!canWriteChat()) { composerError('Read-only or reconnecting. Your draft has been kept.'); return false; }
      if (runtimeContract().serialTurns && turnPending) { composerError('Wait for the current turn to settle. Your draft has been kept.'); return false; }
      if (readingFiles) { composerError('Wait for attachments to finish loading. Your draft has been kept.'); return false; }
      try {
        const options = Chat.buildTurnOptions(controls, controlValues, attachments);
        const clientInputId = runtimeContract().inputReceipts ? newNativeInputId() : null;
        if (clientInputId) options.client_input_id = clientInputId;
        if (!inputSender || !inputSender.push(text, options)) {
          composerError('The session is reconnecting. Your draft has been kept; try again.'); return false;
        }
        const metadata = Object.values(attachments).flat().map(file => ({ name: file.name, type: file.type, size: file.size }));
        Chat.appendUserTurn(chat, text, metadata, clientInputId);
        if (clientInputId) nativeSubmission = {id:clientInputId, text};
        turnPending = true;
        for (const key of Object.keys(attachments)) attachments[key] = [];
        nodes.input.value = '';
        resizeChatInput();
        composerError(''); renderAttachments(); renderChat();
        return true;
      } catch (error) {
        composerError((error.message || 'Message could not be sent.') + ' Your draft has been kept; try again.');
        return false;
      }
    }
    function sendPermission(requestId, allow) {
      if (!runtimeContract().interactiveApproval) return false;
      if (!canWriteChat() || chat?.pendingPermission?.request_id !== requestId) return false;
      if (!sendFrame('permission', { request_id: requestId, allow: !!allow })) return false;
      chat.pendingPermission = null; renderChat(); return true;
    }
    function interrupt() { return !!(canWriteChat() && sendFrame('interrupt')); }
    function fileSize(bytes) {
      return bytes < 1024 ? bytes + ' B' : bytes < 1024 * 1024 ? Math.ceil(bytes / 1024) + ' KB'
        : (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }
    function renderAttachments() {
      if (!nodes.tray) return;
      nodes.tray.textContent = '';
      for (const control of controls.filter(item => item.kind === 'file')) {
        for (const [index, file] of (attachments[control.key] || []).entries()) {
          const chip = element('span', null, 'chat-file-chip');
          const remove = button('chat-file-remove', '×', () => {
            if (!canWriteChat() || readingFiles) return;
            attachments[control.key].splice(index, 1); renderAttachments(); composerError('');
          });
          remove.className = 'chat-file-remove';
          remove.setAttribute('aria-label', 'Remove ' + file.name);
          chip.append(element('span', null, 'chat-file-name', file.name), element('span', null, 'chat-file-size', fileSize(file.size)), remove);
          nodes.tray.appendChild(chip);
        }
      }
      nodes.tray.hidden = !nodes.tray.childElementCount;
      syncChatControls();
    }
    function filePayload(file) {
      return new Promise((resolve, reject) => {
        const reader = new window.FileReader();
        readers.add(reader);
        function finish(error, value) {
          readers.delete(reader);
          reader.onload = reader.onerror = reader.onabort = null;
          error ? reject(error) : resolve(value);
        }
        reader.onerror = () => finish(new Error('Could not read ' + file.name));
        reader.onabort = () => finish(new Error('Attachment read cancelled.'));
        reader.onload = () => {
          const result = String(reader.result || ''), comma = result.indexOf(',');
          if (comma < 0 || result.length - comma - 1 > Math.ceil(MAX_ATTACHMENT_BYTES / 3) * 4)
            return finish(new Error('Could not encode ' + file.name + ' within the attachment byte limit.'));
          finish(null, { name: file.name, type: file.type || '', size: file.size, data: result.slice(comma + 1) });
        };
        try { reader.readAsDataURL(file); } catch (error) { finish(error); }
      });
    }
    async function addFiles(control, files) {
      if (!canWriteChat() || readingFiles) return;
      const incoming = Array.from(files || []), previous = attachments[control.key] || [];
      if (previous.length + incoming.length > control.max_files) { composerError('Up to ' + control.max_files + ' files can be attached.'); return; }
      if (incoming.some(file => !Number.isFinite(file.size) || file.size < 0)) { composerError('Invalid attachment size.'); return; }
      const incomingBytes = incoming.reduce((sum, file) => sum + file.size, 0);
      const existingBytes = Object.values(attachments).flat().reduce((sum, file) => sum + file.size, 0);
      if (incomingBytes + previous.reduce((sum, file) => sum + file.size, 0) > control.max_total_bytes
        || incomingBytes + existingBytes > MAX_ATTACHMENT_BYTES) {
        composerError('Attachments can total up to ' + fileSize(Math.min(control.max_total_bytes, MAX_ATTACHMENT_BYTES)) + '.'); return;
      }
      const view = epoch, ownAttachments = attachments;
      readingFiles = true; composerError(''); syncChatControls();
      try {
        const payloads = await Promise.all(incoming.map(filePayload));
        if (!current(view) || attachments !== ownAttachments || !canWriteChat()) return;
        attachments[control.key] = previous.concat(payloads);
        renderAttachments();
      } catch (error) {
        if (current(view) && attachments === ownAttachments) {
          for (const reader of readers) { try { reader.abort(); } catch (_) {} }
          composerError(error.message || 'Could not read that file.');
        }
      } finally {
        if (current(view)) { readingFiles = false; syncChatControls(); }
      }
    }

    async function loadHistory(view) {
      const sessions = await request(agentPath());
      if (!current(view)) return;
      if (!Array.isArray(sessions)) throw new Error('Invalid session list.');
      const list = element('div', 'history', 'history-wrap');
      const saved = snapshot();
      list.appendChild(button('history-live', 'Open live session', () => open({ ...saved, kind: 'live', sessionId: undefined })));
      if (!sessions.length) list.appendChild(element('p', null, 'muted', 'No sessions recorded.'));
      for (const session of sessions) {
        const row = element('div', 'history-item', 'history-item');
        row.append(element('span', null, 'status', session.state || 'unknown'), element('b', null, 'mono', session.id),
          element('span', null, 'muted', (session.surface === 'structured' ? 'Chat' : session.surface === 'terminal' ? 'Terminal' : 'Legacy surface unknown') + ' · started ' + (session.created_at || '')));
        const metadata = { kind: 'live', agentId: target.agentId, sessionId: session.id, title: target.title, surface: validSurface(session.surface) };
        if (session.state === 'live' && session.available !== false && metadata.surface)
          row.appendChild(button('history-attach', 'Attach live', () => open(metadata)));
        if (canContinueNative(session))
          row.appendChild(button('history-continue-native', 'Continue native conversation', () => open({ ...metadata, continueNative:true })));
        row.appendChild(button('history-replay', 'Replay', () => open({ ...metadata, kind: 'replay' })));
        list.appendChild(row);
      }
      nodes.body.appendChild(list);
      setStatus('history', 'Session history');
    }
    async function loadReplay(view) {
      if (!target.sessionId) { unavailable('Choose a saved session from History to replay.', false); return; }
      const data = await request('/api/sessions/' + encodeURIComponent(target.sessionId) + '/replay');
      if (!current(view)) return;
      recording = Replay.normalizeReplay(data);
      persistedRenderer = data.renderer || data.session?.renderer || persistedRenderer;
      target.surface = validSurface(data.surface) || target.surface ||
        (recording.events.some(event => event.kind === 'event' || event.type === 'event') ? 'structured' : 'terminal');
      let rendererReady = true;
      if (target.surface === 'terminal' && services.ensureTerminal) {
        try { await services.ensureTerminal(); }
        catch (error) {
          if (!current(view)) return;
          rendererReady = false; setStatus('error', 'Terminal renderer unavailable'); reportError(error.message);
        }
        if (!current(view)) return;
      }
      const mounted = rendererReady && mountSurface();
      // Surface initialization only owns its host, never this sibling toolbar.
      mountReplayControls();
      if (mounted) {
        replaySeek(target.surface === 'structured' ? replayDuration() : 0);
        setStatus('replay', 'Replay · read-only');
      }
    }
    function replayDuration() {
      let duration = 0;
      for (const items of [recording?.events || [], recording?.checkpoints || []])
        for (const item of items) if (Number.isFinite(Number(item.time))) duration = Math.max(duration, Number(item.time));
      return duration;
    }
    function mountReplayControls() {
      const bar = element('div', 'replay-controls', 'pane-replay-controls');
      nodes.play = button('replay-play', 'Play', () => { replayPlaying ? pauseReplay() : playReplay(); });
      bar.append(nodes.play, button('replay-start', 'Start', () => { pauseReplay(); replaySeek(0); }),
        button('replay-end', 'End', () => { pauseReplay(); replaySeek(replayDuration()); }));
      const speed = element('select', 'replay-speed');
      speed.setAttribute('aria-label', 'Replay speed');
      for (const value of [0.5, 1, 2, 8]) {
        const option = element('option', null, null, value + '×'); option.value = String(value); speed.appendChild(option);
      }
      speed.value = '1';
      listen(speed, 'change', () => {
        replaySpeed = [0.5, 1, 2, 8].includes(Number(speed.value)) ? Number(speed.value) : 1;
        if (replayPlaying) { pauseReplay(); playReplay(); }
      });
      nodes.seek = element('input', 'replay-seek');
      nodes.seek.type = 'range'; nodes.seek.min = '0'; nodes.seek.max = String(replayDuration()); nodes.seek.step = '0.01';
      nodes.seek.setAttribute('aria-label', 'Replay position');
      listen(nodes.seek, 'input', () => {
        const time = Number(nodes.seek.value);
        pauseReplay(); replaySeek(time);
      });
      nodes.time = element('span', 'replay-time', 'muted');
      const download = element('a', 'replay-download', 'ghost', 'Download recording');
      download.href = '/api/sessions/' + encodeURIComponent(target.sessionId) + '/recording';
      download.setAttribute('download', '');
      bar.append(speed, nodes.seek, nodes.time,
        button('replay-final', target.surface === 'structured' ? 'Final transcript' : 'Final screen', () => { pauseReplay(); replaySeek(replayDuration()); }), download);
      const label = element('label', null, null, 'Retention ');
      const retention = element('select', 'replay-retention');
      for (const [value, text] of [['none', 'None'], ['7d', '7 days'], ['30d', '30 days'], ['permanent', 'Permanent']]) {
        const option = element('option', null, null, text); option.value = value; retention.appendChild(option);
      }
      retention.value = recording.retention || recording.metadata?.retention || 'permanent';
      retention.disabled = !canManageRecording();
      label.appendChild(retention);
      const message = element('span', 'replay-retention-message', 'muted');
      const sessionId = target.sessionId, view = epoch;
      let savedRetention = retention.value, retentionPending = false, deletePending = false;
      listen(retention, 'change', async () => {
        if (!canManageRecording() || retentionPending) { retention.value = savedRetention; return; }
        const value = retention.value;
        if (!['none', '7d', '30d', 'permanent'].includes(value)) return;
        retentionPending = true; retention.disabled = true;
        try {
          await request('/api/sessions/' + encodeURIComponent(sessionId) + '/retention', { method: 'PATCH', body: JSON.stringify({ retention: value }) });
          if (current(view)) { savedRetention = value; message.textContent = 'Saved'; }
        } catch (error) {
          if (current(view)) { retention.value = savedRetention; message.textContent = error.message || 'Could not save retention.'; }
        } finally {
          if (current(view)) { retentionPending = false; retention.disabled = !canManageRecording(); }
        }
      });
      const remove = button('replay-delete', 'Delete recording', async () => {
        if (!canManageRecording() || deletePending) return;
        deletePending = true; remove.disabled = true;
        try {
          const ok = services.confirm && await services.confirm('Delete recording?', 'Permanently erase this recorded transcript and checkpoints. This cannot be undone.', 'Delete recording');
          if (!ok || !current(view) || !canManageRecording()) return;
          await request('/api/sessions/' + encodeURIComponent(sessionId) + '/recording', { method: 'DELETE' });
          if (current(view)) await open(snapshot());
        } catch (error) { if (current(view)) message.textContent = error.message || 'Could not delete recording.'; }
        finally { if (current(view)) { deletePending = false; remove.disabled = !canManageRecording(); } }
      });
      remove.disabled = !canManageRecording();
      bar.append(label, message, remove);
      root.appendChild(bar);
      updateReplayUI();
    }
    function replaySeek(time) {
      if (closed || target.kind !== 'replay' || !recording) return;
      replayCursor = Math.max(0, Math.min(Number(time) || 0, replayDuration()));
      if (target.surface === 'structured') {
        chat = Chat.initialChatState();
        for (const event of Replay.eventsBetween(recording.events, -Infinity, replayCursor))
          if (event.kind === 'event' || event.type === 'event') chat = Chat.foldEventPayload(chat, event.data, false).state;
        renderChat();
      } else if (terminal) {
        terminal.reset();
        const index = Replay.nearestCheckpointIndex(recording.checkpoints, replayCursor);
        let startTime = -Infinity, startCursor = null;
        if (index >= 0) {
          const checkpoint = recording.checkpoints[index];
          if (checkpoint.serialized_screen) terminal.write(checkpoint.serialized_screen);
          startTime = checkpoint.time; startCursor = checkpoint.cursor;
        }
        for (const event of Replay.eventsBetween(recording.events, startTime, replayCursor, startCursor))
          if ((event.type === 'o' || event.type === 'output') && event.data != null) terminal.write(event.data);
      }
      updateReplayUI();
    }
    function updateReplayUI() {
      if (nodes.seek) nodes.seek.value = String(replayCursor);
      if (nodes.time) nodes.time.textContent = Replay.formatClock(replayCursor) + ' / ' + Replay.formatClock(replayDuration());
      if (nodes.play) nodes.play.textContent = replayPlaying ? 'Pause' : 'Play';
    }
    function playReplay() {
      if (closed || target?.kind !== 'replay' || !recording || target.surface === 'terminal' && !terminal) return;
      if (replayCursor >= replayDuration()) replaySeek(0);
      replayPlaying = true; updateReplayUI(); scheduleReplayStep();
    }
    function scheduleReplayStep() {
      if (!replayPlaying || !recording) return;
      let time = Infinity;
      for (const items of [recording.events, recording.checkpoints])
        for (const item of items)
          if (Number.isFinite(item.time) && item.time > replayCursor + 1e-9) time = Math.min(time, item.time);
      if (!Number.isFinite(time)) { pauseReplay(); return; }
      const view = epoch, ownRecording = recording, generation = replayGeneration;
      replayTimer = window.setTimeout(() => {
        if (!current(view) || recording !== ownRecording || !replayPlaying || generation !== replayGeneration) return;
        replayTimer = null;
        replaySeek(time); scheduleReplayStep();
      }, Math.max(0, (time - replayCursor) * 1000 / replaySpeed));
    }

    function reconnect() {
      if (closed || !target) return Promise.resolve(getState());
      if (target.kind === 'live' && target.sessionId && status !== 'ended' && status !== 'inactive' && (terminal || chat)) {
        wantOpen = true; reconnectDelay = 500; clearError(); connectSocket();
        return Promise.resolve(getState());
      }
      return open({ ...snapshot(), restore: restoreRequested });
    }
    function newSession() {
      if (!canOperate() || !target) return Promise.resolve(getState());
      return open({ kind: 'live', agentId: target.agentId, title: target.title, surface: target.surface, forceNew: true });
    }
    async function endSession() {
      if (endPending || !canEndSession()) return false;
      const view = epoch, sessionId = target.sessionId;
      endPending = true;
      try {
        const ok = services.confirm && await services.confirm('End this session?', 'This stops the runtime for everyone in this session. Saved history remains available.', 'End session');
        if (!ok || !current(view) || target.sessionId !== sessionId || !canEndSession()) return false;
        return sendFrame('terminate');
      } catch (error) { if (current(view)) reportError(error.message || 'Could not end the session.'); return false; }
      finally { if (current(view)) endPending = false; }
    }
    function showHistory() {
      if (closed || !target) return Promise.resolve(getState());
      return open({ kind: 'history', agentId: target.agentId, title: target.title, surface: target.surface });
    }
    function close() {
      if (closed) return;
      reset(); closed = true; target = null; status = 'closed'; statusText = 'Closed';
      notify();
    }
    function refreshAccess() {
      if (closed) return;
      syncAccess();
      for (const control of root.querySelectorAll('[data-ui="replay-retention"], [data-ui="replay-delete"]'))
        control.disabled = !canManageRecording();
      notify();
    }
    return { id, open, getState, snapshot, focus, resize, close, reconnect, newSession, endSession,
      showHistory, requestKeyboard, releaseKeyboard, handoffKeyboard, interrupt, sendPrefix, refreshAccess };
  }

  return { createPane };
});
