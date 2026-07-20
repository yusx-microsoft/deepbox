(function(root, factory){
  const api = factory();
  if(typeof module === 'object' && module.exports) module.exports = api;
  if(root) root.DeepboxCollaboration = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function(){
  // Roles that are allowed to type when they hold the keyboard lease.
  const OPERATOR_ROLES = ['operator', 'admin', 'owner'];

  // deriveCollaborationState(frame, currentUser?)
  //   frame       : the raw {type:'collaboration', ...} message from the server.
  //   currentUser : optional {id, username} of the logged-in user, used as a
  //                 fallback when the server does not stamp is_holder/can_request.
  // Returns a normalized, DOM-free view model consumed by the UI and canSendInput.
  function deriveCollaborationState(frame, currentUser){
    frame = frame || {};
    const keyboard = frame.keyboard || {};
    const role = frame.role || 'viewer';
    const isViewer = role === 'viewer';
    const canOperate = OPERATOR_ROLES.indexOf(role) !== -1;

    const holderUserId = keyboard.holder_user_id != null ? keyboard.holder_user_id : null;
    const holderUsername = keyboard.holder_username != null ? keyboard.holder_username : null;
    const expiresAt = keyboard.expires_at != null ? keyboard.expires_at : null;
    const heldByAnyone = holderUserId != null;

    // Prefer the server's authoritative flags; fall back to matching the current
    // user against the holder id when the server omits is_holder.
    let isHolder;
    if(typeof keyboard.is_holder === 'boolean') isHolder = keyboard.is_holder;
    else isHolder = !!(currentUser && currentUser.id != null && currentUser.id === holderUserId);
    if(isViewer) isHolder = false;

    const heldByOther = heldByAnyone && !isHolder;

    // can_request from the server wins; otherwise operators may request when they
    // are not already holding the lease.
    let canRequest;
    if(typeof keyboard.can_request === 'boolean') canRequest = keyboard.can_request;
    else canRequest = canOperate && !isHolder;
    if(isViewer) canRequest = false;

    const canRelease = isHolder;

    let status;
    if(isHolder) status = 'holding';
    else if(heldByOther) status = 'busy';
    else status = 'free';

    return {
      sessionId: frame.session_id != null ? frame.session_id : null,
      role,
      isViewer,
      canOperate,
      holderUserId,
      holderUsername,
      expiresAt,
      heldByAnyone,
      heldByOther,
      isHolder,
      canRequest,
      canRelease,
      status,
    };
  }

  // canSendInput(state): only the current keyboard holder may transmit input.
  function canSendInput(state){
    return !!(state && state.isHolder === true);
  }

  // collabHeaderView(state, requester?)
  //   Pure view model for the terminal header keyboard badge. A null/absent
  //   state means the collaboration frame has not arrived yet (attach in
  //   flight) — we surface an explicit 'pending' label instead of a blank badge
  //   with silently disabled stdin, so the terminal is never mysteriously
  //   untypable. requester is the optional {username} currently asking for the
  //   keyboard when the local user holds it.
  function collabHeaderView(state, requester){
    if(!state) return {cls: 'collab-pending', label: 'connecting\u2026',
                       button: null, canType: false};
    if(state.isViewer) return {cls: 'collab-viewer', label: 'read-only',
                               button: null, canType: false};
    if(state.isHolder){
      return requester
        ? {cls: 'collab-holder',
           label: requester.username + ' requests the keyboard',
           button: 'handoff', canType: true}
        : {cls: 'collab-holder', label: 'you have the keyboard',
           button: 'release', canType: true};
    }
    if(state.heldByOther){
      return {cls: 'collab-busy',
              label: (state.holderUsername || 'someone') + ' is typing',
              button: state.canRequest ? 'request' : null, canType: false};
    }
    return {cls: 'collab-free', label: 'keyboard free',
            button: state.canRequest ? 'request' : null, canType: false};
  }

  return {deriveCollaborationState, canSendInput, collabHeaderView,
          OPERATOR_ROLES};
});
