/* Workbench adaptation of DeepOrca webui: conversation-view's distinct messages,
 * collapsible thinking, tool-presentation's intent-first summaries and compact
 * tool groups. Deliberately NOT the native app/router/approval/network layer.
 * Markdown is built as DOM nodes; provider HTML and images are never executed. */
(function(root, factory) {
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.DeepOrcaChat = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function(root) {
  'use strict';
  const Chat = typeof module === 'object' && module.exports ? require('../../chat.js') : root.AgentBridgeChat;
  const text = value => typeof value === 'string' ? value : value == null ? '' : JSON.stringify(value, null, 2);
  function el(doc, tag, cls, value) {
    const node = doc.createElement(tag);
    if (cls) node.className = cls;
    if (value != null) node.textContent = String(value);
    return node;
  }
  function safeUrl(value) {
    if (/[\u0000-\u0020\u007f]/.test(value)) return null;
    try { const url = new URL(value); return /^(https?:|mailto:)$/.test(url.protocol) ? url.href : null; }
    catch (_) { return null; }
  }
  // A deliberately bounded inline grammar. It never uses innerHTML, even for
  // incomplete streaming markup, literal HTML, unsafe URLs or code contents.
  function inline(doc, parent, source, depth = 0) {
    if (depth > 12) { parent.appendChild(doc.createTextNode(source)); return; }
    let plain = '';
    const flush = () => { if (plain) parent.appendChild(doc.createTextNode(plain)); plain = ''; };
    for (let i = 0; i < source.length;) {
      if (source[i] === '\\' && /[\\`*{}\[\]()#+.!_>~|\-]/.test(source[i + 1] || '')) { plain += source[i + 1]; i += 2; continue; }
      if (source[i] === '`') {
        const marks = source.slice(i).match(/^`+/)[0], end = source.indexOf(marks, i + marks.length);
        if (end >= 0) { flush(); parent.appendChild(el(doc, 'code', '', source.slice(i + marks.length, end).replace(/\n/g, ' '))); i = end + marks.length; continue; }
      }
      // Images are rendered as links/alt text, never fetched. Balanced URL
      // parentheses handle common documentation links such as foo(bar).
      const image = source[i] === '!' && source[i + 1] === '[';
      if (source[i] === '[' || image) {
        const start = i + (image ? 2 : 1), close = source.indexOf('](', start);
        if (close >= 0) {
          let end = close + 2, balance = 1;
          for (; end < source.length && balance; end++) { if (source[end] === '(') balance++; else if (source[end] === ')') balance--; }
          if (!balance) {
            const url = safeUrl(source.slice(close + 2, end - 1).trim()), label = source.slice(start, close);
            flush();
            if (url && !image) { const a = el(doc, 'a'); a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer'; inline(doc, a, label, depth + 1); parent.appendChild(a); }
            else parent.appendChild(doc.createTextNode(label + (image ? ' [image omitted]' : '')));
            i = end; continue;
          }
        }
      }
      const marker = ['**', '__', '~~', '*', '_'].find(mark => source.startsWith(mark, i));
      if (marker && !(marker.includes('_') && /\w/.test(source[i - 1] || ''))) {
        const end = source.indexOf(marker, i + marker.length);
        if (end > i + marker.length) {
          flush(); const tag = marker.length === 1 ? 'em' : marker === '~~' ? 'del' : 'strong';
          const node = el(doc, tag); inline(doc, node, source.slice(i + marker.length, end), depth + 1); parent.appendChild(node);
          i = end + marker.length; continue;
        }
      }
      plain += source[i++];
    }
    flush();
  }
  function cells(line) {
    const result = []; let value = '', code = false;
    line = line.trim().replace(/^\|/, '').replace(/\|\s*$/, '');
    for (let i = 0; i < line.length; i++) {
      if (line[i] === '\\' && line[i + 1] === '|') { value += '|'; i++; }
      else if (line[i] === '`') { code = !code; value += line[i]; }
      else if (line[i] === '|' && !code) { result.push(value.trim()); value = ''; }
      else value += line[i];
    }
    result.push(value.trim()); return result;
  }
  const listMatch = line => /^( *)([-+*]|\d+[.)]) +(.*)$/.exec(line);
  const fenceMatch = line => /^ {0,3}(`{3,}|~{3,})([^\s]*)[^\n]*$/.exec(line);
  function markdown(doc, source, options = {}) {
    const host = el(doc, 'div', 'do-markdown');
    const lines = String(source || '').replace(/\r\n?/g, '\n').split('\n');
    function block(parent, rows, depth = 0) {
      if (depth > 16) { parent.appendChild(el(doc, 'pre', '', rows.join('\n'))); return; }
      const starts = (line, next) => /^\s*$/.test(line) || fenceMatch(line) || /^ {0,3}(#{1,6})\s|^\s*>|^ {0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line) || listMatch(line) || (next && line.includes('|') && cells(next).every(cell => /^:?-{3,}:?$/.test(cell)));
      for (let i = 0; i < rows.length;) {
        let line = rows[i];
        if (!line.trim()) { i++; continue; }
        const fence = fenceMatch(line);
        if (fence) {
          const body = []; i++;
          while (i < rows.length && !new RegExp('^ {0,3}' + fence[1][0] + '{' + fence[1].length + ',}\\s*$').test(rows[i])) body.push(rows[i++]);
          const codeText = body.join('\n') + (i < rows.length && body.length ? '\n' : '');
          if (i < rows.length) i++;
          const wrap = el(doc, 'div', 'do-code'), bar = el(doc, 'div', 'do-code-bar');
          bar.appendChild(el(doc, 'span', '', fence[2] || 'code'));
          const copy = el(doc, 'button', 'do-copy', 'Copy'); copy.type = 'button'; copy.setAttribute('aria-label', 'Copy code');
          copy.onclick = async () => {
            const clipboard = doc.defaultView?.navigator?.clipboard || root.navigator?.clipboard;
            try {
              if (options.copyText) await options.copyText(codeText);
              else if (clipboard) await clipboard.writeText(codeText);
              else throw new Error('Clipboard unavailable');
              copy.textContent = 'Copied';
            } catch (_) { copy.textContent = 'Copy unavailable'; }
          };
          bar.appendChild(copy); wrap.appendChild(bar);
          const pre = el(doc, 'pre'); pre.appendChild(el(doc, 'code', '', codeText)); wrap.appendChild(pre); parent.appendChild(wrap); continue;
        }
        const heading = /^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
        if (heading) { const h = el(doc, 'h' + heading[1].length); inline(doc, h, heading[2]); parent.appendChild(h); i++; continue; }
        if (/^ {0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line)) { parent.appendChild(el(doc, 'hr')); i++; continue; }
        if (/^\s*>/.test(line)) {
          const quote = [];
          while (i < rows.length && /^\s*>/.test(rows[i])) quote.push(rows[i++].replace(/^\s*> ?/, ''));
          const node = el(doc, 'blockquote'); block(node, quote, depth + 1); parent.appendChild(node); continue;
        }
        const match = listMatch(line);
        if (match) {
          const indent = match[1].length, ordered = /^\d/.test(match[2]), list = el(doc, ordered ? 'ol' : 'ul');
          if (ordered && parseInt(match[2], 10) !== 1) list.setAttribute('start', String(parseInt(match[2], 10)));
          while (i < rows.length) {
            const item = listMatch(rows[i]);
            if (!item || item[1].length !== indent || /^\d/.test(item[2]) !== ordered) break;
            const contentIndent = item[1].length + item[2].length + 1, content = [item[3]]; i++;
            while (i < rows.length) {
              const next = listMatch(rows[i]);
              if (next && next[1].length <= indent) break;
              if (!rows[i].trim()) {
                let j = i + 1; while (j < rows.length && !rows[j].trim()) j++;
                if (j >= rows.length || rows[j].search(/\S/) <= indent) break;
                content.push(''); i++; continue;
              }
              if (rows[i].search(/\S/) <= indent) break;
              content.push(rows[i].slice(Math.min(contentIndent, rows[i].search(/\S/)))); i++;
            }
            const li = el(doc, 'li'); block(li, content, depth + 1); list.appendChild(li);
            let j = i; while (j < rows.length && !rows[j].trim()) j++;
            if (listMatch(rows[j] || '')?.[1].length === indent) i = j;
          }
          parent.appendChild(list); continue;
        }
        if (line.includes('|') && rows[i + 1] && cells(rows[i + 1]).every(cell => /^:?-{3,}:?$/.test(cell))) {
          const headers = cells(line), align = cells(rows[i + 1]); i += 2;
          const table = el(doc, 'table'), head = el(doc, 'thead'), body = el(doc, 'tbody');
          const row = (values, tag) => { const tr = el(doc, 'tr'); headers.forEach((_, index) => { const td = el(doc, tag); inline(doc, td, values[index] || ''); const a = align[index] || ''; if (a.endsWith(':')) td.style.textAlign = a.startsWith(':') ? 'center' : 'right'; tr.appendChild(td); }); return tr; };
          head.appendChild(row(headers, 'th'));
          while (i < rows.length && rows[i].trim() && rows[i].includes('|')) body.appendChild(row(cells(rows[i++]), 'td'));
          table.appendChild(head); table.appendChild(body);
          const scroll = el(doc, 'div', 'do-table-scroll'); scroll.tabIndex = 0; scroll.setAttribute('aria-label', 'Scrollable table'); scroll.appendChild(table); parent.appendChild(scroll); continue;
        }
        if (i + 1 < rows.length && /^ {0,3}(=+|-+)\s*$/.test(rows[i + 1])) {
          const node = el(doc, rows[i + 1].trim()[0] === '=' ? 'h1' : 'h2'); inline(doc, node, line); parent.appendChild(node); i += 2; continue;
        }
        const paragraph = [line]; i++;
        while (i < rows.length && !starts(rows[i], rows[i + 1])) paragraph.push(rows[i++]);
        const node = el(doc, 'p');
        paragraph.forEach((part, index) => { inline(doc, node, part.replace(/ {2}$/, '')); if (index < paragraph.length - 1) node.appendChild(/ {2}$/.test(part) ? el(doc, 'br') : doc.createTextNode('\n')); });
        parent.appendChild(node);
      }
    }
    block(host, lines); return host;
  }
  function summarizeTool(item) {
    const name = item.tool || item.name || 'Tool';
    let args = item.input;
    if (typeof args === 'string') { try { args = JSON.parse(args); } catch (_) { args = {command:args}; } }
    args = args && typeof args === 'object' ? args : {};
    // Mirrors native tool-presentation's read/write/search/shell/web summaries,
    // with the exact tool name retained so a label cannot conceal the action.
    let detail = args.command || args.cmd || args.operation?.path || args.file_path || args.filePath || args.path || args.filename || args.pattern || args.query || args.regex || args.keyword || args.url || args.name || args.skill_name || '';
    if (!detail && Array.isArray(args.commands)) detail = args.commands.length + ' commands';
    if (!detail && args.action) detail = args.action + (args.task_id ? ' · ' + args.task_id : '');
    if (/^(?:.*\.)?(read|read_file|read_skill_file)$/.test(name) && Number.isInteger(args.offset) && args.offset >= 1) {
      detail += ' · line ' + args.offset + (Number.isInteger(args.limit) && args.limit > 1 ? '–' + (args.offset + args.limit - 1) : '');
    }
    return {name, detail:text(detail).replace(/\s+/g, ' ').slice(0, 220)};
  }
  // Native tool-presentation distinguishes completion of a callback from the
  // outcome in its structured result (nonzero exit, background job, timeout).
  // Canonical error flags take precedence; we never infer successful execution
  // merely from a turn ending or from a tool card having been created.
  function resultStatus(value, depth = 0) {
    if (depth > 8) return 'Outcome uncertain';
    let data = value;
    if (typeof data === 'string') { try { data = JSON.parse(data); } catch (_) { data = {}; } }
    if (!data || typeof data !== 'object') data = {};
    const status = typeof data.status === 'string' ? data.status.toLowerCase() : '';
    if (['cancelled','canceled','killed','stopped'].includes(status)) return 'Interrupted · result unknown';
    if (['timed_out','timeout'].includes(status)) return 'Timed out';
    if (data.isError || data.is_error || data.ok === false || data.success === false || data.error || data.failure ||
      ['failed','error','blocked','denied','start_failed','rejected'].includes(status) ||
      (Number.isInteger(data.exit_code) && data.exit_code !== 0)) return 'Failed';
    const results = Array.isArray(data.results) ? data.results : Array.isArray(data) ? data : [];
    const states = results.map(item => resultStatus(item?.result ?? item, depth + 1));
    if (states.some(item => ['Failed','Timed out','Interrupted · result unknown'].includes(item))) return 'Failed';
    if (states.includes('Outcome uncertain')) return 'Outcome uncertain';
    if (['running','queued','pending','started'].includes(status) || states.includes('Started · background')) return 'Started · background';
    if (data.result_file && !status && data.ok !== true) return 'Outcome uncertain';
    if (typeof value === 'string' && /^\s*(?:error\s*:|traceback\s*\(|exception\s*:)/i.test(value)) return 'Failed';
    return value == null ? 'Missing result' : 'Done';
  }
  function toolStatus(item, state, access = {live:true}) {
    if (item.code === 'approval_required' || item.status === 'blocked') return 'Blocked by local policy';
    if (item.is_error || item.code === 'missing_result') return item.code === 'missing_result' ? 'Missing result' : 'Failed';
    if (item.result != null || item.kind === 'tool-result') return resultStatus(item.result ?? item.content);
    const status = item.status || (state.run?.state !== 'running' ? state.run?.state : '');
    if (status === 'interrupted' || status === 'cancelled') return 'Interrupted · result unknown';
    if (status === 'completed' || status === 'incomplete') return 'Missing result';
    if (status && status !== 'running') return 'Outcome uncertain';
    if (!access.live) return 'Result not recorded';
    return 'Running';
  }
  function installStyle(doc) {
    if (!doc.head) return;
    if (doc.querySelector('link[data-deeporca-style]')) return;
    const link = doc.createElement('link'); link.rel = 'stylesheet'; link.href = '/static/integrations/deeporca/chat.css?v=2';
    link.setAttribute('data-deeporca-style', ''); doc.head.appendChild(link);
  }
  function createView(container, callbacks = {}) {
    const doc = container.ownerDocument || root.document;
    installStyle(doc);
    const host = el(doc, 'section', 'deeporca-chat'); host.setAttribute('aria-label', 'DeepOrca conversation');
    const surface = container.closest('.chat-surface'); surface?.classList.add('deeporca-surface');
    container.classList.add('deeporca-host'); container.replaceChildren(host);
    let state = Chat.initialChatState(), access = {live:true, readOnly:true}, destroyed = false, first = true;
    const open = new Map();
    function disclosure(cls, key, label) {
      const node = el(doc, 'details', cls); node.setAttribute('data-key', key); node.open = open.get(key) || false;
      const summary = el(doc, 'summary'); summary.appendChild(el(doc, 'span', 'do-chevron', '›')); summary.appendChild(el(doc, 'span', 'do-summary-label', label)); node.appendChild(summary);
      node.ontoggle = () => { if (!destroyed && host.contains(node)) open.set(key, node.open); };
      return node;
    }
    function draw() {
      if (destroyed) return;
      const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 80, scroll = container.scrollTop;
      for (const node of host.querySelectorAll('details[data-key]')) open.set(node.dataset.key, node.open);
      const focused = doc.activeElement?.closest?.('details[data-key]')?.dataset.key;
      const content = el(doc, 'div', 'do-content');
      const header = el(doc, 'header', 'do-status'); header.appendChild(el(doc, 'span', 'do-brand', 'DeepOrca'));
      if (state.config?.model) header.appendChild(el(doc, 'span', 'do-model', state.config.model));
      header.appendChild(el(doc, 'span', 'do-access', access.readOnly || !access.live ? 'Read-only transcript' : 'Managed runtime'));
      content.appendChild(header);
      const items = state.items || [];
      if (!items.length) content.appendChild(el(doc, 'p', 'do-empty', 'Start a conversation. DeepOrca will use the connector’s managed profile.'));
      let group = null;
      items.forEach((item, index) => {
        const key = item.kind + ':' + (item.turn_id || '') + ':' + (item.tool_id || item.message_id || index);
        if (!['tool', 'tool-result'].includes(item.kind)) group = null;
        if (item.kind === 'user' || item.kind === 'assistant') {
          const node = el(doc, 'article', 'do-message do-' + item.kind);
          node.appendChild(el(doc, 'div', 'do-role', item.kind === 'user' ? 'You' : 'DeepOrca'));
          if (item.kind === 'user') node.appendChild(el(doc, 'div', 'do-user-text', item.text || ''));
          else node.appendChild(markdown(doc, item.text, callbacks));
          if (item.pending || item.delivery_error) node.appendChild(el(doc, 'small', item.delivery_error ? 'do-error' : 'do-note', item.delivery_error || 'Awaiting delivery confirmation'));
          content.appendChild(node);
        } else if (item.kind === 'thinking') {
          const node = disclosure('do-thinking', key, 'Thinking');
          node.appendChild(el(doc, 'div', 'do-thinking-text', item.text)); content.appendChild(node);
        } else if (item.kind === 'tool' || item.kind === 'tool-result') {
          // Old snapshots can still contain a native status pseudo-tool.
          if (item.native?.kind === 'status' || /^DeepOrca (usage|status)$/i.test(item.tool || '')) return;
          if (!group) { group = el(doc, 'div', 'do-tool-group'); group.setAttribute('aria-label', 'Tool activity'); content.appendChild(group); }
          const status = toolStatus(item, state, access), info = summarizeTool(item);
          let resultMeta = item.result;
          if (typeof resultMeta === 'string') { try { resultMeta = JSON.parse(resultMeta); } catch (_) { resultMeta = null; } }
          const truncated = item.truncated || item.native?.truncated || resultMeta?.truncated === true;
          const node = disclosure('do-tool' + (!['Done','Running','Started · background'].includes(status) ? ' do-tool-error' : ''), key, info.name);
          const summary = node.children[0];
          summary.appendChild(el(doc, 'span', 'do-tool-detail', info.detail));
          summary.appendChild(el(doc, 'span', 'do-tool-state' + (status === 'Running' ? ' do-running' : ''), status + (truncated ? ' · truncated' : '')));
          const body = el(doc, 'div', 'do-tool-body');
          if (item.input != null) { body.appendChild(el(doc, 'div', 'do-section-label', 'Input')); body.appendChild(el(doc, 'pre', '', text(item.input))); }
          body.appendChild(el(doc, 'div', 'do-section-label', 'Result'));
          body.appendChild(el(doc, 'pre', '', item.result != null ? text(item.result) : item.content != null ? text(item.content) : status === 'Running' ? 'Waiting for the tool result…' : 'No result was recorded. Tool effects may have occurred; inspect before retrying.'));
          if (item.result == null && status !== 'Running') body.appendChild(el(doc, 'p', 'do-warning do-uncertain', 'Side effects may have occurred; stopping does not roll them back. Inspect before retrying.'));
          if (truncated) body.appendChild(el(doc, 'p', 'do-warning do-truncated', 'Output truncated' + (item.original_bytes ? ' · original ' + item.original_bytes + ' bytes' : '') + '. Only the retained portion is shown.'));
          node.appendChild(body); group.appendChild(node);
        } else if (item.kind === 'turn') {
          const status = item.status || (item.is_error ? 'error' : 'completed');
          const node = el(doc, 'footer', 'do-turn' + (item.is_error || ['error','uncertain','interrupted'].includes(status) ? ' do-warning' : ''));
          const labels = {completed:'Turn completed', interrupted:'Turn interrupted · tool effects may have occurred', uncertain:'Turn ended · outcome uncertain', error:'Turn failed', incomplete:'Turn incomplete'};
          node.appendChild(el(doc, 'span', '', labels[status] || 'Turn ' + status));
          if (typeof item.cost_usd === 'number' && Number.isFinite(item.cost_usd)) node.appendChild(el(doc, 'span', '', '$' + item.cost_usd.toFixed(4)));
          const usage = item.usage || item.native?.usage;
          if (usage) { const nodeUsage = disclosure('do-usage', key + ':usage', 'Usage'); nodeUsage.appendChild(el(doc, 'pre', '', text(usage))); node.appendChild(nodeUsage); }
          content.appendChild(node);
          if (item.result) content.appendChild(markdown(doc, item.result, callbacks));
        } else if (item.kind === 'error') {
          content.appendChild(el(doc, 'div', 'do-error-banner', item.text || item.message || 'Runtime error'));
        }
      });
      const running = access.live && (state.run?.state === 'running' || access.pending);
      if (running) { const note = el(doc, 'div', 'do-run-note', 'Working…'); note.setAttribute('role', 'status'); content.appendChild(note); }
      const statuses = Object.values(state.runtimeStatus || {}).concat(items.filter(item =>
        item.kind === 'tool' && (item.native?.kind === 'status' || /^DeepOrca (usage|status)$/i.test(item.tool || ''))
      ).map(item => ({content:item.result ?? item.input})));
      if (statuses.length || state.status) {
        const meta = disclosure('do-runtime-meta', 'runtime-meta', 'Runtime details');
        if (state.status) meta.appendChild(el(doc, 'p', '', state.status));
        statuses.forEach(status => meta.appendChild(el(doc, 'pre', '', text(status.content ?? status.input))));
        content.appendChild(meta);
      }
      host.replaceChildren(content);
      if (focused) for (const node of host.querySelectorAll('details[data-key]')) if (node.dataset.key === focused) node.children[0].focus({preventScroll:true});
      if (first || nearBottom) container.scrollTop = container.scrollHeight; else container.scrollTop = scroll;
      first = false;
    }
    return {
      // Convenience entry points still use the ONE platform event reducer;
      // this renderer never owns transport or a parallel replay implementation.
      restore(events) { if (destroyed) return; state = Chat.initialChatState(); state.renderer = 'deeporca-chat-v1'; for (const event of events || []) Chat.applyEvent(state, event); draw(); },
      applyEvent(event) { if (destroyed) return; Chat.applyEvent(state, event); draw(); },
      renderState(value, nextAccess) { state = value || {items:[]}; if (nextAccess) access = {...access, ...nextAccess}; draw(); },
      setAccess(value) {
        const next = {...access, ...value};
        const changed = ['readOnly', 'live', 'pending', 'canSend', 'canInterrupt']
          .some(key => next[key] !== access[key]);
        access = next;
        // Disconnect/access changes can be the last notification received.
        // Don't leave a read-only transcript claiming a tool is still running.
        if (changed) draw();
      },
      sendInput(value) { return !destroyed && access.live && !access.readOnly && access.canSend !== false && state.run?.state !== 'running' && !access.pending ? callbacks.sendInput?.(value) : false; },
      interrupt() { return !destroyed && access.live && !access.readOnly && access.canInterrupt !== false ? callbacks.interrupt?.() : false; },
      destroy() { destroyed = true; open.clear(); host.remove(); container.classList.remove('deeporca-host'); surface?.classList.remove('deeporca-surface'); },
    };
  }
  return {createView, createDeepOrcaChatView:createView, markdown, safeUrl, summarizeTool, toolStatus};
});