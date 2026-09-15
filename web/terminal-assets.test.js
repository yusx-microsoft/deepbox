const test=require('node:test');
const assert=require('node:assert/strict');
const {createBrowser}=require('./test-dom.js');
async function flush(){for(let i=0;i<10;i++)await Promise.resolve();}
function setup(){
  const h=createBrowser({terminal:false});
  h.window.FitAddon=undefined;
  const assets=h.loadModule('./terminal-assets.js').createTerminalAssets({document:h.document,window:h.window});
  return {...h,assets};
}

test('the optional renderer makes no request at module load and coalesces explicit demand',async()=>{
  const h=setup();
  assert.equal(h.document.querySelectorAll('script,link').length,0);
  const first=h.assets.ready(),second=h.assets.ready();
  assert.strictEqual(first,second);
  const css=h.document.querySelector('link'),terminal=h.document.querySelector('script');
  assert.match(css.href,/cdn\.jsdelivr\.net\/npm\/xterm@5\.3\.0/);
  css.onload();h.window.Terminal=function(){};terminal.onload();await flush();
  const addon=h.document.querySelectorAll('script')[1];
  assert.match(addon.src,/xterm-addon-fit@0\.8\.0/);
  h.window.FitAddon={FitAddon:function(){}};addon.onload();await first;
  assert.equal(h.timers.size,0);
  await h.assets.ready();assert.equal(h.document.querySelectorAll('script').length,2);
});

test('failed styles do not become a false success merely because the script loaded',async()=>{
  const h=setup();h.window.FitAddon={FitAddon:function(){}};
  const pending=h.assets.ready(),failure=assert.rejects(pending,/could not load/);
  h.document.querySelector('link').onerror();
  h.window.Terminal=function(){};h.document.querySelector('script').onload();await failure;
  const retry=h.assets.ready();
  assert.ok(h.document.querySelector('link'));
  h.document.querySelector('link').onload();await retry;
  assert.equal(h.timers.size,0);
  assert.equal(h.document.querySelectorAll('script').length,1);
});

test('renderer timeouts clean failed tags and permit a later retry',async()=>{
  const h=setup(),pending=h.assets.ready();
  const failure=assert.rejects(pending,/timed out/);
  for(const id of [...h.timers.keys()])h.runTimer(id);
  await failure;
  assert.equal(h.timers.size,0);
  assert.equal(h.document.querySelectorAll('script,link').length,0);
});

test('a host-supplied renderer needs no CDN dependency',async()=>{
  const h=createBrowser();
  const assets=h.loadModule('./terminal-assets.js').createTerminalAssets({document:h.document,window:h.window});
  await assets.ready();
  assert.equal(h.document.querySelectorAll('script,link').length,0);
});
