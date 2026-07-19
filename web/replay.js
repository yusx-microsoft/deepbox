(function(root, factory){
  const api = factory();
  if(typeof module === 'object' && module.exports) module.exports = api;
  if(root) root.DeepboxReplay = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function(){
  function nearestCheckpointIndex(checkpoints, targetTime){
    let found = -1;
    for(let i=0; i<(checkpoints||[]).length; i++){
      if(checkpoints[i].time <= targetTime) found = i;
      else break;
    }
    return found;
  }

  function eventsBetween(events, startTime, target, startCursor){
    return (events||[]).filter(event => {
      if(typeof event.time !== 'number' || event.time > target) return false;
      if(startCursor !== undefined && startCursor !== null)
        return event.cursor > startCursor;
      return event.time > startTime;
    });
  }

  function normalizeReplay(payload){
    const events = (Array.isArray(payload.events) ? payload.events : []).map((event, index) => ({
      ...event,
      cursor: event.cursor ?? event.frame_id ?? event.index ?? index,
      type: event.type ?? event.kind ?? 'o',
    }));
    const checkpoints = (Array.isArray(payload.checkpoints) ? payload.checkpoints : []).map(cp => ({
      ...cp,
      cursor: cp.cursor ?? cp.frame_id ?? cp.event_index,
      serialized_screen: cp.serialized_screen ?? cp.screen ?? '',
    })).filter(cp => typeof cp.time === 'number');
    events.sort((a,b)=>(a.time-b.time)||((a.cursor||0)-(b.cursor||0)));
    checkpoints.sort((a,b)=>(a.time-b.time)||((a.cursor||0)-(b.cursor||0)));
    return Object.assign({}, payload, {events, checkpoints});
  }

  function formatClock(seconds){
    const value = Math.max(0, Number(seconds)||0);
    const minutes = Math.floor(value/60);
    const remainder = Math.floor(value%60);
    return `${minutes}:${String(remainder).padStart(2,'0')}`;
  }

  return {nearestCheckpointIndex, eventsBetween, normalizeReplay, formatClock};
});
