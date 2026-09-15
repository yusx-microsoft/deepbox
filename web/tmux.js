/* UI-only tmux bindings. Nothing here evaluates or sends shell commands. */
(function(root, factory){
  const api = factory();
  if(typeof module === 'object' && module.exports) module.exports = api;
  else root.AgentBridgeTmux = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function(){
  'use strict';
  function isPrefix(event){
    return !!event.ctrlKey && !event.metaKey && !event.altKey && String(event.key).toLowerCase() === 'b';
  }
  function binding(event){
    if(isPrefix(event)) return {type:'send-prefix'};
    if(event.ctrlKey || event.metaKey || event.altKey) return null;
    if(/^\d$/.test(event.key)) return {type:'select',index:Number(event.key)};
    const actions = {
      '%':{type:'split',axis:'row'}, '"':{type:'split',axis:'column'},
      ArrowLeft:{type:'focus',direction:'left'}, ArrowRight:{type:'focus',direction:'right'},
      ArrowUp:{type:'focus',direction:'up'}, ArrowDown:{type:'focus',direction:'down'},
      o:{type:'cycle',step:1}, O:{type:'cycle',step:-1}, z:{type:'zoom'}, x:{type:'close'},
      w:{type:'tree'}, s:{type:'workspace'}, c:{type:'new-session'}, r:{type:'reconnect'},
      h:{type:'history'}, m:{type:'menu'}, '?':{type:'help'}, ':':{type:'command'},
      Escape:{type:'cancel'},
    };
    return actions[event.key] || null;
  }
  function command(text){
    if(typeof text !== 'string' || text.length > 160 || /[\r\n\0]/.test(text)) return null;
    const words = text.trim().split(/\s+/);
    const name = words[0], args = words.slice(1);
    if(['split-window','split'].includes(name) && args.length === 1){
      if(args[0] === '-h') return {type:'split',axis:'row'};
      if(args[0] === '-v') return {type:'split',axis:'column'};
    }
    if(name === 'select-pane'){
      const directions = {'-L':'left','-R':'right','-U':'up','-D':'down'};
      if(args.length === 1 && directions[args[0]]) return {type:'focus',direction:directions[args[0]]};
      if(args.length === 2 && args[0] === '-t' && /^\d$/.test(args[1])) return {type:'select',index:Number(args[1])};
    }
    if(name === 'resize-pane' && args.length === 1 && args[0] === '-Z') return {type:'zoom'};
    if(name === 'theme' && args.length === 1 && ['dark','light'].includes(args[0])) return {type:'theme',value:args[0]};
    if(args.length) return null;
    const actions = {
      'choose-tree':{type:'tree'}, agents:{type:'tree'},
      'choose-session':{type:'workspace'}, workspaces:{type:'workspace'},
      'next-pane':{type:'cycle',step:1}, 'previous-pane':{type:'cycle',step:-1},
      zoom:{type:'zoom'}, 'close-pane':{type:'close'}, close:{type:'close'},
      'new-session':{type:'new-session'}, new:{type:'new-session'},
      reconnect:{type:'reconnect'}, history:{type:'history'},
      'end-session':{type:'end-session'}, interrupt:{type:'interrupt'},
      'request-keyboard':{type:'request-keyboard'}, 'release-keyboard':{type:'release-keyboard'},
      help:{type:'help'}, 'list-keys':{type:'help'},
    };
    return actions[name] || null;
  }
  return {isPrefix,binding,command};
});
