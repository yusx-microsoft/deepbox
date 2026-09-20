const test = require('node:test');
const assert = require('node:assert/strict');
const {webcrypto, createHash} = require('node:crypto');
const UI = require('./integrations/deeporca/agent-ui.js');
const values = changes=>({base_url:'http://localhost:11434/v1', model:'vendor/model:latest',
  context_window:'32768', reasoning_effort:'', auth_mode:'none', ...changes});

test('all native reasoning efforts and provider model routing are supported', ()=>{
  assert.deepEqual(UI.EFFORTS, ['', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']);
  for(const reasoning_effort of UI.EFFORTS){
    const config = UI.modelConfig(values({reasoning_effort, model:'provider/model@revision+variant'}));
    assert.equal(config.reasoning_effort, reasoning_effort);
    assert.equal(config.context_window, 32768);
  }
});

test('browser endpoint validation agrees with shared and native SDK validation', ()=>{
  for(const base_url of ['https://my_llm.invalid/v1', 'https://bad..invalid/v1',
    'https://-invalid/v1', 'https://host/v1/%', 'https://host/v1/{bad}',
    'https://host/v1/<bad>', 'https://éxample.invalid/v1', 'https://host:0/v1',
    'https://host/v1?', 'https://user:secret@host/v1', 'https://host/v1#']){
    assert.throws(()=>UI.modelConfig(values({base_url})), /Endpoint/);
  }
  for(const base_url of ['http://[::1]:11434/v1/', 'https://api.example.invalid/v1',
    'https://local-model/v1/%20', 'http://localhost:1234/v1']){
    assert.equal(UI.modelConfig(values({base_url})).base_url, base_url);
  }
});

test('context windows are not guessed and must be exact positive token integers', ()=>{
  for(const context_window of ['', '0', '-1', '1.2', '128,000', '1e5', '9007199254740992'])
    assert.throws(()=>UI.modelConfig(values({context_window})), /Context window/);
});

test('legacy fixed model survives settings serialization instead of changing profile identity', async()=>{
  const agent = {runtime_config:{integration_version:1,model:'legacy-model'}};
  const result = await UI.configuredPayload(values({model:'legacy-model'}), {}, agent);
  assert.equal(result.model, 'legacy-model');
  assert.equal(UI.settingsFields(agent).find(field=>field.name === 'model').value, 'legacy-model');
  await assert.rejects(UI.configuredPayload(values({model:'other-model'}), {}, agent), /fixed model override/);
});

test('no-auth needs no WebCrypto, keeping secrets never resends the stored envelope', async()=>{
  const descriptor = {agent_config:{profile_modes:['create'],configuration_templates:[{id:'connector-default'}]}};
  assert.deepEqual((await UI.configuredPayload(values(), descriptor)).credential, {mode:'none'});
  const agent = {runtime_config:{llm:UI.modelConfig(values()),credential:{mode:'sealed',ciphertext:'stored'}}};
  assert.equal((await UI.configuredPayload(values({auth_mode:'keep'}), {}, agent)).credential, undefined);
  await assert.rejects(UI.configuredPayload(values({auth_mode:'keep',base_url:'https://other.invalid/v1'}), {}, agent), /Endpoint changed/);
});

test('browser sealing is randomized, endpoint authenticated and rejects unsafe key text', async()=>{
  const pair = await webcrypto.subtle.generateKey({name:'RSA-OAEP',modulusLength:2048,
    publicExponent:new Uint8Array([1,0,1]),hash:'SHA-256'}, true, ['encrypt','decrypt']);
  const public_key = await webcrypto.subtle.exportKey('jwk', pair.publicKey);
  const spki = await webcrypto.subtle.exportKey('spki', pair.publicKey);
  const key_id = createHash('sha256').update(Buffer.from(spki)).digest('hex');
  const descriptor = {agent_config:{credential_key:{version:1,algorithm:'RSA-OAEP-256+A256GCM',key_id,public_key}}};
  const endpoint = values().base_url, secret = 'fake-browser-node-test-key';
  const a = await UI.sealCredential(secret, endpoint, descriptor, webcrypto);
  const b = await UI.sealCredential(secret, endpoint, descriptor, webcrypto);
  assert.notEqual(a.ciphertext, b.ciphertext);
  assert.ok(!JSON.stringify(a).includes(secret));
  const raw = await webcrypto.subtle.decrypt({name:'RSA-OAEP'}, pair.privateKey, Buffer.from(a.wrapped_key,'base64'));
  const aes = await webcrypto.subtle.importKey('raw', raw, {name:'AES-GCM'}, false, ['decrypt']);
  const decrypt = url=>webcrypto.subtle.decrypt({name:'AES-GCM',iv:Buffer.from(a.iv,'base64'),
    additionalData:Buffer.from('agentbridge/deeporca/credential/v1\0'+key_id+'\0'+url)}, aes, Buffer.from(a.ciphertext,'base64'));
  assert.equal(Buffer.from(await decrypt(endpoint)).toString(), secret);
  await assert.rejects(decrypt('https://other.invalid/v1'));
  for(const key of ['fake key', 'fake\nkey', 'fake\u0085key', '${ENV_KEY}', ' fake-key'])
    await assert.rejects(UI.sealCredential(key, endpoint, descriptor, webcrypto), /whitespace|control|placeholders/);
  await assert.rejects(UI.sealCredential(secret, endpoint, descriptor, {}), /encryption is unavailable/);
});

test('rename-only settings do not require configuring a legacy profile', async()=>{
  const input = {display_name:'New name',base_url:'',model:'',context_window:'',reasoning_effort:'',api_key:'',auth_mode:'keep'};
  assert.deepEqual(await UI.settingsPayload(input, {runtime_config:{}}, {}), {display_name:'New name'});
  await assert.rejects(UI.settingsPayload({...input, base_url:'https://models.invalid/v1'}, {runtime_config:{}}, {}), /Model/);
});

test('unknown status and malformed capabilities cannot echo paths or crash presentation', ()=>{
  assert.equal(UI.runtimeStatus({state:'C:/private/profile',code:'C:/private/key'}).state,'pending');
  assert.equal(UI.runtimeStatus({state:'error',code:'constructor'}).message.includes('function'),false);
  assert.deepEqual(UI.agentConfiguration({agent_config:{profile_modes:['create'],configuration_templates:[null,{id:'../private'},{id:'connector-default'}]}}).templates,
    [{value:'connector-default',label:'connector-default'}]);
});
