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
assert.equal(root.querySelector('[data-field="profile_mode"]'),null);
assert.match(root.textContent,/Automatic managed profile/);
assert.match(root.textContent,/credentials stay on the Connector/);
root.querySelector('[data-field="handle"]').value='Local helper';
root.querySelector('[data-field="runtime"]').value='deeporca';
root.querySelector('[data-field="template"]').value='connector-default';
root.querySelector('form').dispatchEvent({type:'submit'});await flush();
assert.equal(calls.length,0);assert.match(root.querySelector('[data-error]').textContent,/requires a registered local project/);
const project=root.querySelector('[data-field="local_project_id"]');assert.equal(project.required,true);
const runtime=root.querySelector('[data-field="runtime"]');
runtime.value='cli';runtime.dispatchEvent({type:'change'});assert.equal(project.required,false);
assert.match(project.textContent,/No project \(runtime default\)/);
runtime.value='deeporca';runtime.dispatchEvent({type:'change'});assert.equal(project.required,true);
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
 runtime_config:{integration_version:1,profile:{mode:'create',configuration_template_ref:'connector-default'}}});
root=b.document.querySelector('.overlay');assert.match(root.textContent,/needs configuration/);
assert.match(root.textContent,/Retry initialization/);
assert.match(root.textContent,/Agent settings/);
root.querySelector('[data-refresh-status]').click();await flush();
assert.equal(calls.length,1);assert.ok(refreshes>=3);dialogs.close();await flush();
manager.agentSettings('a');await flush();root=b.document.querySelector('.overlay');
assert.match(root.textContent,/needs configuration/);assert.equal(machine.agents.length,1);
assert.equal(root.querySelectorAll('input').length,1);dialogs.close();await flush();
// Existing CLI behavior remains optional and sends null.
manager.createAgent('m');await flush();root=b.document.querySelector('.overlay');
root.querySelector('[data-field="handle"]').value='CLI helper';
root.querySelector('[data-field="runtime"]').value='cli';
root.querySelector('[data-field="runtime"]').dispatchEvent({type:'change'});
root.querySelector('form').dispatchEvent({type:'submit'});await flush();
assert.equal(calls.length,2);assert.equal(calls[1].body.local_project_id,null);
assert.deepEqual(calls[1].body.runtime_config,{});
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
for(const renderer of ['deeporca-chat-v1','https://untrusted.test/plugin.js']){
 const root=b.document.createElement('section');b.document.body.appendChild(root);
 const requests=[];
 const pane=mod.createPane({id:'offline'+requests.length,root,services:{getWorkspace:()=>({id:'w',role:'viewer'}),getUser:()=>({id:7}),findAgent:()=>null,
  api:async(path)=>{requests.push(path);return {surface:'structured',renderer,events:[],checkpoints:[]};}
 }});
 await pane.open({kind:'replay',agentId:'archived',sessionId:'stored',surface:'structured'});await flush();
 assert.equal(!!root.querySelector('.deeporca-chat'),renderer==='deeporca-chat-v1');
 assert.deepEqual(requests,['/api/sessions/stored/replay']);assert.equal(b.sockets.length,0);
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
