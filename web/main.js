/* Browser composition only. The modules have no automatic network side effects. */
(function(){
  'use strict';
  const root = document.getElementById('app');
  let application = null;
  try {
    application = AgentBridgeApp.createApp({root});
    application.start().catch(()=>{
      root.textContent = 'AgentBridge could not start. Refresh the page to retry.';
    });
  } catch {
    root.textContent = 'AgentBridge could not load its interface. Refresh the page to retry.';
  }
  window.addEventListener('pagehide',()=>application?.destroy(),{once:true});
})();
