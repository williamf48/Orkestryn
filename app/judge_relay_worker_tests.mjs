// Actual Worker handler and SQL against Node SQLite. Upstream is always a mock.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import worker, {handle, VERSION, MODEL, PROMPTS} from './judge_relay_worker.mjs';

const now = Date.parse('2026-10-28T18:00:00Z');
const password = 'test-password-used-only-in-offline-fixtures';
const providerKey = 'nebius-fixture-secret-not-a-real-key';
const digest = async s => Buffer.from(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s))).toString('hex');
function database() {
  const db = new DatabaseSync(':memory:');
  db.exec(readFileSync(new URL('./judge_relay_schema.sql',import.meta.url),'utf8'));
  return {raw:db, prepare(sql) { return {bind(...args) { return {
    async first() { return db.prepare(sql).get(...args) || null; },
    async run() { return {meta:{changes:Number(db.prepare(sql).run(...args).changes)}}; },
  }; }}; }};
}
function environment(extra={}) {return {DB:database(), RELAY_ENABLED:'1', RELAY_USERNAME:'judge',
  RELAY_PASSWORD:password, NEBIUS_API_KEY:providerKey, MAX_ATTEMPTS:'1200', ACCESS_UNTIL:'2026-12-17T00:00:00Z', ...extra};}
async function input(role='factory', workflow='offline-fixture') {return {schema_version:VERSION,role,workflow,
  request_id:await digest(workflow+':'+role), review_digest:'1'.repeat(64), prompt_digest:await digest(PROMPTS[role]),
  expires_ms:now+3600000, context:JSON.stringify({fixture:'Mock only; no model call.'})};}
function request(value, headers={}, method='POST', path='/v1/proposal') {return new Request('https://evaluation.example.com'+path,
  {method,headers:{Authorization:'Basic '+btoa('judge:'+password),'Content-Type':'application/json',...headers},
   ...(method==='GET'?{}:{body:typeof value==='string'?value:JSON.stringify(value)})});}
function upstream(captured, content='{"mock":true}') {return async (url, options) => {
  captured.push({url,options});return Response.json({model:MODEL,id:'offline-response',
    choices:[{finish_reason:'stop',message:{content}}],usage:{prompt_tokens:10,completion_tokens:20,total_tokens:30}});
};}
function rows(env) {return env.DB.raw.prepare('SELECT * FROM relay_requests').all();}

test('health is public, unconfigured by default and makes no inference request',async()=>{
  let calls=0;const response=await handle(new Request('https://evaluation.example.com/health'),{},()=>calls++,now);
  assert.equal(response.status,200);assert.equal((await response.json()).configured,false);assert.equal(calls,0);
});
test('Cloudflare fetch export does not treat the execution context as a network function',async()=>{
  const response=await worker.fetch(new Request('https://evaluation.example.com/health'),{}, {waitUntil(){}});
  assert.equal(response.status,200);
});
test('configuration closed, expired or missing secret denies before a claim',async t=>{
  for (const changes of [{RELAY_ENABLED:'0'},{NEBIUS_API_KEY:''},{RELAY_PASSWORD:'short'},
      {MAX_ATTEMPTS:'NaN'},{MAX_ATTEMPTS:'2'},{ACCESS_UNTIL:'2026-10-01T00:00:00Z'}]) {
    await t.test(Object.keys(changes)[0],async()=>{const env=environment(changes),calls=[];
      const r=await handle(request(await input()),env,upstream(calls),now);assert.equal(r.status,503);
      assert.equal(calls.length,0);assert.equal(rows(env).length,0);});
  }
});
test('authentication, browser-origin and content-type denials make zero provider calls',async t=>{
  for (const [headers,status] of [[{Authorization:''},401],[{Authorization:'Basic broken'},401],
      [{Origin:'https://other.example.com'},403],[{'Content-Type':'text/plain'},415]]) {
    await t.test(JSON.stringify(headers),async()=>{const env=environment(),calls=[];
      const r=await handle(request(await input(),headers),env,upstream(calls),now);
      assert.equal(r.status,status);assert.equal(calls.length,0);assert.equal(rows(env).length,0);});
  }
});
test('unknown routes and non-POST methods cannot call upstream',async()=>{
  const env=environment(),calls=[];
  assert.equal((await handle(request(null,{},'GET'),env,upstream(calls),now)).status,405);
  assert.equal((await handle(request({}, {},'POST','/chat/completions'),env,upstream(calls),now)).status,404);
  assert.equal(calls.length,0);
});
test('injected model, tools, system prompt and output-limit fields are rejected',async t=>{
  for (const field of ['model','tools','system','max_tokens','endpoint']) await t.test(field,async()=>{
    const env=environment(),calls=[],value={...await input(),[field]:'not-permitted'};
    assert.equal((await handle(request(value),env,upstream(calls),now)).status,400);
    assert.equal(calls.length,0);assert.equal(rows(env).length,0);
  });
});
test('invalid binding, role, prompt, expiry and context are rejected',async t=>{
  for (const [field,bad] of [['request_id','0'.repeat(64)],['role','arbitrary-chat'],['prompt_digest','0'.repeat(64)],
      ['review_digest','bad'],['expires_ms',now-1],['expires_ms',now+7200000],['context','[]']]) await t.test(field,async()=>{
    const env=environment(),calls=[];
    assert.equal((await handle(request({...await input(),[field]:bad}),env,upstream(calls),now)).status,400);
    assert.equal(calls.length,0);assert.equal(rows(env).length,0);
  });
});
test('streaming input size is bounded before any claim or provider fetch',async()=>{
  const env=environment(),calls=[];
  assert.equal((await handle(request('x'.repeat(131073)),env,upstream(calls),now)).status,413);
  assert.equal(calls.length,0);assert.equal(rows(env).length,0);
});
test('a started durable claim exists before the one fixed Nebius request',async()=>{
  const env=environment(),calls=[];
  const response=await handle(request(await input()),env,async(url,options)=>{
    assert.equal(rows(env)[0].status,'STARTED');return upstream(calls)(url,options);
  },now);
  assert.equal(response.status,200);assert.equal(calls.length,1);assert.equal(rows(env)[0].status,'RECEIVED');
  const {url,options}=calls[0],body=JSON.parse(options.body);
  assert.equal(url,'https://api.tokenfactory.nebius.com/v1/chat/completions');assert.equal(options.redirect,'error');
  assert.equal(options.headers.Authorization,'Bearer '+providerKey);assert.equal(body.model,MODEL);
  assert.equal(body.max_tokens,8192);assert.deepEqual(body.chat_template_kwargs,{enable_thinking:true});
  assert.equal(body.messages[0].content,PROMPTS.factory);assert.equal(body.tools,undefined);
  const text=await response.text();assert.ok(!text.includes(providerKey));assert.ok(!text.includes(password));
});
test('three roles are separate and cannot be called out of order',async()=>{
  const env=environment(),calls=[];
  assert.equal((await handle(request(await input('governance')),env,upstream(calls),now)).status,409);
  assert.equal((await handle(request(await input('recovery')),env,upstream(calls),now)).status,409);
  for (const role of ['factory','governance','recovery']) {
    assert.equal((await handle(request(await input(role)),env,upstream(calls),now)).status,200);
    assert.equal(calls.at(-1).options.body.includes(PROMPTS[role].slice(0,24)),true);
  }
  assert.equal(calls.length,3);assert.equal(rows(env).length,3);
});
test('an exact repeated request returns its stored receipt without another charge',async()=>{
  const env=environment(),calls=[],value=await input();
  const first=await handle(request(value),env,upstream(calls),now);
  const second=await handle(request(value),env,upstream(calls),now);
  assert.deepEqual(await first.json(),await second.json());assert.equal(calls.length,1);
});
test('reusing an attempt with a changed review/context is rejected without new inference',async()=>{
  const env=environment(),calls=[],value=await input();await handle(request(value),env,upstream(calls),now);
  const r=await handle(request({...value,context:'{"changed":true}'}),env,upstream(calls),now);
  assert.equal(r.status,409);assert.equal(calls.length,1);
});
test('concurrent duplicate requests reserve only one provider attempt',async()=>{
  const env=environment(),calls=[],value=await input();
  const results=await Promise.all([1,2,3].map(()=>handle(request(value),env,upstream(calls),now)));
  assert.equal(calls.length,1);assert.equal(rows(env).length,1);
  assert.ok(results.some(r=>r.status===200));assert.ok(results.every(r=>[200,409].includes(r.status)));
});
test('atomic global budget cannot be exceeded by concurrent new workflows',async()=>{
  const env=environment({MAX_ATTEMPTS:'3'}),calls=[];
  const inputs=await Promise.all([0,1,2,3,4].map(n=>input('factory','workflow-'+n)));
  const results=await Promise.all(inputs.map(v=>handle(request(v),env,upstream(calls),now)));
  assert.equal(calls.length,3);assert.equal(rows(env).length,3);assert.equal(results.filter(r=>r.status===409).length,2);
});
test('transport loss retains an unconfirmed attempt and cannot trigger a retry',async()=>{
  const env=environment(),value=await input();let calls=0;
  const fail=async()=>{calls++;throw new Error(providerKey+' '+password);};
  const first=await handle(request(value),env,fail,now);assert.equal(first.status,503);
  assert.equal(rows(env)[0].status,'UNCONFIRMED');assert.ok(!(await first.text()).includes(providerKey));
  assert.equal((await handle(request(value),env,fail,now)).status,409);assert.equal(calls,1);
});
test('provider errors, malformed and oversized replies retain their spent slots',async t=>{
  for (const [label,reply] of [['HTTP',()=>new Response(providerKey,{status:401})],
      ['JSON',()=>new Response('bad-json')],['shape',()=>Response.json({})],
      ['size',()=>new Response('x'.repeat(262145))]]) await t.test(label,async()=>{
    const env=environment();let calls=0;const value=await input();
    const fetcher=async()=>{calls++;return reply();};
    const response=await handle(request(value),env,fetcher,now);
    assert.ok(response.status>=400);assert.equal(rows(env)[0].status,'UNCONFIRMED');
    assert.equal((await handle(request(value),env,fetcher,now)).status,409);assert.equal(calls,1);
  });
});
test('upstream secret echoes are removed before receipt persistence and response',async()=>{
  for (const secret of [providerKey,'long-provider-key-'+'x'.repeat(240)]) {
    const env=environment({NEBIUS_API_KEY:secret});
    const response=await handle(request(await input()),env,async()=>Response.json({
      model:secret,id:secret,choices:[{finish_reason:secret,message:{content:secret+' '+password}}]}),now);
    const text=await response.text();assert.ok(text.includes('[REDACTED]'));
    assert.ok(!text.includes(secret.slice(0,20)));assert.ok(!text.includes(password));
    assert.ok(!rows(env)[0].result_json.includes(secret.slice(0,20)));
  }
});
test('a finished receipt does not invent missing token usage',async()=>{
  const env=environment();const r=await handle(request(await input()),env,async()=>Response.json({
    choices:[{finish_reason:'length',message:{content:'unfinished'}}]}),now);
  const value=await r.json();assert.equal(value.metadata.total_tokens,null);assert.equal(value.metadata.finish_reason,'length');
});
test('governance cannot extend the initial workflow expiry',async()=>{
  const env=environment(),calls=[];await handle(request(await input()),env,upstream(calls),now);
  const value=await input('governance');value.expires_ms-=1000;
  assert.equal((await handle(request(value),env,upstream(calls),now)).status,409);assert.equal(calls.length,1);
});
