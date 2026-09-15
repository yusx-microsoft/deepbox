const test=require('node:test');
const assert=require('node:assert/strict');
const T=require('./tmux.js');

test('prefix is literal Control+B, not browser Meta+B or an Alt shortcut',()=>{
  assert.equal(T.isPrefix({key:'b',ctrlKey:true}),true);
  assert.equal(T.isPrefix({key:'B',ctrlKey:true}),true);
  assert.equal(T.isPrefix({key:'b',metaKey:true}),false);
  assert.equal(T.isPrefix({key:'b',ctrlKey:true,altKey:true}),false);
});

test('tmux split, focus, zoom and control-passthrough bindings are explicit',()=>{
  assert.deepEqual(T.binding({key:'%'}),{type:'split',axis:'row'});
  assert.deepEqual(T.binding({key:'"'}),{type:'split',axis:'column'});
  assert.deepEqual(T.binding({key:'ArrowLeft'}),{type:'focus',direction:'left'});
  assert.deepEqual(T.binding({key:'o'}),{type:'cycle',step:1});
  assert.deepEqual(T.binding({key:'O'}),{type:'cycle',step:-1});
  assert.deepEqual(T.binding({key:'z'}),{type:'zoom'});
  assert.deepEqual(T.binding({key:'x'}),{type:'close'});
  assert.deepEqual(T.binding({key:'3'}),{type:'select',index:3});
  assert.deepEqual(T.binding({key:'b',ctrlKey:true}),{type:'send-prefix'});
  assert.equal(T.binding({key:'d',ctrlKey:true}),null);
});

test('the command prompt parses UI actions, never arbitrary shell or model text',()=>{
  assert.deepEqual(T.command(' split-window -h '),{type:'split',axis:'row'});
  assert.deepEqual(T.command('split-window -v'),{type:'split',axis:'column'});
  assert.deepEqual(T.command('select-pane -t 2'),{type:'select',index:2});
  assert.deepEqual(T.command('select-pane -D'),{type:'focus',direction:'down'});
  assert.deepEqual(T.command('resize-pane -Z'),{type:'zoom'});
  assert.deepEqual(T.command('theme light'),{type:'theme',value:'light'});
  assert.deepEqual(T.command('end-session'),{type:'end-session'});
  for(const value of ['rm -rf /','run-shell echo hello','kill-pane','split -h; bad','select-pane -t 999','theme neon','help\nend-session','x'.repeat(200)]){
    assert.equal(T.command(value),null,value);
  }
});
