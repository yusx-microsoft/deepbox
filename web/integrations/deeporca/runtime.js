/* DeepOrca's small workbench contract. No transport, credentials or native globals.
 * Shared pane/replay code owns delivery, ACL checks and authoritative snapshots. */
(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AgentBridgeDeepOrcaRuntime = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function() {
  'use strict';
  const renderer = 'deeporca-chat-v1';
  function matches(agent, capability, rendererId) {
    return agent?.runtime === 'deeporca' || capability?.family === 'deeporca' || rendererId === renderer;
  }
  function ownsEvent(state, event) {
    return state.renderer === renderer || event.native?.runtime === 'deeporca';
  }
  function foldEvent(state, event) {
    if (!ownsEvent(state, event)) return false;
    if (event.ev === 'status') {
      const kind = event.subtype || event.native?.type || 'status';
      state.status = event.note || kind;
      // Keep latest metadata per kind, not noisy transcript/tool rows. Completed
      // turns retain their own native usage in the shared event reducer.
      if (!state.runtimeStatus) state.runtimeStatus = Object.create(null);
      state.runtimeStatus['native:' + kind] = {
        label:kind, content:event.native?.usage ?? event.native ?? event.note,
      };
      return true;
    }
    const statusKey = (event.turn_id || '') + ':' + (event.tool_id || '');
    if (event.ev === 'permission.ask') { state.pendingPermission = null; return true; }
    if (event.ev === 'turn.end') {
      state.pendingPermission = null;
      for (const item of state.items) {
        if (item.kind === 'tool' && item.turn_id === event.turn_id && item.result == null)
          item.status = event.status === 'completed' ? 'incomplete' : event.status || 'uncertain';
      }
      return false;
    }
    // Native status/usage callbacks are not tools. Keep them out of the transcript,
    // paired by canonical tool_id, and summarize them in the turn's quiet footer.
    if (event.ev === 'tool.call' && event.native?.kind === 'status') {
      if (!state.runtimeStatus) state.runtimeStatus = Object.create(null);
      state.runtimeStatus[statusKey] = {label:event.tool, input:event.input};
      return true;
    }
    if (event.ev === 'tool.result' && (event.native?.kind === 'status' || state.runtimeStatus?.[statusKey])) {
      if (!state.runtimeStatus) state.runtimeStatus = Object.create(null);
      state.runtimeStatus[statusKey] = Object.assign({}, state.runtimeStatus[statusKey], {content:event.content});
      return true;
    }
    return false;
  }
  return {renderer, matches, foldEvent, serialTurns:true, inputReceipts:true,
    label:'DeepOrca', agentUiModule:'runtime-catalog', requiresRegisteredProject:true,
    interactiveApproval:false, explicitContinuation:true};
});