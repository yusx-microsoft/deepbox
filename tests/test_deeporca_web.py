"""Offline native-chat frontend contract tests using the repository's Node DOM harness.

No browser downloads, credentials, servers or network requests are needed.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="Node is required for frontend contract tests")
COMMON = r"""
const assert = require('node:assert/strict');
const {createBrowser} = require('./web/test-dom.js');
const Chat = require('./web/chat.js');
const plain = v => JSON.parse(JSON.stringify(v));
const flush = async () => { for(let i=0;i<30;i++) await Promise.resolve(); };
const descriptor = {
  runtime:'deeporca',family:'deeporca',schema_version:2,backend:'python-library',
  installation:{status:'installed'},compatibility:{status:'compatible'},
  features:{renderer:'deeporca-chat-v1',interactive_approval:false},
  agent_config:{profile_modes:['create'],configuration_templates:[{id:'connector-default',label:'Connector default'}],execution_policy:'non_interactive'},
  surfaces:[{id:'structured',available:true,default:true,features:{renderer:'deeporca-chat-v1',interactive_approval:false}}]
};
const events = [
 {ev:'session.config',renderer:'deeporca-chat-v1',config:{model:'local-reference'}},
 {ev:'user.echo',text:'Hi',client_input_id:'input-1'},
 {ev:'turn.start',turn_id:'t1'},
 {ev:'thinking.delta',text:'Inspect ',turn_id:'t1'},
 {ev:'thinking.delta',text:'locally',turn_id:'t1'},
 {ev:'message.delta',message_id:'m1',turn_id:'t1',text:'Hello '},
 {ev:'tool.call',tool_id:'x',turn_id:'t1',tool:'write_file',input:{path:'untrusted display only'}},
 {ev:'tool.result',tool_id:'x',content:'Local review required',is_error:true,code:'approval_required',status:'blocked'},
 {ev:'message.delta',message_id:'m1',turn_id:'t1',text:'world'},
 {ev:'turn.end',turn_id:'t1',status:'interrupted',subtype:'interrupted'}
];
"""


def run_js(source):
    result = subprocess.run(
        [NODE, "-e", COMMON + "\n(async()=>{\n" + source + "\n})().catch(e=>{console.error(e);process.exitCode=1;});"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_reducer_live_restore_ids_thinking_and_policy():
    run_js(r"""
const live=Chat.initialChatState();
Chat.appendUserTurn(live,'Hi',[],'input-1');
for(const event of events) Chat.applyEvent(live,event);
Chat.applyEvent(live,events[1]);
Chat.applyEvent(live,events.at(-1));
assert.equal(live.items.filter(i=>i.kind==='user').length,1);
assert.equal(live.items.filter(i=>i.kind==='turn').length,1);
assert.equal(live.items.find(i=>i.kind==='assistant').text,'Hello world');
assert.equal(live.items.find(i=>i.kind==='thinking').text,'Inspect locally');
assert.equal(live.run.state,'interrupted');
const restored=Chat.foldEventPayload(Chat.initialChatState(),events.map(JSON.stringify).join('\n'),true).state;
assert.deepEqual(live,restored);
Chat.applyEvent(live,{ev:'permission.ask',request_id:'forbidden'});
assert.equal(live.pendingPermission,null);
const cli=Chat.initialChatState();
Chat.applyEvent(cli,{ev:'permission.ask',request_id:'cli'});
assert.equal(cli.pendingPermission.request_id,'cli');
Chat.appendUserTurn(live,'Hi',[],'input-2');
Chat.applyEvent(live,{ev:'user.echo',text:'Hi',client_input_id:'input-2'});
assert.equal(live.items.filter(i=>i.kind==='user').length,2);
assert.equal(live.items.at(-1).local,false);
""")


def test_renderer_scoped_safe_markdown_copy_disclosures_and_read_only():
    run_js(r"""
const b=createBrowser({terminal:false});
const {createDeepOrcaChatView}=b.loadModule('integrations/deeporca/chat.js');
const native=require('./web/integrations/deeporca/chat.js');
for(const value of ['javascript:alert(1)','data:text/html,hi','file:///tmp/x','https://a.test/\nscript']) assert.equal(native.safeUrl(value),null);
assert.equal(native.safeUrl('https://example.test/docs'),'https://example.test/docs');
const a=b.document.createElement('div'), other=b.document.createElement('div');
const copies=[], sent=[];
const view=createDeepOrcaChatView(a,{copyText:text=>copies.push(text),sendInput:text=>sent.push(text)});
const second=createDeepOrcaChatView(other);
view.restore(events);
assert.ok(a.querySelector('.deeporca-chat'));
assert.match(a.textContent,/Blocked by local policy/);
assert.match(a.textContent,/Thinking/);
assert.match(a.textContent,/interrupted/);
assert.equal(a.querySelector('.chat-perm'),null);
assert.equal(view.sendInput('not allowed'),false);
view.setAccess({readOnly:false,canSend:true});view.sendInput('allowed');assert.deepEqual(sent,['allowed']);
view.applyEvent({ev:'message.delta',message_id:'safe',turn_id:'t2',text:'<img src=x onerror=evil()> [bad](javascript:evil) **safe**\n\n```js\n<unsafe>\n```',native:{schema:'deeporca.turn.v1',html:'<script>evil()</script>'}});
assert.equal(a.querySelector('img'),null);assert.equal(a.querySelector('script'),null);assert.equal(a.querySelector('a'),null);
assert.match(a.textContent,/<img src=x onerror=evil\(\)>/);
assert.equal(a.querySelector('strong').textContent,'safe');
await a.querySelector('.do-copy').onclick();assert.deepEqual(copies,['<unsafe>\n']);
const details=a.querySelector('.do-tool');details.open=true;details.ontoggle();
view.applyEvent({ev:'status',message:'still local'});assert.equal(a.querySelector('.do-tool').open,true);
second.restore([{ev:'user.echo',text:'Other pane',client_input_id:'o'}]);
assert.doesNotMatch(a.textContent,/Other pane/);
view.destroy();view.applyEvent(events[1]);assert.equal(a.textContent,'');assert.match(other.textContent,/Other pane/);
""")


def test_runtime_catalog_and_real_add_agent_form_payload_and_status():
    run_js(r"""
const catalog=require('./web/integrations/deeporca/agent-ui.js');
assert.equal(catalog.agentConfiguration(descriptor).canCreate,true);
assert.throws(()=>catalog.creationConfig({...descriptor,agent_config:{profile_modes:['bind']}},'connector-default'));
assert.throws(()=>catalog.creationConfig(descriptor,'C:/private/credentials.json'));
assert.equal(catalog.runtimeStatus({state:'ready'}).message.includes('not been verified'),true);
const b=createBrowser({terminal:false}), calls=[];
const workspace={id:'w',role:'owner'};
const machine={id:'m',workspace_id:'w',name:'Local Machine',capabilities:{runtimes:[descriptor,{runtime:'cli',installed:true}]},projects:[],agents:[]};
const context={user:{id:'u'},workspace,epoch:1,devboxes:[machine]};
const dialogs=b.loadModule('dialogs.js').createDialogs(b.document);
let refreshes=0;
const manager=b.loadModule('management.js').createManagement({dialogs,context:()=>context,
 refresh:async()=>{refreshes++;},api:async(path,options)=>{
   calls.push({path,body:JSON.parse(options.body)});
   const result={id:'a',...JSON.parse(options.body),runtime_status:{state:'needs_configuration',code:'template_missing'}};
   machine.agents.push(result);return result;
 }});
manager.createAgent('m');await flush();
let root=b.document.querySelector('.overlay');
assert.equal(root.querySelector('[data-field="profile_mode"]').closest('.field').hidden,true);
assert.equal(root.querySelector('[data-field="profile_mode"]').disabled,true);
assert.match(root.textContent,/Automatic managed profile/);
assert.match(root.textContent,/Encrypted in this browser/);
assert.equal(root.querySelector('[data-field="template"]'),null);
root.querySelector('[data-field="handle"]').value='Local helper';
root.querySelector('[data-field="runtime"]').value='deeporca';
root.querySelector('[data-field="base_url"]').value='http://localhost:11434/v1';
root.querySelector('[data-field="model"]').value='provider/model-name';
root.querySelector('[data-field="context_window"]').value='32768';
root.querySelector('[data-field="auth_mode"]').value='none';
root.querySelector('[data-field="auth_mode"]').dispatchEvent({type:'change'});
root.querySelector('form').dispatchEvent({type:'submit'});await flush();
assert.equal(calls.length,0);assert.match(root.querySelector('[data-error]').textContent,/requires a registered local project/);
const project=root.querySelector('[data-field="local_project_id"]');assert.equal(project.required,true);
const runtime=root.querySelector('[data-field="runtime"]');
runtime.value='cli';runtime.dispatchEvent({type:'change'});assert.equal(project.required,false);
assert.equal(root.querySelector('.do-agent-group').hidden,true);
assert.equal(root.querySelector('[data-field="model"]').disabled,true);
assert.match(project.textContent,/No project \(runtime default\)/);
runtime.value='deeporca';runtime.dispatchEvent({type:'change'});assert.equal(project.required,true);
assert.equal(root.querySelector('[data-field="model"]').value,'provider/model-name');
assert.equal(root.querySelector('.do-agent-group').hidden,false);
machine.projects=[{id:'project-local-1',name:'Registered test project'}];
root.querySelector('[data-refresh-projects]').click();await flush();
assert.equal(project.value,'');project.value='project-local-1';
root.querySelector('[data-refresh-projects]').click();await flush();assert.equal(project.value,'project-local-1');
machine.projects=[];root.querySelector('[data-refresh-projects]').click();await flush();assert.equal(project.value,'');
// A stale selected id cannot bypass the current Machine catalog.
project.value='project-local-1';root.querySelector('form').dispatchEvent({type:'submit'});await flush();
assert.equal(calls.length,0);assert.match(root.querySelector('[data-error]').textContent,/no longer available/);
machine.projects=[{id:'project-local-1',name:'Registered test project'}];
root.querySelector('[data-refresh-projects]').click();await flush();project.value='project-local-1';
root.querySelector('form').dispatchEvent({type:'submit'});await flush();
assert.equal(calls.length,1);assert.equal(calls[0].path,'/api/devboxes/m/agents');
assert.deepEqual(calls[0].body,{handle:'Local helper',display_name:'Local helper',runtime:'deeporca',local_project_id:'project-local-1',
 runtime_config:{integration_version:1,profile:{mode:'create',configuration_template_ref:'connector-default'},
 llm:{provider:'openai',base_url:'http://localhost:11434/v1',model:'provider/model-name',context_window:32768,reasoning_effort:''},credential:{mode:'none'}}});
root=b.document.querySelector('.overlay');assert.match(root.textContent,/Needs configuration/);
assert.match(root.textContent,/Retry initialization/);
assert.match(root.textContent,/Agent settings/);
root.querySelector('[data-refresh-status]').click();await flush();
assert.equal(calls.length,1);assert.ok(refreshes>=3);dialogs.close();await flush();
manager.agentSettings('a');await flush();root=b.document.querySelector('.overlay');
assert.match(root.textContent,/Needs configuration/);assert.equal(machine.agents.length,1);
assert.equal(root.querySelector('[data-field="api_key"]').value,'');
assert.equal(root.querySelector('[data-field="auth_mode"]').value,'keep');dialogs.close();await flush();
// Existing CLI behavior remains optional and sends null.
manager.createAgent('m');await flush();root=b.document.querySelector('.overlay');
root.querySelector('[data-field="handle"]').value='CLI helper';
root.querySelector('[data-field="runtime"]').value='cli';
root.querySelector('[data-field="runtime"]').dispatchEvent({type:'change'});
root.querySelector('form').dispatchEvent({type:'submit'});await flush();
assert.equal(calls.length,2);assert.equal(calls[1].body.local_project_id,null);
assert.deepEqual(calls[1].body.runtime_config,{});
""")


def test_switch_to_cli_does_not_submit_runtime_fields_or_key_draft():
    run_js(r"""
const b=createBrowser({terminal:false}),calls=[];
const box={id:'m',name:'Fixture',workspace_id:'w',capabilities:{runtimes:[descriptor,{runtime:'cli',installed:true}]},
 projects:[{id:'p',name:'Project'}],agents:[]};
const context={user:{id:'u'},workspace:{id:'w',role:'owner'},epoch:1,devboxes:[box]};
const dialogs=b.loadModule('dialogs.js').createDialogs(b.document);
const manager=b.loadModule('management.js').createManagement({dialogs,context:()=>context,refresh:async()=>{},api:async(path,options)=>{
 calls.push({path,body:JSON.parse(options.body)});return {id:'cli-agent',runtime:'cli'};
}});
manager.createAgent('m');await flush();
const root=b.document.querySelector('.overlay'),field=name=>root.querySelector(`[data-field="${name}"]`);
field('handle').value='CLI Agent';field('base_url').value='https://fixture.test/v1';field('model').value='provider/model';
field('api_key').value='cli-must-never-receive-this-fixture-key';
field('runtime').value='cli';field('runtime').dispatchEvent({type:'change'});
assert.equal(root.querySelector('.do-agent-group').hidden,true);
assert.equal(root.querySelector('.modal').classList.contains('do-agent-modal'),false);
root.querySelector('form').dispatchEvent({type:'submit'});await flush();
assert.deepEqual(calls,[{path:'/api/devboxes/m/agents',body:{handle:'CLI Agent',display_name:'CLI Agent',runtime:'cli',local_project_id:null,runtime_config:{}}}]);
assert.ok(!JSON.stringify(calls).includes('fixture-key'));
""")


def test_model_configuration_validation_and_sealed_credentials():
    run_js(r"""
const catalog=require('./web/integrations/deeporca/agent-ui.js');
const crypto=require('node:crypto');
const values={base_url:'http://localhost:11434/v1',model:'provider/model-name:latest',context_window:'32768',reasoning_effort:'',auth_mode:'none'};
for(const [name,badValues] of Object.entries({base_url:['','file:///private','https://key:secret@test/v1','https://test/v1?api_key=x','https://test/v1#x','https://test/v1\\bad','https://test:0/v1'],model:['','not a model','${secret}'],context_window:['','0','-1','1.5','1e5','9007199254740992'],reasoning_effort:['automatic']})){
  for(const bad of badValues) assert.throws(()=>catalog.modelConfig({...values,[name]:bad}));
}
for(const effort of ['', 'none','minimal','low','medium','high','xhigh','max']){
  assert.equal(catalog.modelConfig({...values,reasoning_effort:effort}).reasoning_effort,effort);
}
assert.equal(catalog.modelConfig({...values,context_window:'9007199254740991'}).context_window,Number.MAX_SAFE_INTEGER);
const none=await catalog.configuredPayload(values,descriptor);
assert.deepEqual(none.credential,{mode:'none'});
const secret='fixture-only-key/round-trip';
await assert.rejects(catalog.configuredPayload({...values,auth_mode:'api_key',api_key:secret},descriptor),/HTTPS/);
const pair=crypto.generateKeyPairSync('rsa',{modulusLength:2048});
const key_id=crypto.createHash('sha256').update(pair.publicKey.export({format:'der',type:'spki'})).digest('hex');
const key={version:1,algorithm:'RSA-OAEP-256+A256GCM',key_id,public_key:pair.publicKey.export({format:'jwk'})};
const advertised={...descriptor,agent_config:{...descriptor.agent_config,credential_key:key}};
await assert.rejects(catalog.sealCredential(' invalid fixture key ',values.base_url,advertised),/whitespace/);
const encrypted=await catalog.configuredPayload({...values,auth_mode:'api_key',api_key:secret},advertised);
const envelope=encrypted.credential;
assert.deepEqual(Object.keys(envelope).sort(),['mode','key_id','wrapped_key','iv','ciphertext'].sort());
assert.equal(envelope.mode,'sealed');assert.equal(envelope.key_id,key_id);
assert.ok(!JSON.stringify(encrypted).includes(secret));
for(const field of ['wrapped_key','iv','ciphertext'])assert.match(envelope[field],/^[A-Za-z0-9+/]+={0,2}$/);
const raw=crypto.privateDecrypt({key:pair.privateKey,padding:crypto.constants.RSA_PKCS1_OAEP_PADDING,oaepHash:'sha256'},Buffer.from(envelope.wrapped_key,'base64'));
assert.equal(raw.length,32);assert.equal(Buffer.from(envelope.iv,'base64').length,12);
const data=Buffer.from(envelope.ciphertext,'base64');
function decrypt(url){
 const decipher=crypto.createDecipheriv('aes-256-gcm',raw,Buffer.from(envelope.iv,'base64'));
 decipher.setAAD(Buffer.from('agentbridge/deeporca/credential/v1\0'+key_id+'\0'+url));
 decipher.setAuthTag(data.subarray(-16));
 return Buffer.concat([decipher.update(data.subarray(0,-16)),decipher.final()]).toString('utf8');
}
assert.equal(decrypt(values.base_url),secret);assert.throws(()=>decrypt('https://other.test/v1'));
const again=await catalog.sealCredential(secret,values.base_url,advertised);
assert.notEqual(again.iv,envelope.iv);assert.notEqual(again.wrapped_key,envelope.wrapped_key);
await assert.rejects(catalog.sealCredential(secret,values.base_url,advertised,{}),/HTTPS/);
await assert.rejects(catalog.sealCredential(secret,values.base_url,{...advertised,agent_config:{credential_key:{...key,key_id:'0'.repeat(64)}}}),/HTTPS/);
await assert.rejects(catalog.sealCredential('',values.base_url,advertised),/API key is required/);
await assert.rejects(catalog.sealCredential('x'.repeat(4097),values.base_url,advertised),/4,096/);
const agent={runtime:'deeporca',runtime_config:{...encrypted,credential:{...envelope,untrusted_plaintext:secret}}};
const keep=await catalog.settingsPayload({...values,auth_mode:'keep',display_name:'Renamed'},agent,descriptor);
assert.equal(keep.runtime_config.credential,undefined);assert.ok(!JSON.stringify(keep).includes(secret));
assert.deepEqual(keep.runtime_config.profile,encrypted.profile);
await assert.rejects(catalog.settingsPayload({...values,auth_mode:'keep',base_url:'https://other.test/v1'},agent,descriptor),/Endpoint changed/);
const replaced=await catalog.settingsPayload({...values,auth_mode:'api_key',api_key:secret,display_name:'Renamed'},agent,advertised);
assert.equal(replaced.runtime_config.credential.mode,'sealed');assert.ok(!JSON.stringify(replaced).includes(secret));
""")


def test_edit_model_settings_validation_drafts_refresh_retry_and_legacy():
    run_js(r"""
const b=createBrowser({terminal:false}),calls=[];
const agent={id:'edit',runtime:'deeporca',handle:'orca',display_name:'Orca',local_project_id:'p',
 runtime_config:{integration_version:1,profile:{mode:'create',configuration_template_ref:'connector-default'}},
 runtime_status:{state:'needs_configuration',code:'model_not_configured'}};
const box={id:'m',workspace_id:'w',capabilities:{runtimes:[descriptor]},projects:[{id:'p',name:'Fixture project'}],agents:[agent]};
const context={user:{id:'u'},workspace:{id:'w',role:'owner'},epoch:1,devboxes:[box]};
const dialogs=b.loadModule('dialogs.js').createDialogs(b.document);
let fail=true;
const manager=b.loadModule('management.js').createManagement({dialogs,context:()=>context,refresh:async()=>{},api:async(path,options)=>{
 const body=options.body?JSON.parse(options.body):null;calls.push({path,body,method:options.method});
 if(options.method==='PATCH'){
  if(fail)throw new Error('End active conversations before changing model settings.');
  Object.assign(agent,body);return agent;
 }
 return agent;
}});
manager.agentSettings('edit');await flush();
const root=b.document.querySelector('.overlay'),field=name=>root.querySelector(`[data-field="${name}"]`);
const submit=async()=>{root.querySelector('form').dispatchEvent({type:'submit'});await flush();};
assert.equal(field('base_url').value,'');assert.equal(field('model').value,'');assert.equal(field('context_window').value,'');
assert.equal(field('api_key').getAttribute('autocomplete'),'new-password');
field('context_window').value='32768'; // An edited model field requires full setup, unlike rename-only.
await submit();assert.match(root.querySelector('[data-error]').textContent,/Endpoint is required/);assert.equal(calls.length,0);
field('base_url').value='http://localhost:11434/v1';field('base_url').dispatchEvent({type:'input'});
assert.match(root.textContent,/Endpoint changed/);
field('model').value='provider/model';field('context_window').value='32768';
await submit();assert.match(root.querySelector('[data-error]').textContent,/Endpoint changed/);assert.equal(calls.length,0);
field('auth_mode').value='api_key';field('auth_mode').dispatchEvent({type:'change'});field('api_key').value='in-memory-only-fixture';
await submit();assert.equal(calls.length,0);assert.match(root.querySelector('[data-error]').textContent,/HTTPS/);
assert.equal(field('api_key').value,'in-memory-only-fixture');assert.equal(field('api_key').disabled,false);
root.querySelector('[data-refresh-status]').click();await flush();assert.equal(field('api_key').value,'in-memory-only-fixture');
root.querySelector('[data-retry-runtime]').click();await flush();assert.deepEqual(calls[0],{path:'/api/agents/edit/runtime/retry',body:null,method:'POST'});
field('auth_mode').value='none';field('auth_mode').dispatchEvent({type:'change'});
await submit();assert.match(root.querySelector('[data-error]').textContent,/End active conversations/);
assert.equal(field('model').value,'provider/model');assert.equal(field('api_key').disabled,true);
assert.ok(!JSON.stringify(calls).includes('in-memory-only-fixture'));
fail=false;await submit();assert.equal(field('api_key').value,'');assert.equal(field('auth_mode').value,'keep');
assert.equal(root.querySelector('[data-submit]').textContent,'Saved');
assert.deepEqual(calls.at(-1).body,{display_name:'Orca',runtime_config:{integration_version:1,profile:{mode:'create',configuration_template_ref:'connector-default'},
 llm:{provider:'openai',base_url:'http://localhost:11434/v1',model:'provider/model',context_window:32768,reasoning_effort:''},credential:{mode:'none'}}});
field('reasoning_effort').value='high';await submit();assert.equal(calls.at(-1).body.runtime_config.credential,undefined);
assert.equal(calls.at(-1).body.runtime_config.llm.reasoning_effort,'high');
dialogs.close();await flush();
""")


def test_pane_reuses_transport_blocks_permissions_and_keeps_panes_isolated():
    run_js(r"""
const b=createBrowser({terminal:false}), mod=b.loadModule('pane.js'), workspace={id:'w',role:'operator'}, requests=[];
function make(id){
 const root=b.document.createElement('section');b.document.body.appendChild(root);
 const pane=mod.createPane({id,root,services:{
   getUser:()=>({id:7}),getWorkspace:()=>workspace,
   findAgent:()=>({agent:{runtime:'deeporca',renderer:'deeporca-chat-v1'},box:{capabilities:[descriptor]}}),
   api:async(path,options={})=>{requests.push(path);return options.method==='POST'?{id:id+'-session',surface:'structured',state:'live',renderer:'deeporca-chat-v1'}:[];}
 }});return {pane,root};
}
const one=make('one'),two=make('two');
async function attach(v){await v.pane.open({kind:'live',agentId:v.pane.id||'a',surface:'structured'});const ws=b.sockets.at(-1);ws.open();ws.receive({type:'ready',surface:'structured'});ws.receive({type:'collaboration',role:'operator',keyboard:{is_holder:false}});await flush();return ws;}
const ws=await attach(one), ws2=await attach(two);
assert.ok(one.root.querySelector('.deeporca-chat'));
const input=one.root.querySelector('[data-ui="chat-input"]');input.value='Hi';input.dispatchEvent({type:'keydown',key:'Enter',preventDefault(){}});
const packet=ws.frames.find(f=>f.type==='input');assert.ok(packet.client_input_id);assert.equal(packet.data,'Hi');
ws.receive({type:'event',data:JSON.stringify({ev:'user.echo',text:'Hi',client_input_id:packet.client_input_id})});
ws.receive({type:'event',data:events.slice(2).map(JSON.stringify).join('\n')});
ws.receive({type:'event',data:JSON.stringify({ev:'permission.ask',request_id:'blocked'})});await flush();
assert.equal(one.root.querySelectorAll('.do-user').length,1);
assert.equal(one.root.querySelector('.chat-perm'),null);
assert.doesNotMatch(two.root.textContent,/Hello world/);
ws.receive({type:'event',data:JSON.stringify({ev:'turn.start',turn_id:'t2'})});
one.root.querySelector('[data-ui="chat-interrupt"]').click();assert.ok(ws.frames.some(f=>f.type==='interrupt'));
assert.ok(!ws.frames.some(f=>f.type==='permission'));
workspace.role='viewer';one.pane.refreshAccess();input.value='no';input.dispatchEvent({type:'keydown',key:'Enter',preventDefault(){}});
assert.equal(ws.frames.filter(f=>f.type==='input').length,1);assert.equal(input.disabled,true);
one.pane.close();two.pane.close();assert.equal(one.root.querySelector('.deeporca-chat'),null);
assert.ok(requests.every(path=>path.startsWith('/api/agents/')));
""")


def test_archived_renderer_metadata_and_unknown_renderer_fallback():
    run_js(r"""
const b=createBrowser({terminal:false}), mod=b.loadModule('pane.js');
for(const renderer of ['deeporca-chat-v1','https://untrusted.test/plugin.js']) for(const source of ['metadata','replay','replay-session']){
 const root=b.document.createElement('section');b.document.body.appendChild(root);
 const requests=[];
 const session={id:'stored',agent_id:'archived',state:'inactive',surface:'structured',title:'Archived conversation',
  ...(source==='metadata'?{renderer}:{})};
 const recording={surface:'structured',events:[],checkpoints:[],
  ...(source==='replay'?{renderer}:source==='replay-session'?{session:{...session,renderer}}:{})};
 const pane=mod.createPane({id:'offline-'+renderer+'-'+source,root,services:{getWorkspace:()=>({id:'w',role:'viewer'}),getUser:()=>({id:7}),findAgent:()=>null,
  api:async(path,options={})=>{
   requests.push(path);assert.equal(options.method||'GET','GET');
   if(path==='/api/sessions/stored')return session;
   assert.equal(path,'/api/sessions/stored/replay');return recording;
  }
 }});
 await pane.open({kind:'replay',agentId:'archived',sessionId:'stored',surface:'structured'});await flush();
 assert.equal(pane.getState().status,'replay');assert.equal(pane.getState().readOnly,true);
 assert.equal(!!root.querySelector('.deeporca-chat'),renderer==='deeporca-chat-v1');
 // Current history fetches identity/action metadata before the retained recording.
 assert.deepEqual(requests,['/api/sessions/stored','/api/sessions/stored/replay']);assert.equal(b.sockets.length,0);
 assert.equal(root.querySelector('[data-ui="session-title"]').textContent,'Archived conversation');
 assert.ok(root.querySelector('[data-ui="replay-controls"]'));
 assert.equal(root.querySelector('[data-ui="chat-renderer-notice"]').hidden,renderer==='deeporca-chat-v1');
 if(renderer!=='deeporca-chat-v1')assert.match(root.textContent,/Unsupported conversation renderer/);
 assert.equal(root.querySelector('[data-ui="chat-composer"]').hidden,true);
 pane.close();
}
await assert.rejects(Chat.loadLocalModule('https://untrusted.test/plugin.js'),/Unsupported renderer/);
await assert.rejects(Chat.loadLocalModule('__proto__'),/Unsupported renderer/);
assert.equal(Chat.rendererId({renderer:'unknown'},descriptor),'unknown');
assert.equal(Chat.rendererId({},descriptor),'deeporca-chat-v1');
""")


def test_offline_browser_scrolling_safe_links_and_lazy_allowlist():
    sync_api = pytest.importorskip('playwright.sync_api')
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except sync_api.Error as exc:
            pytest.skip(f'Installed Chromium unavailable: {exc}')
        try:
            page = browser.new_page()
            requested = []
            page.on('request', lambda request: requested.append(request.url))
            page.route('**/*', lambda route: route.abort())
            page.set_content('<div id="a" style="height:220px;overflow:auto"></div><div id="b"></div>')
            for name in ['chat.js', 'integrations/deeporca/chat.js']:
                page.add_script_tag(path=str(ROOT / 'web' / name))
            page.add_style_tag(path=str(ROOT / 'web/integrations/deeporca/chat.css'))
            result = page.evaluate(r"""async () => {
                const chat = await AgentBridgeChat.loadLocalModule('deeporca-chat-v1');
                const container = document.querySelector('#a');
                const view = chat.createDeepOrcaChatView(container);
                const second = chat.createDeepOrcaChatView(document.querySelector('#b'));
                const events = [{ev:'session.config',renderer:'deeporca-chat-v1',config:{}},
                  {ev:'thinking.delta',turn_id:'t',text:'private local thought'},
                  {ev:'tool.call',tool_id:'tool',tool:'inspect',input:{path:'display only'}},
                  {ev:'tool.result',tool_id:'tool',content:'blocked',is_error:true,status:'blocked'}];
                view.restore(events);
                const card = container.querySelector('.do-tool'); card.open = true;
                await new Promise(resolve => setTimeout(resolve, 5));
                container.scrollTop = container.scrollHeight;
                for(let i=0;i<12;i++) view.applyEvent({ev:'user.echo',client_input_id:String(i),text:'Message '+i});
                const following = container.scrollHeight-container.scrollTop-container.clientHeight < 3;
                container.scrollTop = 0;
                view.applyEvent({ev:'message.delta',message_id:'m',turn_id:'t',text:'<img src=x onerror=evil()> [safe](https://example.test/docs) [bad](javascript:evil)'});
                second.restore([{ev:'user.echo',text:'Second pane only'}]);
                let unknownBlocked = false;
                try { await AgentBridgeChat.loadLocalModule('__proto__'); } catch (_) { unknownBlocked = true; }
                return {following,scrollTop:container.scrollTop,open:container.querySelector('.do-tool').open,
                  links:[...container.querySelectorAll('a')].map(a=>({href:a.href,rel:a.rel})),
                  imageCount:container.querySelectorAll('img').length,unknownBlocked,
                  isolated:!container.textContent.includes('Second pane only')};
            }""")
            assert result == {
                'following': True, 'scrollTop': 0, 'open': True,
                'links': [{'href': 'https://example.test/docs', 'rel': 'noopener noreferrer'}],
                'imageCount': 0, 'unknownBlocked': True, 'isolated': True,
            }
            assert requested == []
        finally:
            browser.close()
