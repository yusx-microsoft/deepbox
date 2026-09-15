/* Optional terminal renderer. Chat/layout must not wait on a third-party CDN. */
(function(root, factory){
  const api = factory();
  if(typeof module === 'object' && module.exports) module.exports = api;
  else root.AgentBridgeTerminalAssets = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function(){
  'use strict';
  const BASE = 'https://cdn.jsdelivr.net/npm/';
  function createTerminalAssets({document, window, timeoutMs=12000}){
    // null permits a renderer supplied by the host (including isolated tests).
    let pending = null, stylesReady = null;
    function load(tag, url){
      return new Promise((resolve,reject)=>{
        const element = document.createElement(tag);
        let settled = false;
        const finish = error=>{
          if(settled) return;
          settled = true;
          window.clearTimeout(timer);
          element.onload = element.onerror = null;
          if(error){ element.remove(); reject(error); } else resolve();
        };
        const timer = window.setTimeout(()=>finish(new Error('Terminal renderer timed out. Check access to cdn.jsdelivr.net, then retry.')),timeoutMs);
        element.onload = ()=>finish();
        element.onerror = ()=>finish(new Error('Terminal renderer could not load. Chat remains available; check network access and retry.'));
        if(tag === 'link'){ element.rel = 'stylesheet'; element.href = url; }
        else { element.src = url; element.async = true; }
        (document.head || document.body).appendChild(element);
      });
    }
    function ready(){
      if(window.Terminal && window.FitAddon?.FitAddon && stylesReady !== false) return Promise.resolve();
      if(!pending){
        if(stylesReady === null) stylesReady = false;
        pending = Promise.allSettled([
          stylesReady ? Promise.resolve() : load('link',BASE+'xterm@5.3.0/css/xterm.css').then(()=>{stylesReady=true;}),
          (window.Terminal ? Promise.resolve() : load('script',BASE+'xterm@5.3.0/lib/xterm.js'))
            .then(()=>window.FitAddon?.FitAddon ? undefined : load('script',BASE+'xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js')),
        ]).then(results=>{
          const failed = results.find(result=>result.status === 'rejected');
          if(failed) throw failed.reason;
          if(!window.Terminal || !window.FitAddon?.FitAddon) throw new Error('Terminal renderer did not initialize. Retry or use Chat.');
        }).finally(()=>{pending=null;});
      }
      return pending;
    }
    return {ready};
  }
  return {createTerminalAssets};
});
