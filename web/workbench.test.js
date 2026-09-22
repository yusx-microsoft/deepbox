const test = require('node:test');
const assert = require('node:assert/strict');
const W = require('./workbench.js');
const L = require('./layout.js');

test('layout storage is isolated by both signed-in user and workspace', () => {
  assert.notEqual(W.storageKey('alice','one'), W.storageKey('alice','two'));
  assert.notEqual(W.storageKey('alice','one'), W.storageKey('bob','one'));
  assert.notEqual(W.storageKey('a.b','c'), W.storageKey('a','b.c'));
  assert.equal(W.storageKey(null,'one'), null);
});

test('pane persistence contains only view identities, not drafts, permissions or credentials', () => {
  const target = {kind:'live',agentId:'a',sessionId:'s',surface:'structured',
    title:'private title',text:'private prompt',attachments:['private'],token:'private',
    canOperate:true,forceNew:true,restore:false};
  assert.deepEqual(W.persistentTarget(target), {kind:'live',agentId:'a',sessionId:'s',surface:'structured'});
  assert.equal(W.persistentTarget({kind:'live',agentId:'a',forceNew:true}), null,
    'a selected agent without a known session must not be auto-created on reload');
  assert.deepEqual(W.persistentTarget({kind:'history',agentId:'a',sessionId:'unused'}), {kind:'history',agentId:'a'});
  assert.equal(W.persistentTarget({kind:'replay',agentId:'a',sessionId:'../unsafe'}), null);
});

test('saved workbench validates version, active pane, identities and targets', () => {
  const tree = L.split(L.pane('left'),'left','right','row');
  const value = JSON.stringify({version:1,tree,active:'missing',panes:[
    {id:'left',target:{kind:'live',agentId:'a',sessionId:'s',surface:'structured',forceNew:true}},
    {id:'right',target:{kind:'replay',agentId:'b',sessionId:'old',surface:'terminal'}},
    {id:'not-in-layout',target:{kind:'live',agentId:'c',sessionId:'hidden'}},
  ]});
  const saved = W.decodeSaved(value);
  assert.equal(saved.active, 'left');
  assert.deepEqual(saved.tree, tree);
  assert.equal(saved.targets.size, 2);
  assert.equal(saved.targets.get('left').forceNew, undefined);
  assert.equal(W.decodeSaved('{broken'), null);
  assert.equal(W.decodeSaved(JSON.stringify({version:2,tree})), null);
  assert.equal(W.decodeSaved(' '.repeat(17000)), null);
  assert.equal(W.decodeSaved(JSON.stringify({version:1,tree:{type:'pane',id:'bad id'}})), null);
});

const {createBrowser,deferred}=require('./test-dom.js');
function mountedWorkbench(saved=null){
  const browser=createBrowser(), root=browser.document.createElement('main'); browser.document.body.appendChild(root);
  const values=new Map(saved?[['layout',saved]]:[]), controllers=[], menus=[], notices=[];
  const writes=[], layoutChanges=[], activeChanges=[], choices=[];
  const storage={getItem:key=>values.get(key)??null,setItem:(key,value)=>{writes.push(value);values.set(key,value);}};
  let blocked=null;
  const bench=W.createWorkbench({root,storage,storageKey:'layout',services:{
    chooseAgent:async state=>{choices.push(state);return null;},defaultSurface:()=> 'structured',notice:message=>notices.push(message),
    menu:(anchor,items)=>menus.push({anchor,items}),
    onLayoutChange:state=>layoutChanges.push(state),onActiveChange:state=>activeChanges.push(state),
  },createPane:options=>{
    const state={kind:'empty',status:'empty',statusText:'',canOperate:false,canEndSession:false,readOnly:false,
      keyboardOwned:false,keyboardBusy:false,keyboardHolder:'',keyboardPending:false,keyboardRequestPending:false,
      pending:false,permissionPending:false};
    const controller={id:options.id,root:options.root,opens:[],closes:0,resizes:0,focuses:0,snapshots:0,actions:[],
      async open(target){
        this.opens.push(target); Object.assign(state,target,{status:'opening'}); options.services.onChange(state);
        if(blocked) await blocked.promise;
        if(this.closes) return;
        if(target.kind==='live'&&!target.sessionId)state.sessionId='session-'+target.agentId;
        state.canOperate=true;state.title=target.agentId;state.status='live';
        state.readOnly=target.surface==='terminal';
        options.root.innerHTML='<textarea data-draft></textarea>';
        options.services.onChange(state);
      },
      getState:()=>({...state}),snapshot(){this.snapshots++;return {...state};},
      updateState(value={}){Object.assign(state,value);options.services.onChange(state);},
      close(){this.closes++;},resize(){this.resizes++;},focus(){this.focuses++;},
      reconnect(){this.actions.push('reconnect');},newSession(){this.actions.push('newSession');},
      interrupt(){this.actions.push('interrupt');},
      endSession(){if(!this.allowEnd)throw new Error('layout may not terminate sessions');this.actions.push('endSession');},
      showHistory(){this.actions.push('showHistory');},requestKeyboard(){this.actions.push('requestKeyboard');},
      releaseKeyboard(){this.actions.push('releaseKeyboard');},handoffKeyboard(){this.actions.push('handoffKeyboard');},
    };
    controllers.push(controller);return controller;
  }});
  return {...browser,root,bench,controllers,values,menus,notices,writes,layoutChanges,activeChanges,choices,block:value=>{blocked=value;}};
}

function paneMenu(h, id=h.bench.getState().active, action='menu'){
  const button=h.bench.getPane(id).root.parentNode.querySelector(`[data-action="${action}"]`);button.click();
  assert.strictEqual(h.menus.at(-1).anchor,button);
  return h.menus.at(-1).items.filter(item=>!item.separator);
}
function splitMenu(h,id){return paneMenu(h,id,'split');}

test('split, maximize and restore retain pane controllers and drafts without new sessions',async()=>{
  const h=mountedWorkbench();
  await h.bench.open({kind:'live',agentId:'a',surface:'structured'});
  const first=h.bench.getActive(), input=first.root.querySelector('[data-draft]'); input.value='unfinished';
  const secondId=h.bench.split(first.id,'row');
  await h.bench.open({kind:'live',agentId:'b',surface:'structured'},secondId);
  const second=h.bench.getActive();
  h.bench.toggleMaximize(first.id);
  assert.equal(h.bench.getState().maximized,first.id);
  assert.equal(second.root.parentNode.hidden,true);
  h.bench.toggleMaximize(first.id);
  assert.equal(h.bench.getState().maximized,null);
  assert.strictEqual(first.root.querySelector('[data-draft]'),input);
  assert.equal(input.value,'unfinished');
  assert.equal(first.opens.length,1);assert.equal(second.opens.length,1);
  assert.equal(first.closes,0);assert.equal(second.closes,0);
  h.bench.closePane(second.id);
  assert.equal(second.closes,1);assert.equal(first.closes,0);
  h.bench.close();assert.equal(first.closes,1);
});

test('the same known live view focuses its existing pane rather than attaching twice',async()=>{
  const h=mountedWorkbench();
  await h.bench.open({kind:'live',agentId:'a',surface:'structured'});
  const first=h.bench.getActive();
  const secondId=h.bench.split(first.id,'column');
  await h.bench.open({kind:'live',agentId:'a',surface:'structured'},secondId);
  assert.equal(h.bench.getState().active,first.id);
  assert.equal(h.bench.getPane(secondId).opens.length,0);
  await h.bench.open({kind:'live',agentId:'a',sessionId:'session-a',surface:'structured'},secondId);
  assert.equal(h.bench.getPane(secondId).opens.length,0);
  h.bench.close();
});

test('passive pane clicks cannot leave typing focus in another pane',async()=>{
  const h=mountedWorkbench();await h.bench.open({kind:'live',agentId:'a',surface:'structured'});
  const first=h.bench.getActive();const input=first.root.querySelector('[data-draft]');input.focus();
  const secondId=h.bench.split();const second=h.bench.getPane(secondId);
  input.focus();const frame=second.root.parentNode;
  frame.dispatchEvent({type:'pointerdown',target:second.root});
  assert.equal(h.bench.getState().active,secondId);
  assert.equal(h.document.activeElement,frame);
  const before=second.focuses;
  frame.dispatchEvent({type:'pointerdown',target:frame.querySelector('.pane-status')});
  assert.equal(second.focuses,before+1);
  h.bench.close();
});

test('late pane opens do not steal focus from a new shell command prompt',async()=>{
  const h=mountedWorkbench(), waiting=deferred();h.block(waiting);
  const opening=h.bench.open({kind:'live',agentId:'a',surface:'structured'});
  const prompt=h.document.createElement('input');h.document.body.appendChild(prompt);prompt.focus();
  waiting.resolve();await opening;
  assert.equal(h.document.activeElement,prompt);
  h.bench.close();
});

test('dragging and keyboard resize update the layout without replacing a session DOM',async()=>{
  const h=mountedWorkbench();await h.bench.open({kind:'live',agentId:'a',surface:'structured'});
  const first=h.bench.getActive();h.bench.split(first.id,'row');
  const input=first.root.querySelector('[data-draft]');
  const separator=h.root.querySelector('[role="separator"]');
  separator.onkeydown({key:'ArrowRight',preventDefault(){}});
  assert.equal(h.bench.getState().tree.ratio,0.55);
  separator.onpointerdown({button:0,preventDefault(){}});
  h.window.dispatchEvent({type:'pointermove',clientX:560,clientY:0});
  h.window.dispatchEvent({type:'pointerup'});
  assert.equal(h.bench.getState().tree.ratio,0.7);
  assert.strictEqual(first.root.querySelector('[data-draft]'),input);
  assert.equal(first.opens.length,1);
  assert.equal(h.windowListeners.get('pointermove').size,0);
  assert.equal(JSON.parse(h.values.get('layout')).tree.ratio,0.7);
  h.bench.close();
});

test('four panes is a hard bound and closing the last one produces only a blank view',()=>{
  const h=mountedWorkbench();
  for(let i=0;i<3;i++)h.bench.split();
  assert.equal(h.controllers.length,4);
  assert.equal(h.bench.split(),null);assert.equal(h.controllers.length,4);
  assert.equal(h.notices.length,1);
  for(const controller of [...h.controllers])h.bench.closePane(controller.id);
  assert.equal(h.bench.getState().count,1);
  assert.equal(h.bench.getActive().opens.length,0);
  assert.ok(h.controllers.slice(0,4).every(controller=>controller.closes===1));
  h.bench.close();
});

test('a known saved session restores without forceNew and is not erased during initialization',async()=>{
  const saved=JSON.stringify({version:1,tree:L.pane('saved'),active:'saved',panes:[
    {id:'saved',target:{kind:'live',agentId:'a',sessionId:'known',surface:'structured',forceNew:true}},
  ]});
  const h=mountedWorkbench(saved);
  await Promise.resolve();
  assert.equal(h.controllers[0].opens[0].restore,true);
  assert.equal(h.controllers[0].opens[0].forceNew,undefined);
  assert.equal(JSON.parse(h.values.get('layout')).panes[0].target.sessionId,'known');
  h.bench.close();
});

test('closing a workbench cancels drag listeners and ignores a late pane open',async()=>{
  const h=mountedWorkbench(), pending=deferred();
  h.bench.split();
  h.root.querySelector('[role="separator"]').onpointerdown({button:0,preventDefault(){}});
  h.block(pending);
  const open=h.bench.open({kind:'live',agentId:'a',surface:'structured'});
  h.bench.close();pending.resolve();await open;
  assert.equal(h.root.children.length,0);
  assert.ok(h.controllers.every(controller=>controller.closes===1));
  assert.equal(h.windowListeners.get('pointermove').size,0);
  assert.equal(h.windowListeners.get('pointerup').size,0);
});

test('compact pane header has an agent chooser, readable badges and only split/overflow SVG tools',async()=>{
  const h=mountedWorkbench();
  assert.equal(h.root.querySelector('.pane-title').textContent,'Choose an agent');
  assert.equal(h.root.querySelector('.pane-mode').hidden,true);
  assert.equal(h.root.querySelector('.pane-status').hidden,true);
  assert.equal(h.root.querySelector('.pane-access').hidden,true);
  await h.bench.open({kind:'live',agentId:'builder',surface:'structured'});
  const first=h.bench.getActive(), firstFrame=first.root.parentNode;
  const secondId=h.bench.split(first.id,'row');
  await h.bench.open({kind:'live',agentId:'reviewer',surface:'terminal'},secondId);
  const second=h.bench.getActive(), secondFrame=second.root.parentNode;
  assert.deepEqual(h.root.querySelectorAll('.pane-title').map(node=>node.textContent),['builder','reviewer']);
  for(const [index,frame] of [firstFrame,secondFrame].entries()){
    const bar=frame.querySelector('.pane-bar'), buttons=bar.querySelectorAll('button');
    assert.equal(frame.tagName,'SECTION');
    assert.equal(buttons.length,3);
    assert.deepEqual(buttons.map(button=>button.dataset.action),['choose-agent','split','menu']);
    assert.ok(buttons.every(button=>button.type==='button'));
    assert.equal(buttons[0].className,'pane-title');
    assert.equal(buttons[0].getAttribute('aria-haspopup'),'dialog');
    assert.match(buttons[0].getAttribute('aria-label'),new RegExp(`Choose agent for pane ${index}: `));
    assert.match(buttons[0].title,new RegExp(`Pane ${index}: `));
    for(const [button,label,icon] of [[buttons[1],'Split pane','split'],[buttons[2],'Pane actions','more']]){
      assert.equal(button.getAttribute('aria-label'),label);
      assert.equal(button.getAttribute('aria-haspopup'),'menu');
      assert.equal(button.textContent,'','tools use artwork, not Unicode glyphs');
      const svg=button.querySelector('svg.pane-icon');assert.ok(svg);
      assert.equal(svg.dataset.icon,icon);assert.equal(svg.getAttribute('viewBox'),'0 0 24 24');
      assert.equal(svg.getAttribute('width'),'16');assert.equal(svg.getAttribute('height'),'16');
      assert.equal(svg.getAttribute('stroke'),'currentColor');assert.equal(svg.getAttribute('fill'),'none');
      assert.equal(svg.getAttribute('stroke-width'),'1.6');assert.equal(svg.getAttribute('stroke-linecap'),'round');
      assert.equal(svg.getAttribute('aria-hidden'),'true');assert.equal(svg.getAttribute('focusable'),'false');
    }
    assert.equal(bar.querySelectorAll('.pane-tool').length,2);
    assert.equal(bar.querySelectorAll('svg').length,2);
    assert.equal(frame.dataset.paneIndex,String(index));
    assert.equal(bar.querySelector('.pane-status').textContent,'Live');
  }
  assert.equal(firstFrame.getAttribute('aria-label'),'Pane 0: builder');
  assert.equal(firstFrame.querySelector('.pane-mode').textContent,'Chat');
  assert.equal(secondFrame.querySelector('.pane-mode').textContent,'Terminal');
  assert.equal(h.root.querySelectorAll('.pane-tools').length,0,'no crowded toolbar wrapper');
  h.bench.activate(first.id);
  const focuses=second.focuses, choices=h.choices.length;
  const chooser=secondFrame.querySelector('.pane-title');
  secondFrame.dispatchEvent({type:'pointerdown',target:chooser});
  assert.equal(second.focuses,focuses,'interactive chrome does not refocus the composer');
  await chooser.onclick();
  assert.equal(h.bench.getState().active,second.id);
  assert.equal(h.choices.length,choices+1);assert.equal(h.choices.at(-1).agentId,'reviewer');
  first.updateState({title:'<img src=x onerror=alert(1)> AgentBridge'});
  assert.equal(firstFrame.querySelector('.pane-title').textContent,'<img src=x onerror=alert(1)> AgentBridge');
  assert.equal(firstFrame.querySelectorAll('img, script').length,0,'agent titles are text, never trusted icon HTML');
  first.updateState({title:'builder'});
  const thirdId=h.bench.split(first.id,'column');
  await h.bench.open({kind:'live',agentId:'checker',surface:'structured'},thirdId);
  const thirdFrame=h.bench.getPane(thirdId).root.parentNode;
  assert.deepEqual(h.bench.listPanes().map(({id,index})=>({id,index})),[
    {id:first.id,index:0},{id:thirdId,index:1},{id:secondId,index:2},
  ]);
  assert.deepEqual(h.root.querySelectorAll('.pane-title').map(node=>node.textContent),['builder','checker','reviewer']);
  assert.deepEqual(h.root.querySelectorAll('.pane').map(frame=>frame.dataset.paneIndex),['0','1','2']);
  assert.deepEqual(JSON.parse(h.values.get('layout')).panes.map(pane=>pane.id),[first.id,thirdId,secondId]);
  h.bench.closePane(first.id);
  assert.deepEqual(h.bench.listPanes().map(pane=>pane.id),[thirdId,secondId]);
  assert.equal(thirdFrame.querySelector('.pane-title').textContent,'checker');
  assert.equal(secondFrame.querySelector('.pane-title').textContent,'reviewer');
  assert.equal(thirdFrame.getAttribute('aria-label'),'Pane 0: checker');
  assert.equal(secondFrame.getAttribute('aria-label'),'Pane 1: reviewer');
  assert.deepEqual(h.root.querySelectorAll('.pane').map(frame=>frame.dataset.paneIndex),['0','1']);
  assert.strictEqual(h.bench.getPane(secondId).root.parentNode,secondFrame);
  assert.equal(first.closes,1);assert.equal(firstFrame.isConnected,false);
  const changes=h.layoutChanges.length;
  first.updateState({title:'late title'});
  assert.equal(h.layoutChanges.length,changes,'closed pane metadata is ignored');
  h.bench.close();
});

test('header separates connection/activity from viewer, saved history and terminal keyboard access',async()=>{
  const h=mountedWorkbench();await h.bench.open({kind:'live',agentId:'builder',surface:'structured'});
  const pane=h.bench.getActive(), frame=pane.root.parentNode, input=pane.root.querySelector('[data-draft]');
  const status=frame.querySelector('.pane-status'), access=frame.querySelector('.pane-access');
  input.value='keep this draft';
  pane.updateState({statusText:'Chat ready',pending:true});
  assert.equal(status.textContent,'Working');assert.equal(status.dataset.state,'live');
  assert.equal(status.dataset.activity,'working');assert.equal(status.title,'Working · Chat ready');
  assert.equal(access.hidden,true);
  pane.updateState({permissionPending:true});
  assert.equal(status.textContent,'Permission needed');assert.equal(status.dataset.activity,'permission');
  pane.updateState({pending:false,permissionPending:false,canOperate:false,readOnly:true});
  assert.equal(status.textContent,'Live');assert.equal(access.textContent,'Viewer');
  assert.equal(access.dataset.access,'viewer');assert.match(access.title,/Read-only/);
  pane.updateState({canOperate:true});
  assert.equal(access.textContent,'Read-only');assert.equal(access.dataset.access,'readonly');
  pane.updateState({kind:'replay',status:'history',statusText:'Session history · read-only',canOperate:false});
  assert.equal(status.textContent,'History');assert.equal(access.textContent,'Read-only');
  pane.updateState({status:'replay',statusText:'Session history · read-only'});
  assert.equal(status.textContent,'History','the legacy internal state is never labelled as a player');
  pane.updateState({kind:'live',surface:'terminal',status:'live',statusText:'Terminal ready',canOperate:true});
  assert.equal(access.textContent,'Keyboard free');assert.equal(access.dataset.access,'available');
  assert.match(access.title,/Read-only until you take the keyboard/);
  pane.updateState({keyboardBusy:true,keyboardHolder:'<b>Collaborator</b>'});
  assert.equal(access.textContent,'Keyboard held');assert.equal(access.dataset.access,'busy');
  assert.match(access.title,/<b>Collaborator<\/b>/);assert.equal(access.querySelector('b'),null);
  assert.match(access.title,/Request keyboard in Pane actions/);
  pane.updateState({canOperate:false});
  assert.equal(access.textContent,'Viewer','viewer is not invited to take/request keyboard');
  pane.updateState({canOperate:true,keyboardBusy:false,keyboardPending:true});
  assert.equal(access.textContent,'Syncing');assert.equal(access.dataset.access,'pending');
  pane.updateState({keyboardPending:false,keyboardOwned:true,readOnly:false});
  assert.equal(access.textContent,'You control');assert.equal(access.dataset.access,'owned');
  pane.updateState({keyboardRequestPending:true});
  assert.equal(access.textContent,'Request waiting');assert.equal(access.dataset.access,'requested');
  assert.match(access.title,/Hand off keyboard in Pane actions/);
  pane.updateState({status:'reconnecting',statusText:'Reconnecting…',keyboardOwned:false,readOnly:true});
  assert.equal(status.textContent,'Reconnecting');assert.equal(status.dataset.activity,'idle');
  assert.equal(access.textContent,'Read-only');
  assert.equal(frame.querySelector('.pane-bar').querySelectorAll('button').length,3,'access stays passive');
  assert.strictEqual(pane.root.querySelector('[data-draft]'),input);assert.equal(input.value,'keep this draft');
  assert.equal(pane.actions.length,0);assert.equal(pane.opens.length,1);
  h.bench.close();
});

test('cycle and index selection use DFS order, wrap only for cycle, and ignore invalid inputs',()=>{
  const h=mountedWorkbench(), first=h.bench.getState().active;
  const second=h.bench.split(first,'row'), third=h.bench.split(first,'column'), fourth=h.bench.split(second,'column');
  const order=[first,third,second,fourth];
  assert.deepEqual(h.bench.listPanes().map(pane=>pane.id),order);
  assert.equal(h.bench.selectIndex(0),first);
  for(const id of [third,second,fourth,first])assert.equal(h.bench.cycle(),id);
  assert.equal(h.bench.cycle(-1),fourth);
  assert.equal(h.bench.cycle(5),first);
  assert.equal(h.bench.cycle(-6),second);
  assert.equal(h.bench.cycle(0),second);
  assert.equal(h.bench.selectIndex(3),fourth);
  for(const index of [-1,4,0.5,'0',NaN,Infinity])assert.equal(h.bench.selectIndex(index),null);
  for(const step of [0.5,'1',NaN,Infinity])assert.equal(h.bench.cycle(step),null);
  assert.equal(h.bench.activate('missing'),null);
  assert.equal(h.bench.getPane('missing'),null);
  assert.equal(h.bench.moveFocus('diagonal'),null);
  assert.equal(h.bench.getState().active,fourth);
  assert.strictEqual(h.bench.getActive(),h.bench.getPane(fourth));
  assert.ok(h.controllers.every(controller=>controller.focuses>0));
  h.bench.getPane(first).updateState({id:'forged',index:99});
  assert.equal(h.bench.listPanes()[0].id,first);assert.equal(h.bench.listPanes()[0].index,0);
  h.bench.close();
  assert.deepEqual(h.bench.listPanes(),[]);assert.equal(h.bench.getActive(),null);
  assert.equal(h.bench.selectIndex(0),null);assert.equal(h.bench.cycle(),null);assert.equal(h.bench.moveFocus('left'),null);
});

test('directional focus follows mixed layout geometry while zoomed without rearranging DOM',()=>{
  const h=mountedWorkbench(), left=h.bench.getState().active;
  const top=h.bench.split(left,'row'), bottom=h.bench.split(top,'column');
  h.bench.selectIndex(0);
  assert.equal(h.bench.moveFocus('right'),top);
  assert.equal(h.bench.moveFocus('down'),bottom);
  assert.equal(h.bench.moveFocus('left'),left);
  for(const direction of ['left','up','down'])assert.equal(h.bench.moveFocus(direction),null);
  h.bench.toggleMaximize(bottom);
  const frames=[...h.root.children];
  for(const frame of frames)frame.getBoundingClientRect=()=>{throw new Error('focus must use the tree, not visible rectangles');};
  assert.equal(h.bench.moveFocus('up'),top);
  assert.equal(h.bench.getState().maximized,top);
  assert.equal(h.bench.moveFocus('left'),left);
  assert.equal(h.bench.getState().maximized,left);
  assert.equal(h.bench.moveFocus('down'),null);
  assert.equal(h.bench.cycle(-1),bottom);
  assert.equal(h.bench.selectIndex(1),top);
  assert.equal(h.bench.getState().maximized,top);
  frames.forEach((frame,index)=>{
    assert.strictEqual(h.root.children[index],frame);
    assert.equal(frame.hidden,frame.dataset.paneId!==top);
  });
  h.bench.toggleMaximize();
  assert.equal(h.bench.getState().maximized,null);
  assert.ok(frames.every(frame=>!frame.hidden&&frame.isConnected));
  assert.ok(h.controllers.every(controller=>controller.opens.length===0&&controller.closes===0));
  h.bench.close();
});

test('metadata, focus and render notify pane tabs, but repeated output neither renders nor persists',async()=>{
  const h=mountedWorkbench();await h.bench.open({kind:'live',agentId:'builder',surface:'structured'});
  const first=h.bench.getActive(), input=first.root.querySelector('[data-draft]');input.value='draft';
  const secondId=h.bench.split(first.id,'row');
  await h.bench.open({kind:'live',agentId:'reviewer',surface:'structured'},secondId);
  const second=h.bench.getActive(), branch=h.root.firstElementChild;
  let count=h.layoutChanges.length, writes=h.writes.length;
  first.updateState({title:'renamed'});
  assert.equal(h.layoutChanges.length,++count);
  assert.equal(first.root.parentNode.querySelector('.pane-title').textContent,'renamed');
  second.updateState({status:'disconnected'});
  assert.equal(h.layoutChanges.length,++count);
  assert.equal(h.activeChanges.at(-1).status,'disconnected');
  assert.equal(h.writes.length,writes,'titles and connection state are not persistent targets');
  const activeChanges=h.activeChanges.length, snapshots=h.controllers.map(controller=>controller.snapshots);
  for(let i=0;i<100;i++){first.updateState();second.updateState();}
  assert.equal(h.layoutChanges.length,count);assert.equal(h.activeChanges.length,activeChanges);
  assert.equal(h.writes.length,writes);
  assert.deepEqual(h.controllers.map(controller=>controller.snapshots),snapshots);
  assert.strictEqual(h.root.firstElementChild,branch);assert.strictEqual(first.root.querySelector('[data-draft]'),input);
  assert.equal(input.value,'draft');
  h.bench.selectIndex(0);
  assert.equal(h.layoutChanges.length,++count);assert.equal(h.layoutChanges.at(-1).active,first.id);
  assert.equal(h.activeChanges.at(-1).title,'renamed');
  assert.equal(h.writes.length,++writes);
  h.bench.toggleMaximize(secondId);
  assert.equal(h.layoutChanges.length,++count);assert.equal(h.layoutChanges.at(-1).maximized,secondId);
  assert.equal(h.activeChanges.at(-1).title,'reviewer');
  assert.equal(JSON.parse(h.values.get('layout')).active,secondId);
  h.bench.closePane(first.id);
  assert.equal(h.layoutChanges.length,++count);assert.equal(h.layoutChanges.at(-1).count,1);
  assert.equal(second.root.parentNode.querySelector('.pane-title').textContent,'reviewer');
  h.bench.close();
});

test('overflow retains session operations; a separate two-item split menu respects the four-pane bound',async()=>{
  const h=mountedWorkbench();await h.bench.open({kind:'live',agentId:'builder',surface:'structured'});
  const first=h.bench.getActive();first.updateState({canEndSession:true});first.allowEnd=true;
  const items=paneMenu(h);
  assert.deepEqual(items.map(item=>item.label),['Choose agent','New session','Interrupt turn','Reconnect','History',
    'Maximize pane','End session for everyone…','Close pane (keep running)']);
  await items.find(item=>item.label==='Choose agent').action();assert.equal(h.choices.length,1);
  for(const label of ['New session','Interrupt turn','Reconnect','History','End session for everyone…'])items.find(item=>item.label===label).action();
  assert.deepEqual(first.actions,['newSession','interrupt','reconnect','showHistory','endSession']);
  const split=splitMenu(h,first.id);
  assert.deepEqual(split.map(item=>item.label),['Split right','Split below']);
  assert.ok(split.every(item=>!item.disabled));
  split.find(item=>item.label==='Split below').action();assert.equal(h.bench.getState().tree.axis,'column');
  paneMenu(h,first.id).find(item=>item.label==='Maximize pane').action();
  assert.equal(h.bench.getState().maximized,first.id);
  paneMenu(h).find(item=>item.label==='Restore layout').action();assert.equal(h.bench.getState().maximized,null);
  splitMenu(h).find(item=>item.label==='Split right').action();h.bench.split();
  const bounded=splitMenu(h);
  assert.ok(bounded.every(item=>item.disabled));bounded[0].action();
  split[1].action(); // A menu opened before hitting the limit must still obey it.
  assert.equal(h.bench.getState().count,4);assert.equal(h.controllers.length,4);
  paneMenu(h,first.id).find(item=>item.label==='Close pane (keep running)').action();
  assert.equal(first.closes,1);assert.equal(first.actions.filter(action=>action==='endSession').length,1);
  h.bench.close();
});

test('stale split/overflow actions cannot touch a closed pane or a replacement session',async()=>{
  const h=mountedWorkbench();await h.bench.open({kind:'live',agentId:'builder',sessionId:'old',surface:'structured'});
  const pane=h.bench.getActive();pane.updateState({canEndSession:true});pane.allowEnd=true;
  const oldSession=paneMenu(h), splits=splitMenu(h);
  await h.bench.open({kind:'live',agentId:'reviewer',sessionId:'new',surface:'terminal'});
  for(const label of ['New session','Interrupt turn','Reconnect','History','End session for everyone…']){
    await oldSession.find(item=>item.label===label).action();
  }
  assert.deepEqual(pane.actions,[],'old session menu cannot act on the new session');
  const current=paneMenu(h), choices=h.choices.length;
  h.bench.closePane(pane.id);
  const state=h.bench.getState(), controllers=h.controllers.length;
  for(const item of [...oldSession,...current,...splits])await item.action();
  assert.deepEqual(h.bench.getState(),state);assert.equal(h.controllers.length,controllers);
  assert.equal(h.choices.length,choices);assert.equal(pane.closes,1);assert.deepEqual(pane.actions,[]);
  assert.equal(h.bench.getActive().opens.length,0,'closing creates a blank view, not a hidden session');
  h.bench.close();
  for(const item of [...current,...splits])await item.action();
  assert.equal(h.bench.getState().count,0);assert.equal(h.controllers.length,controllers);
});

test('terminal menu routes keyboard take/request/release/handoff from primitive state and ignores stale actions',async()=>{
  const h=mountedWorkbench();await h.bench.open({kind:'live',agentId:'reviewer',surface:'terminal'});
  const pane=h.bench.getActive();
  let items=paneMenu(h);
  assert.equal(items.find(item=>item.label==='Take keyboard').disabled,false);
  assert.equal(items.find(item=>item.label==='Release keyboard').disabled,true);
  assert.equal(items.find(item=>item.label==='Hand off keyboard').disabled,true);
  items.find(item=>item.label==='Take keyboard').action();
  pane.updateState({keyboardBusy:true});items=paneMenu(h);
  assert.equal(items.find(item=>item.label==='Request keyboard').disabled,false);
  items.find(item=>item.label==='Request keyboard').action();
  pane.updateState({keyboardOwned:true,keyboardBusy:false});items=paneMenu(h);
  assert.equal(items.find(item=>item.label==='Take keyboard').disabled,true);
  assert.equal(items.find(item=>item.label==='Hand off keyboard').disabled,true,'no requester yet');
  pane.updateState({keyboardRequestPending:true});items=paneMenu(h);
  for(const label of ['Release keyboard','Hand off keyboard']){
    const item=items.find(item=>item.label===label);assert.equal(item.disabled,false);item.action();
  }
  assert.deepEqual(pane.actions,['requestKeyboard','requestKeyboard','releaseKeyboard','handoffKeyboard']);
  for(const state of [{canOperate:false},{canOperate:true,status:'disconnected'},{status:'live',keyboardPending:true}]){
    pane.updateState(state);
    assert.ok(paneMenu(h).filter(item=>item.label.includes('keyboard')).every(item=>item.disabled));
  }
  const stale=items;
  pane.updateState({surface:'structured'});
  assert.equal(paneMenu(h).filter(item=>item.label.includes('keyboard')).length,0);
  for(const item of stale.filter(item=>item.label.includes('keyboard')))item.action();
  assert.equal(pane.actions.length,4,'terminal menu cannot operate on a replacement chat surface');
  h.bench.closePane(pane.id);
  for(const item of stale.filter(item=>item.label.includes('keyboard')))item.action();
  assert.equal(pane.actions.length,4);assert.equal(pane.closes,1);
  h.bench.close();
});

test('real pane sessions retain sockets, terminal and chat drafts across rearrange and close without termination',async()=>{
  const browser=createBrowser(), root=browser.document.createElement('main');browser.document.body.appendChild(root);
  const Pane=browser.loadModule('pane.js'), requests=[], menus=[];
  const runtime={schema_version:2,runtime:'runtime',surfaces:[
    {id:'structured',available:true,default:true,features:{}},{id:'terminal',available:true,features:{}},
  ]};
  const bench=W.createWorkbench({root,createPane:Pane.createPane,services:{
    chooseAgent:async()=>null,getUser:()=>({id:7,username:'me'}),getWorkspace:()=>({id:'workspace',role:'operator'}),
    menu:(anchor,items)=>menus.push(items),
    findAgent:id=>({agent:{id,display_name:id,runtime:'runtime'},box:{capabilities:[runtime]}}),
    api:async(path,options={})=>{
      requests.push({path,...options});
      assert.ok(!options.method||options.method==='GET','layout must not create or delete a session');
      const match=path.match(/^\/api\/agents\/([^/]+)\/sessions$/);assert.ok(match,'only session lookup is expected');
      return [{id:'session-'+match[1],surface:match[1]==='reviewer'?'terminal':'structured',state:'live'}];
    },
  }});
  const builderId=bench.getState().active;
  await bench.open({kind:'live',agentId:'builder',sessionId:'session-builder',surface:'structured'});
  const chatSocket=browser.sockets[0];chatSocket.open();chatSocket.receive({type:'ready',surface:'structured'});
  chatSocket.receive({type:'collaboration',role:'operator'});
  const builder=bench.getPane(builderId), builderFrame=root.querySelector('.pane');
  assert.equal(builderFrame.querySelector('.pane-mode').textContent,'Chat');
  assert.equal(builderFrame.querySelector('.pane-status').textContent,'Live');
  assert.equal(builderFrame.querySelector('.pane-access').hidden,true);
  const input=builderFrame.querySelector('[data-ui="chat-input"]');input.value='unfinished draft';
  const reviewerId=bench.split(builderId,'row');
  await bench.open({kind:'live',agentId:'reviewer',sessionId:'session-reviewer',surface:'terminal'},reviewerId);
  const termSocket=browser.sockets[1];termSocket.open();termSocket.receive({type:'ready',surface:'terminal'});
  const reviewer=bench.getPane(reviewerId), reviewerFrame=root.querySelectorAll('.pane').find(frame=>frame.dataset.paneId===reviewerId);
  const access=reviewerFrame.querySelector('.pane-access');
  assert.equal(access.textContent,'Syncing');
  termSocket.receive({type:'collaboration',role:'operator',keyboard:{is_holder:false,can_request:true}});
  assert.equal(access.textContent,'Keyboard free');assert.equal(reviewer.getState().readOnly,true);
  reviewerFrame.querySelector('.pane-menu').click();
  const take=menus.at(-1).find(item=>item.label==='Take keyboard');
  assert.equal(take.disabled,false);take.action();
  assert.equal(termSocket.frames.filter(frame=>frame.type==='keyboard_acquire').length,1);
  termSocket.receive({type:'collaboration',role:'viewer',keyboard:{is_holder:false,can_request:false}});
  assert.equal(access.textContent,'Viewer');assert.equal(reviewer.getState().canOperate,false);
  take.action();
  assert.equal(termSocket.frames.filter(frame=>frame.type==='keyboard_acquire').length,1,'stale menu cannot bypass the controller permission check');
  reviewerFrame.querySelector('.pane-menu').click();
  assert.ok(menus.at(-1).filter(item=>item.label?.includes('keyboard')).every(item=>item.disabled));
  termSocket.receive({type:'collaboration',role:'operator',keyboard:{is_holder:true,holder_user_id:7}});
  assert.equal(access.textContent,'You control');assert.equal(reviewer.getState().readOnly,false);
  for(const pane of [builder,reviewer]){
    const state=pane.getState();
    for(const key of ['keyboardOwned','keyboardBusy','pending','canOperate','canEndSession','readOnly'])assert.equal(typeof state[key],'boolean',key);
    for(const key of ['keyboardHolder','statusText'])assert.equal(typeof state[key],'string',key);
  }
  const terminal=browser.terminals[0], terminalInput=terminal.input;
  const blank=bench.split(builderId,'column'), requestCount=requests.length;
  const frames=root.querySelectorAll('.pane');
  bench.selectIndex(2);bench.toggleMaximize();
  assert.equal(bench.moveFocus('left'),builderId);
  assert.equal(bench.cycle(),blank);
  assert.equal(bench.moveFocus('right'),reviewerId);
  bench.toggleMaximize();bench.closePane(blank);
  assert.strictEqual(bench.getPane(builderId),builder);assert.strictEqual(bench.getPane(reviewerId),reviewer);
  assert.strictEqual(builderFrame.querySelector('[data-ui="chat-input"]'),input);assert.equal(input.value,'unfinished draft');
  assert.strictEqual(browser.terminals[0],terminal);assert.strictEqual(terminal.input,terminalInput);
  assert.ok(frames.filter(frame=>frame.dataset.paneId!==blank).every(frame=>frame.isConnected));
  assert.deepEqual(browser.sockets,[chatSocket,termSocket]);assert.equal(browser.terminals.length,1);
  assert.equal(chatSocket.readyState,1);assert.equal(termSocket.readyState,1);assert.equal(requests.length,requestCount);
  root.querySelector('[role="separator"]').onpointerdown({button:0,preventDefault(){}});
  bench.closePane(reviewerId);
  assert.equal(termSocket.closeCount,1);assert.equal(terminal.disposed,true);assert.equal(terminal.subscriptionDisposed,true);
  assert.equal(chatSocket.readyState,1);assert.equal(input.value,'unfinished draft');
  for(const event of ['pointermove','pointerup','pointercancel'])assert.equal(browser.windowListeners.get(event).size,0);
  bench.close();bench.close();
  assert.equal(chatSocket.closeCount,1);assert.equal(termSocket.closeCount,1);
  assert.ok(browser.sockets.every(socket=>socket.frames.every(frame=>frame.type!=='terminate')));
  assert.ok(browser.observers.every(observer=>observer.disconnected));
  assert.equal(browser.timers.size,0);assert.equal(browser.windowListeners.get('resize')?.size||0,0);
  assert.equal(requests.length,requestCount,'closing is a local detach, not an API operation');
  assert.equal(root.children.length,0);
});
