/* JSON HTTP boundary. Session transport belongs to the pane, not this client. */
(function(root, factory){
  const common = typeof module === 'object' && module.exports;
  const api = factory(common ? require('./ui.js') : (root.AgentBridgeUI || root.DeepboxUI));
  if(common) module.exports = api;
  else root.AgentBridgeApi = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function(UI){
  'use strict';
  function createApi(fetch){
    return async function api(path, options={}){
      if(typeof path !== 'string' || !path.startsWith('/api/')) throw new TypeError('API paths must stay on this app');
      const response = await fetch(path, {credentials:'same-origin', ...options,
        headers:{...(options.body == null ? {} : {'Content-Type':'application/json'}), ...options.headers}});
      if(!response.ok){
        let body = await response.text();
        try { JSON.parse(body); } catch { body = ''; }
        const message = UI.apiErrorMessage(response.status, response.statusText, body);
        const error = new Error(message); error.status = response.status; throw error;
      }
      if(response.status === 204) return null;
      try { return await response.json(); }
      catch {
        const error = new Error('The server did not return JSON. Refresh or sign in again.');
        error.status = response.status; throw error;
      }
    };
  }
  return {createApi};
});
