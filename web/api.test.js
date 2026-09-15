const test=require('node:test');
const assert=require('node:assert/strict');
const {createApi}=require('./api.js');

test('the JSON API stays on this origin and passes cancellation through',async()=>{
  const calls=[], signal=new AbortController().signal;
  const api=createApi(async(path,options)=>{calls.push({path,options});return {ok:true,status:200,json:async()=>({ok:true})};});
  assert.deepEqual(await api('/api/workspaces'),{ok:true});
  assert.equal(calls[0].options.credentials,'same-origin');
  assert.equal(calls[0].options.headers['Content-Type'],undefined);
  await api('/api/agents/a/sessions',{method:'POST',body:'{}',signal});
  assert.equal(calls[1].options.headers['Content-Type'],'application/json');
  assert.equal(calls[1].options.signal,signal);
  await assert.rejects(api('https://other.example/api/workspaces'),/stay on this app/);
});

test('HTTP status remains available to auth handling without exposing proxy HTML',async()=>{
  const api=createApi(async()=>({ok:false,status:401,statusText:'Unauthorized',text:async()=>'<html>private proxy details</html>'}));
  await assert.rejects(api('/api/me/user'),error=>error.status===401&&!error.message.includes('<html>'));
  const empty=createApi(async()=>({ok:true,status:204}));
  assert.equal(await empty('/api/agents/a',{method:'DELETE'}),null);
  const bad=createApi(async()=>({ok:true,status:200,json:async()=>{throw new Error('unexpected HTML');}}));
  await assert.rejects(bad('/api/me/user'),/sign in again/);
});
