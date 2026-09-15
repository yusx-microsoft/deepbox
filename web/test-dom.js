// Minimal, dependency-free DOM/browser doubles for Node orchestration tests.
// Not a layout engine. Replacing nodes REALLY detaches them, exposing lifetime bugs.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

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
    this.disabled = false;
    this.hidden = false;
    this.scrollTop = 0;
    this.scrollHeight = 100;
    this.clientHeight = 100;
    this.clientWidth = 800;
    this.classList = {
      contains: name => this.className.split(/\s+/).includes(name),
      add: (...names) => { this.className = [...new Set([...this.className.split(/\s+/), ...names])].join(' ').trim(); },
      remove: (...names) => { this.className = this.className.split(/\s+/).filter(item => !names.includes(item)).join(' '); },
      toggle: (name, value) => {
        const add = value ?? !this.classList.contains(name);
        add ? this.classList.add(name) : this.classList.remove(name);
        return add;
      },
    };
  }
  get id() { return this.attributes.id || ''; }
  set id(value) { this.attributes.id = String(value); }
  get className() { return this.attributes.class || ''; }
  set className(value) { this.attributes.class = String(value); }
  get isConnected() { return this === this.ownerDocument.body || !!this.parentNode?.isConnected; }
  get firstChild() { return this.children[0] || null; }
  get firstElementChild() { return this.children.find(child => child.tagName !== '#TEXT') || null; }
  get lastElementChild() { return this.children.filter(child => child.tagName !== '#TEXT').at(-1) || null; }
  get offsetWidth() { return this.clientWidth; }
  get offsetHeight() { return this.clientHeight; }
  get childElementCount() { return this.children.filter(child => child.tagName !== '#TEXT').length; }
  get textContent() { return (this._text || '') + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  get innerHTML() { return this._html || ''; }
  set innerHTML(html) {
    this.replaceChildren();
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
      for (const attr of token[2].matchAll(/([\w:-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\s/>]+)))?/g))
        node.setAttribute(attr[1], attr[2] ?? attr[3] ?? attr[4] ?? '');
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
  hasAttribute(key) { return key in this.attributes; }
  removeAttribute(key) {
    delete this.attributes[key];
    if (key === 'disabled' || key === 'hidden') this[key] = false;
  }
  appendChild(node) { node.remove(); node.parentNode = this; this.children.push(node); return node; }
  append(...nodes) {
    for (let node of nodes) {
      if (typeof node === 'string') node = this.ownerDocument.createTextNode(node);
      this.appendChild(node);
    }
  }
  insertBefore(node, reference) {
    if (!reference) return this.appendChild(node);
    const index = this.children.indexOf(reference);
    if (index < 0) throw new Error('insertBefore reference must belong to its parent');
    node.remove(); node.parentNode = this; this.children.splice(index, 0, node); return node;
  }
  replaceChildren(...nodes) {
    for (const child of this.children) child.parentNode = null;
    this.children = []; this._text = ''; this._html = ''; this.append(...nodes);
  }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this);
    this.parentNode = null;
  }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
  matches(selector) {
    if (this.tagName === '#TEXT') return false;
    if (selector.includes(',')) return selector.split(',').some(part => this.matches(part.trim()));
    for (const attr of selector.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g))
      if (!(attr[1] in this.attributes) || attr[2] !== undefined && this.attributes[attr[1]] !== attr[2]) return false;
    const simple = selector.replace(/\[[^\]]*\]/g, '');
    const tag = simple.match(/^[\w-]+/), id = simple.match(/#([\w-]+)/);
    const classes = [...simple.matchAll(/\.([\w-]+)/g)].map(match => match[1]);
    return (!tag || this.tagName.toLowerCase() === tag[0]) && (!id || this.id === id[1])
      && classes.every(name => this.classList.contains(name));
  }
  closest(selector) {
    for (let node = this; node; node = node.parentNode) if (node.matches(selector)) return node;
    return null;
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
  addEventListener(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(callback);
  }
  removeEventListener(type, callback) { this.listeners.get(type)?.delete(callback); }
  dispatchEvent(event) {
    event.target ||= this;
    event.currentTarget = this;
    event.preventDefault ||= function () { this.defaultPrevented = true; };
    event.stopPropagation ||= function () { this.cancelBubble = true; };
    this['on' + event.type]?.(event);
    for (const callback of [...(this.listeners.get(event.type) || [])]) callback(event);
    if (event.bubbles && !event.cancelBubble) this.parentNode?.dispatchEvent(event);
    return !event.defaultPrevented;
  }
  click() { if (!this.disabled) this.dispatchEvent({ type: 'click', bubbles: true }); }
  focus() { if (!this.disabled) this.ownerDocument.activeElement = this; }
  getBoundingClientRect() { return { width: this.clientWidth, height: this.clientHeight, left: 0, top: 0, right:this.clientWidth, bottom:this.clientHeight }; }
}

function createDocument() {
  const document = {};
  document.body = new Element(document, 'body');
  document.activeElement = document.body;
  document.createElement = tag => new Element(document, tag);
  document.createTextNode = text => { const node = document.createElement('#text'); node.textContent = text; return node; };
  document.querySelector = selector => document.body.querySelector(selector);
  document.querySelectorAll = selector => document.body.querySelectorAll(selector);
  document.getElementById = id => document.querySelector('#' + id);
  document.addEventListener = document.body.addEventListener.bind(document.body);
  document.removeEventListener = document.body.removeEventListener.bind(document.body);
  return document;
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function createBrowser({ terminal = true, observer = true } = {}) {
  const document = createDocument();
  const sockets = [], terminals = [], observers = [], fileReaders = [];
  const timers = new Map(), windowListeners = new Map();
  let timerId = 0;
  function addTimer(callback, ms, repeat = false) {
    const id = ++timerId; timers.set(id, { callback, ms, repeat }); return id;
  }
  class Socket {
    constructor(url) { this.url = url; this.readyState = 0; this.frames = []; sockets.push(this); }
    open() { this.readyState = 1; this.onopen?.(); }
    receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
    send(raw) {
      if (this.fail || this.readyState !== 1) throw new Error('socket unavailable');
      this.frames.push(JSON.parse(raw));
    }
    close() { this.readyState = 3; this.closeCount = (this.closeCount || 0) + 1; this.onclose?.(); }
  }
  class Terminal {
    constructor(options) {
      this.options = options; this.cols = 120; this.rows = 30; this.output = ''; terminals.push(this);
    }
    loadAddon(addon) { addon.terminal = this; }
    open(host) {
      this.host = host; this.input = document.createElement('textarea');
      this.input.className = 'xterm-helper-textarea'; host.appendChild(this.input);
    }
    onData(callback) { this.data = callback; return { dispose: () => { this.data = null; this.subscriptionDisposed = true; } }; }
    emit(data) { this.data?.(data); }
    write(data, callback) { this.output += data; callback?.(); }
    reset() { this.output = ''; }
    resize(cols, rows) { this.cols = cols; this.rows = rows; }
    focus() { this.input.focus(); }
    scrollToBottom() {}
    dispose() { this.disposed = true; }
  }
  class ResizeObserver {
    constructor(callback) { this.callback = callback; observers.push(this); }
    observe(root) { this.root = root; }
    disconnect() { this.disconnected = true; }
  }
  class FileReader {
    constructor() { fileReaders.push(this); }
    readAsDataURL(file) {
      this.file = file;
      if (!file.deferRead) queueMicrotask(() => this.complete());
    }
    complete() {
      if (this.aborted) return;
      this.result = 'data:' + (this.file.type || '') + ';base64,' + Buffer.from(this.file.text || 'file').toString('base64');
      this.onload?.();
    }
    abort() { this.aborted = true; this.onabort?.(); }
  }
  const window = {
    document, console, URL, URLSearchParams, AbortController, FileReader,
    TextEncoder, TextDecoder, setTimeout: (callback, ms) => addTimer(callback, ms),
    clearTimeout: id => timers.delete(id), setInterval: (callback, ms) => addTimer(callback, ms, true),
    clearInterval: id => timers.delete(id), requestAnimationFrame: callback => addTimer(callback, 16),
    cancelAnimationFrame: id => timers.delete(id), queueMicrotask,
    location: { protocol: 'http:', host: 'test.invalid', origin:'http://test.invalid', pathname: '/', search: '', hash: '' },
    innerWidth:1200, innerHeight:800,
    WebSocket: Socket, navigator: { userAgent: 'Node test' },
    addEventListener(type, callback) {
      if (!windowListeners.has(type)) windowListeners.set(type, new Set());
      windowListeners.get(type).add(callback);
    },
    removeEventListener(type, callback) { windowListeners.get(type)?.delete(callback); },
    dispatchEvent(event) { for (const callback of [...(windowListeners.get(event.type) || [])]) callback(event); },
  };
  window.window = window;
  if (terminal) {
    window.Terminal = Terminal;
    window.FitAddon = { FitAddon: class { fit() { this.fits = (this.fits || 0) + 1; } } };
  }
  if (observer) window.ResizeObserver = ResizeObserver;
  const context = vm.createContext(window);
  document.defaultView = context;
  const modules = new Map();
  function loadModule(file) {
    const filename = path.resolve(__dirname, file);
    if (modules.has(filename)) return modules.get(filename).exports;
    const module = { exports: {} }; modules.set(filename, module);
    const code = fs.readFileSync(filename, 'utf8');
    const execute = vm.runInContext('(function(exports, require, module, __filename, __dirname) {\n' + code + '\n})', context, { filename });
    execute(module.exports, name => {
      if (!name.startsWith('.')) return require(name);
      let next = path.resolve(path.dirname(filename), name);
      if (!path.extname(next)) next += '.js';
      return loadModule(next);
    }, module, filename, path.dirname(filename));
    return module.exports;
  }
  function loadScript(file) {
    const filename = path.resolve(__dirname, file);
    return vm.runInContext(fs.readFileSync(filename, 'utf8'), context, { filename });
  }
  function runTimer(id) {
    const timer = timers.get(id);
    if (!timer) return;
    if (!timer.repeat) timers.delete(id);
    timer.callback();
  }
  function type(text) {
    const active = document.activeElement;
    const terminal = terminals.find(item => item.input === active && !item.disposed);
    if (terminal) { if (!terminal.options.disableStdin) terminal.emit(text); }
    else if (active && !active.disabled && /^(INPUT|TEXTAREA)$/.test(active.tagName)) active.value += text;
  }
  return { document, window: context, context, sockets, terminals, timers, observers, fileReaders, windowListeners,
    loadModule, loadScript, runTimer, type };
}

module.exports = { Element, createDocument, createBrowser, deferred };
