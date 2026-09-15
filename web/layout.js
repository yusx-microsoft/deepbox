/* Small, serializable split layout. No DOM, sessions, or runtime knowledge. */
(function(root, factory){
  const api = factory();
  if(typeof module === 'object' && module.exports) module.exports = api;
  else root.AgentBridgeLayout = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function(){
  'use strict';
  const MAX_PANES = 4;
  const MIN_RATIO = 0.15;
  const MAX_RATIO = 0.85;
  const validId = id=>typeof id === 'string' && /^[a-zA-Z0-9_-]{1,64}$/.test(id);
  const ratio = value=>Math.max(MIN_RATIO, Math.min(MAX_RATIO, Number.isFinite(value) ? value : 0.5));

  function pane(id){
    if(!validId(id)) throw new TypeError('Invalid pane id');
    return {type:'pane', id};
  }

  function ids(node){
    if(!node) return [];
    return node.type === 'pane' ? [node.id] : [...ids(node.first), ...ids(node.second)];
  }

  // DFS-ordered rectangles in a unit square, even when the DOM is zoomed.
  function rects(node){
    const result = [];
    function visit(item, x, y, width, height){
      if(!item) return;
      if(item.type === 'pane'){
        result.push({id:item.id, x, y, width, height});
        return;
      }
      const fraction = ratio(item.ratio);
      if(item.axis === 'row'){
        const firstWidth = width * fraction;
        visit(item.first, x, y, firstWidth, height);
        visit(item.second, x + firstWidth, y, width - firstWidth, height);
      } else {
        const firstHeight = height * fraction;
        visit(item.first, x, y, width, firstHeight);
        visit(item.second, x, y + firstHeight, width, height - firstHeight);
      }
    }
    visit(node, 0, 0, 1, 1);
    return result;
  }

  function neighbor(node, id, direction){
    if(!['left', 'right', 'up', 'down'].includes(direction)) return null;
    const panes = rects(node), source = panes.find(item=>item.id === id);
    if(!source) return null;
    const horizontal = direction === 'left' || direction === 'right';
    const forward = direction === 'right' || direction === 'down';
    const edge = horizontal ? source.x + (forward ? source.width : 0)
      : source.y + (forward ? source.height : 0);
    const epsilon = 1e-9;
    let nearest = null, distance = Infinity;
    for(const candidate of panes){
      if(candidate.id === id) continue;
      const otherEdge = horizontal ? candidate.x + (forward ? 0 : candidate.width)
        : candidate.y + (forward ? 0 : candidate.height);
      const overlap = horizontal
        ? Math.min(source.y + source.height, candidate.y + candidate.height) - Math.max(source.y, candidate.y)
        : Math.min(source.x + source.width, candidate.x + candidate.width) - Math.max(source.x, candidate.x);
      // Sharing only a corner is not adjacency. Equal distances keep DFS order.
      if(Math.abs(edge - otherEdge) > epsilon || overlap <= epsilon) continue;
      const dx = source.x + source.width / 2 - candidate.x - candidate.width / 2;
      const dy = source.y + source.height / 2 - candidate.y - candidate.height / 2;
      const nextDistance = dx * dx + dy * dy;
      if(nextDistance < distance - epsilon){ nearest = candidate.id; distance = nextDistance; }
    }
    return nearest;
  }

  function split(node, targetId, newId, axis){
    if(!['row', 'column'].includes(axis)) throw new TypeError('Invalid split axis');
    if(!validId(newId)) throw new TypeError('Invalid pane id');
    const current = ids(node);
    if(current.length >= MAX_PANES || current.includes(newId) || !current.includes(targetId)) return node;
    function visit(item){
      if(item.type === 'pane') return item.id === targetId
        ? {type:'split', axis, ratio:0.5, first:item, second:pane(newId)} : item;
      const first = visit(item.first), second = visit(item.second);
      return first === item.first && second === item.second ? item : {...item, first, second};
    }
    return visit(node);
  }

  function remove(node, targetId){
    if(!node) return null;
    if(node.type === 'pane') return node.id === targetId ? null : node;
    const first = remove(node.first, targetId), second = remove(node.second, targetId);
    if(!first) return second;
    if(!second) return first;
    return first === node.first && second === node.second ? node : {...node, first, second};
  }

  function resize(node, path, value){
    if(!Array.isArray(path) || path.some(part=>part !== 'first' && part !== 'second')) return node;
    function visit(item, depth){
      if(!item || item.type !== 'split') return item;
      if(depth === path.length){
        const next = ratio(value);
        return next === item.ratio ? item : {...item, ratio:next};
      }
      const key = path[depth], child = visit(item[key], depth + 1);
      return child === item[key] ? item : {...item, [key]:child};
    }
    return visit(node, 0);
  }

  function at(node, path){
    let current = node;
    for(const part of path){
      if(current?.type !== 'split' || !['first', 'second'].includes(part)) return null;
      current = current[part];
    }
    return current;
  }

  function sanitize(value){
    const seen = new Set();
    function visit(node, depth){
      if(!node || typeof node !== 'object' || depth > MAX_PANES) return null;
      if(node.type === 'pane'){
        if(!validId(node.id) || seen.has(node.id) || seen.size >= MAX_PANES) return null;
        seen.add(node.id);
        return pane(node.id);
      }
      if(node.type !== 'split' || !['row', 'column'].includes(node.axis)) return null;
      const first = visit(node.first, depth + 1), second = visit(node.second, depth + 1);
      return first && second ? {type:'split', axis:node.axis, ratio:ratio(node.ratio), first, second} : null;
    }
    return visit(value, 0);
  }

  return {MAX_PANES, MIN_RATIO, MAX_RATIO, pane, ids, rects, neighbor, split, remove, resize, at, sanitize};
});
