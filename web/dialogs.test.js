const test=require('node:test');
const assert=require('node:assert/strict');
const {createBrowser}=require('./test-dom.js');
function setup(){
  const browser=createBrowser();
  const dialogs=browser.loadModule('./dialogs.js').createDialogs(browser.document);
  return {...browser,dialogs};
}

test('replacing dialogs resolves the old promise and leaves one Escape listener',async()=>{
  const h=setup(), count=()=>h.document.body.listeners.get('keydown')?.size||0;
  const first=h.dialogs.confirm('First','old question');
  assert.equal(count(),1);
  const second=h.dialogs.form({title:'Second',fields:[{name:'name',label:'Name',value:'kept'}]});
  assert.equal(await first,null);assert.equal(count(),1);
  h.dialogs.close();assert.equal(await second,null);assert.equal(count(),0);
  assert.equal(h.document.getElementById('overlay'),null);
  h.dialogs.destroy();
});

test('forms keep validation inline, resolve once, and use scoped unique field ids',async()=>{
  const h=setup();
  const result=h.dialogs.form({title:'Create',fields:[{name:'name',label:'Name',required:true}]});
  const overlay=h.document.getElementById('overlay'),form=overlay.querySelector('form'),field=overlay.querySelector('input');
  const handler=form.onsubmit;
  handler({preventDefault(){}});
  assert.match(overlay.querySelector('[data-error]').textContent,/Name is required/);
  field.value='  hello  '; handler({preventDefault(){}}); handler({preventDefault(){}});
  assert.equal((await result).name,'hello');
  const second=h.dialogs.form({title:'Next',fields:[{name:'name',label:'Name'}]});
  assert.notEqual(h.document.getElementById('overlay').querySelector('input').id,field.id);
  h.dialogs.close();await second;h.dialogs.destroy();
});

test('picker filtering and keyboard selection never evaluate labels as HTML',async()=>{
  const h=setup();
  const picked=h.dialogs.pick({items:[
    {label:'Alpha',detail:'machine one',value:'a'},
    {label:'<script>evil</script>',detail:'machine two',value:'b'},
    {label:'Hidden',disabled:true,value:'hidden'},
  ]});
  const overlay=h.document.getElementById('overlay'), search=overlay.querySelector('[data-search]');
  assert.equal(overlay.querySelector('script'),null);
  assert.equal(overlay.querySelectorAll('[data-pick]').length,2);
  search.value='machine two';search.oninput();
  assert.equal(overlay.querySelectorAll('[data-pick]').length,1);
  search.onkeydown({key:'Enter',preventDefault(){}});
  assert.equal(await picked,'b');h.dialogs.destroy();
});

test('destroy cancels pending menu actions, timers and later notices',async()=>{
  const h=setup(), anchor=h.document.createElement('button');h.document.body.appendChild(anchor);
  let called=0;
  h.dialogs.menu(anchor,[{label:'Action',action:()=>{called++;}}]);
  h.document.querySelector('.context-menu button').click();
  h.dialogs.notice('temporary');
  h.dialogs.destroy();await Promise.resolve();await Promise.resolve();
  assert.equal(called,0);assert.equal(h.timers.size,0);
  h.dialogs.notice('too late');
  assert.equal(h.document.querySelector('.toast'),null);
  assert.equal(await h.dialogs.alert('Closed','not rendered'),null);
  assert.equal(h.document.getElementById('overlay'),null);
});
