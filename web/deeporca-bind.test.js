'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const AgentUI = require('./integrations/deeporca/agent-ui.js');
const {createBrowser} = require('./test-dom.js');
const nativeRef = 'native-' + 'a'.repeat(32);
const secondRef = 'native-' + 'b'.repeat(32);
const descriptor = {runtime:'delegated-orca',installed:true,scope:'project',default_project_mode:'required',
  execution:{environment:'managed',permission_mode:'runtime_managed',configuration_ui:'deeporca_managed_profile'},
  agent_config:{profile_modes:['create','bind'],existing_profiles:[{id:nativeRef,label:'Personal native profile'}]}};
const values = {base_url:'http://localhost:11434/v1',model:'local-model',auth_mode:'none',api_key:'',context_window:'32768',reasoning_effort:''};
const flush = async()=>{for(let i=0;i<20;i++) await Promise.resolve();};

test('profile controls default to create; binding requires explicit capability and opaque inventory', () => {
  for (const item of [undefined,{}, {agent_config:{}}, {agent_config:{existing_profiles:[{id:nativeRef,label:'Local'}]}},
    {agent_config:{profile_modes:['create']}}, {agent_config:{profile_modes:'bind'}}]) {
    const config = AgentUI.agentConfiguration(item), fields = AgentUI.creationFields(config);
    assert.equal(config.canBind,false);
    assert.equal(fields[0].value,'create');
    assert.deepEqual(fields[0].options.map(item=>item.value),['create']);
    assert.equal(fields.find(item=>item.name === 'native_stopped').type,'checkbox');
  }
  const config = AgentUI.agentConfiguration({...descriptor,agent_config:{...descriptor.agent_config,
    existing_profiles:[...descriptor.agent_config.existing_profiles,{id:'/private/home',label:'Invalid'},{id:'native-'+'A'.repeat(32),label:'Invalid'}]}});
  const fields = AgentUI.creationFields(config);
  assert.equal(config.canBind,true);
  assert.deepEqual(config.profiles,[{value:nativeRef,label:'Personal native profile'}]);
  assert.deepEqual(fields[0].options.map(item=>item.label),['Create a new profile','Bind an existing profile']);
  assert.equal(fields[1].value,''); // Never select the first profile implicitly.
  assert.equal(fields[2].value,'');
  assert.equal(fields.find(item=>item.name === 'api_key').value,'');
  assert.ok(fields.every(item=>item.type !== 'hidden'));
});

test('binding serializes only consent and advertised ref without inspecting model or secret drafts', async () => {
  const draft = {profile_mode:'bind',profile_ref:nativeRef,native_stopped:true};
  for (const key of Object.keys(values)) Object.defineProperty(draft,key,{get(){throw new Error('Managed draft read: '+key);}});
  assert.deepEqual(await AgentUI.configuredPayload(draft,descriptor),{
    integration_version:1,profile:{mode:'bind',profile_ref:nativeRef,native_stopped:true},
  });
  const created = await AgentUI.configuredPayload({...values,profile_mode:'create',profile_ref:nativeRef,native_stopped:true},descriptor);
  assert.equal(created.profile.mode,'create'); assert.equal(created.profile.profile_ref,undefined);
  assert.equal(created.credential.mode,'none'); assert.equal(created.llm.context_window,32768);
});

test('binding requires real boolean consent, exact current inventory ref and Connector support', async () => {
  const draft = {profile_mode:'bind',profile_ref:nativeRef,native_stopped:true};
  for (const native_stopped of [undefined,false,'true','on',1])
    await assert.rejects(AgentUI.configuredPayload({...draft,native_stopped},descriptor),/Confirm that you have stopped/);
  for (const profile_ref of ['',undefined,'Personal native profile','C:/private/home','native-'+'A'.repeat(32),secondRef])
    await assert.rejects(AgentUI.configuredPayload({...draft,profile_ref},descriptor),/Select an available native profile/);
  for (const item of [undefined,{}, {agent_config:{profile_modes:['create'],existing_profiles:[{id:nativeRef,label:'Old SDK'}]}}])
    await assert.rejects(AgentUI.configuredPayload(draft,item),/does not support binding/);
  await assert.rejects(AgentUI.configuredPayload({...draft,profile_mode:'free-form'},descriptor),/supported profile mode/);
});

test('native Agent settings are immutable and rename-only even if mutable values are supplied', async () => {
  const agent = {runtime:'delegated-orca',display_name:'Before',runtime_config:{integration_version:1,
    profile:{mode:'bind',profile_ref:nativeRef,native_stopped:true}}};
  assert.deepEqual(AgentUI.settingsFields(agent),[]);
  assert.deepEqual(await AgentUI.settingsPayload({display_name:'After',...values,api_key:'never-forward-this'},agent,descriptor),{display_name:'After'});
  await assert.rejects(AgentUI.configuredPayload(values,descriptor,agent),/read-only/);
  assert.match(AgentUI.runtimeStatus({state:'ready'},true).message,/Keep native DeepOrca stopped/);
  assert.match(AgentUI.runtimeStatus({state:'error',code:'configuration_busy',message:'C:/private/profile'},true).message,/Stop native DeepOrca/);
  assert.match(AgentUI.runtimeStatus({state:'error',code:'existing_profile_unavailable'},true).message,/Restore it on the Connector/);
  assert.match(AgentUI.runtimeStatus({state:'error',code:'existing_profile_api_unavailable'},true).message,/Update its DeepOrca SDK/);
  assert.doesNotMatch(AgentUI.runtimeStatus({state:'error',code:'existing_profile_unavailable',message:'C:/private/profile'},true).message,/C:\/private/);
});

function setup() {
  const browser = createBrowser({terminal:false}), calls = [];
  const chat = browser.loadModule('chat.js'), contract = chat.runtimeContract({runtime:'deeporca'});
  chat.runtimeContract = agent=>agent?.runtime === 'delegated-orca' ? contract : {};
  const capability = JSON.parse(JSON.stringify(descriptor));
  const box = {id:'m',name:'Local Connector',workspace_id:'w',online:true,capabilities:{runtimes:[capability,{runtime:'cli',installed:true}]},
    projects:[{id:'p',name:'Registered project'}],agents:[]};
  const context = {user:{id:'u'},workspace:{id:'w',role:'owner'},epoch:1,devboxes:[box]};
  const dialogs = browser.loadModule('dialogs.js').createDialogs(browser.document);
  const state = {fail:false};
  const management = browser.loadModule('management.js').createManagement({dialogs,document:browser.document,context:()=>context,refresh:async()=>{},
    api:async(path,options)=>{
      const body = options.body ? JSON.parse(options.body) : undefined; calls.push({path,method:options.method,body});
      if(state.fail) throw new Error('Fixture Connector unavailable; retry');
      if(options.method === 'PATCH') return Object.assign(box.agents[0],body);
      if(path.endsWith('/retry')) return {state:'pending'};
      const agent = {id:'a',...body,runtime_status:{state:'ready'}}; box.agents.push(agent); return agent;
    }});
  return {browser,box,capability,dialogs,calls,management,state};
}

test('real delegated creation preserves drafts, has boolean consent, excludes hidden managed fields and retries', async t => {
  const {browser,box,capability,dialogs,calls,management,state} = setup();
  t.after(()=>dialogs.close());
  management.createAgent('m'); await flush();
  let root = browser.document.querySelector('.overlay');
  const field = name=>root.querySelector(`[data-field="${name}"]`);
  const change = (name,value)=>{field(name).value=value;field(name).dispatchEvent({type:'change'});};
  const submit = async()=>{root.querySelector('form').dispatchEvent({type:'submit'});await flush();};
  assert.equal(field('runtime').value,'delegated-orca');
  assert.equal(field('profile_mode').value,'create'); assert.equal(field('profile_ref').value,'');
  assert.match(root.textContent,/minimal security: broad tools, no approvals/);
  assert.match(root.textContent,/trusted Connector template may override/);
  field('handle').value='Bound helper'; field('local_project_id').value='p';
  for(const [key,value] of Object.entries(values)) field(key).value=value;
  field('api_key').value='draft-secret-never-send';
  change('profile_mode','bind');
  for(const name of Object.keys(values)) {assert.equal(field(name).disabled,true);assert.equal(field(name).closest('.field').hidden,true);}
  assert.equal(field('native_stopped').checked || false,false);
  assert.equal(field('native_stopped').required,true);
  assert.match(root.textContent,/Old native chats are not imported/);
  assert.equal(root.querySelector('input[type="hidden"]'),null);
  await submit(); assert.equal(calls.length,0); assert.match(root.querySelector('[data-error]').textContent,/Select an available/);
  change('profile_ref',nativeRef); await submit(); assert.equal(calls.length,0); assert.match(root.querySelector('[data-error]').textContent,/Confirm that/);
  field('native_stopped').checked=true;
  change('profile_mode','create'); assert.equal(field('api_key').value,'draft-secret-never-send');
  assert.equal(field('model').value,'local-model'); assert.equal(field('model').disabled,false);
  change('profile_mode','bind'); assert.equal(field('profile_ref').value,nativeRef); assert.equal(field('native_stopped').checked,true);
  change('runtime','cli'); assert.equal(field('profile_mode').closest('.field').hidden,true); assert.equal(field('native_stopped').disabled,true);
  assert.equal(root.querySelector('.do-native-profile').hidden,true);
  change('runtime','delegated-orca'); assert.equal(field('profile_ref').value,nativeRef);
  // Removed inventory never silently chooses the replacement or switches to create.
  capability.agent_config.existing_profiles=[{id:secondRef,label:'Another profile'}];
  root.querySelector('[data-refresh-projects]').click(); await flush();
  assert.equal(field('profile_ref').value,nativeRef);
  await submit(); assert.equal(calls.length,0); assert.match(root.querySelector('[data-error]').textContent,/Select an available/);
  capability.agent_config.profile_modes=['create']; root.querySelector('[data-refresh-projects]').click(); await flush();
  assert.equal(field('profile_mode').value,'bind'); assert.equal(field('profile_mode').closest('.field').hidden,true);
  await submit(); assert.equal(calls.length,0); assert.match(root.querySelector('[data-error]').textContent,/does not support/);
  capability.agent_config.profile_modes=['create','bind']; capability.agent_config.existing_profiles=descriptor.agent_config.existing_profiles;
  root.querySelector('[data-refresh-projects]').click(); await flush();
  state.fail=true; await submit(); assert.equal(calls.length,1);
  assert.equal(field('profile_ref').value,nativeRef); assert.equal(field('native_stopped').checked,true); assert.equal(field('api_key').value,'draft-secret-never-send');
  state.fail=false; await submit(); assert.equal(calls.length,2);
  assert.deepEqual(calls[1].body,{handle:'Bound helper',display_name:'Bound helper',runtime:'delegated-orca',local_project_id:'p',
    runtime_config:{integration_version:1,profile:{mode:'bind',profile_ref:nativeRef,native_stopped:true}}});
  assert.doesNotMatch(JSON.stringify(calls),/draft-secret|credential|llm|context_window/);
  assert.equal(box.agents.length,1);
  dialogs.close(); await flush(); management.agentSettings('a'); await flush(); root=browser.document.querySelector('.overlay');
  assert.equal(root.querySelectorAll('[data-field]').length,1);
  for(const name of Object.keys(values)) assert.equal(field(name),null);
  assert.match(root.textContent,/Personal native profile/); assert.match(root.textContent,new RegExp(nativeRef));
  assert.match(root.textContent,/Only the Agent name/); assert.match(root.textContent,/Keep native DeepOrca stopped/);
  field('display_name').value='Renamed native helper'; await submit();
  assert.deepEqual(calls.at(-1).body,{display_name:'Renamed native helper'});
});

test('bound settings safely explain missing and busy profiles, retry has no configuration body', async t => {
  const {browser,box,capability,dialogs,calls,management} = setup(); t.after(()=>dialogs.close());
  box.agents=[{id:'a',runtime:'delegated-orca',local_project_id:'p',handle:'native',display_name:'Native',
    runtime_config:{integration_version:1,profile:{mode:'bind',profile_ref:nativeRef,native_stopped:true}},
    runtime_status:{state:'error',code:'configuration_busy',message:'C:/private/profile'}}];
  capability.agent_config.existing_profiles=[];
  management.agentSettings('a');await flush();
  const root=browser.document.querySelector('.overlay');
  assert.match(root.textContent,/Profile unavailable in current inventory/);
  assert.match(root.textContent,/Stop native DeepOrca/); assert.doesNotMatch(root.textContent,/C:\/private|API key/);
  root.querySelector('[data-refresh-status]').click();await flush();assert.equal(calls.length,0);
  root.querySelector('[data-retry-runtime]').click();await flush();
  assert.equal(calls[0].path,'/api/agents/a/runtime/retry'); assert.equal(calls[0].body,undefined);
});

test('old Connector hides binding controls; empty supported inventory shows safe help without an implicit choice', async t => {
  const {browser,capability,dialogs,management,calls} = setup(); t.after(()=>dialogs.close());
  delete capability.agent_config;
  management.createAgent('m'); await flush();
  const root=browser.document.querySelector('.overlay');
  const field=name=>root.querySelector(`[data-field="${name}"]`);
  assert.equal(field('profile_mode').closest('.field').hidden,true);
  assert.equal(field('profile_mode').disabled,true);
  assert.equal(root.querySelector('.do-native-profile').hidden,true);
  assert.equal(field('profile_ref').disabled,true); assert.equal(field('native_stopped').disabled,true);
  assert.equal(field('model').disabled,false);
  capability.agent_config={profile_modes:['create','bind'],existing_profiles:[]};
  root.querySelector('[data-refresh-projects]').click(); await flush();
  assert.equal(field('profile_mode').closest('.field').hidden,false);
  field('profile_mode').value='bind';field('profile_mode').dispatchEvent({type:'change'});
  assert.equal(field('profile_ref').value,''); assert.equal(field('profile_ref').querySelectorAll('option').length,1);
  assert.match(root.querySelector('[data-native-inventory]').textContent,/No native profiles are available/);
  assert.match(root.querySelector('[data-native-inventory]').textContent,/Paths cannot be entered/);
  field('handle').value='missing';field('local_project_id').value='p';field('native_stopped').checked=true;
  root.querySelector('form').dispatchEvent({type:'submit'});await flush();assert.equal(calls.length,0);
  assert.match(root.querySelector('[data-error]').textContent,/Select an available native profile/);
});
