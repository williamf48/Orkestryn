#!/usr/bin/env python3
"""Optional UI behaviour checks using Node.js; no browser, SDK or provider calls.

Run: python operations_ui_checks.py --output ui_checks.json
The main Windows installer runs the Python acceptance suite independently.
"""
from pathlib import Path
import argparse
import os
import shutil
import subprocess
import tempfile

SCRIPT = r"""const fs=require('fs'),vm=require('vm'),assert=require('assert'),crypto=require('crypto');
const path=require('path'),app=process.env.AGENT_REHEARSAL_UI_ROOT||(fs.existsSync(path.join(__dirname,'operations_ui.py'))?__dirname:path.join(__dirname,'../agent-rehearsal'));
const summary=JSON.parse(fs.readFileSync(path.join(app,'OPERATIONS_DEMO.json'),'utf8')).summary;
const fixture={csrf:'test-csrf',principal:{name:'Test manager',roles:['admin','manager','owner_C','deployer','cost','traffic','consumer','viewer']},users:[],runs:[{id:summary.mission_id,summary}],supervision:{},inference_enabled:false};
const nodes=new Map(),listeners={},posts=[],gets=[],checks=[];
let now=Date.now(),rule=null,created=0;
class ClockDate extends Date{static now(){return now;}}
function node(s){if(!nodes.has(s))nodes.set(s,{id:s.startsWith('#')?s.slice(1):'',dataset:{},innerHTML:'',textContent:'',value:'',disabled:false,checked:false,style:{},showModal(){this.open=true;},close(){this.open=false;}});return nodes.get(s);}
const reply=(value,status=200)=>({ok:status<400,json:async()=>structuredClone(value)});
const ctx={fixture,Date:ClockDate,console,structuredClone,crypto:{randomUUID:()=>crypto.randomUUID()},document:{querySelector:node,addEventListener:(type,fn)=>(listeners[type]??=[]).push(fn)},setTimeout:()=>1,clearTimeout(){},fetch:async(path,opt)=>{
 const body=opt?.body?JSON.parse(opt.body):null;if(body)posts.push({path,body});else gets.push(path);
 if(rule){const value=await rule(path,body);if(value)return value;}
 if(path==='/api/state')return reply(fixture);
 if(path==='/api/review')return reply({digest:'exact-review',phrase:'exact-phrase',proposal:{scope:'<untrusted>'},scope:'Only exact arguments'});
 if(path==='/api/infer')return reply({status:'SUCCEEDED',profile:'stable-v1'});
 if(path==='/api/action'&&body.action==='create'){
  const summary=structuredClone(fixture.runs[0].summary),id='new-workflow-'+(++created);
  Object.assign(summary,{mission_id:id,phase:'AWAITING_BATCH_APPROVAL',lifecycle:'LEASED',expires_unix:now/1000+(body.duration_minutes??60)*60,ttl:(body.duration_minutes??60)*60,lease_elapsed:false,historical_operations:false});
  fixture.runs.unshift({id,summary});return reply({run:id,phase:summary.phase});
 }
 return reply({status:'RECORDED'});
}};
vm.createContext(ctx);
const originalSource=fs.readFileSync(path.join(app,'operations_ui.py'),'utf8').split('<script>')[1].split('</script>')[0];
const source=originalSource.replace(/const BOOTSTRAP=[\s\S]*$/,'const BOOTSTRAP="";');
vm.runInContext(source,ctx);const run=s=>vm.runInContext(s,ctx);
async function click(b){b.closest=()=>b;b.dataset??={};for(const fn of listeners.click||[])await fn({target:b});}
function ok(name,fn){fn();checks.push(name);}
function paint(){run('paint(fixture)');}
function active(phase='AWAITING_BATCH_APPROVAL'){
 const s=fixture.runs[0].summary;Object.assign(s,{phase,lifecycle:'LEASED',expires_unix:now/1000+900,lease_elapsed:false,historical_operations:false});
 run('current=fixture.runs[0].id');paint();return s;
}
(async()=>{
 paint();ok('Stopped evidence remains readable without implicit requests',()=>{assert(node('#runbody').innerHTML.includes('View outcome report'));assert(!node('#runbody').innerHTML.includes('data-review="batch"'));assert(!node('#runbody').innerHTML.includes('id="infer"'));assert.equal(posts.length,0);});
 const old=run('current');await click(node('#create'));node('#profile').value='balanced';node('#customworkflow').value='';
 await click(node('#confirm'));ok('Unchecked creation consent sends no operation',()=>assert.equal(posts.length,0));
 node('#consent').checked=true;await click(node('#confirm'));
 ok('Creation automatically selects the returned new workflow',()=>{assert.notEqual(run('current'),old);assert.equal(run('current'),'new-workflow-1');assert(node('#runbody').innerHTML.includes('Review batch'));});
 ok('Default one-hour run appears as a fixed countdown',()=>{assert.equal(posts[0].body.duration_minutes,60);assert.equal(run('timeLeft(fixture.runs[0].summary)'),'60:00');assert(node('#runbody').innerHTML.includes('Run time remaining'));assert.equal(fixture.runs[0].summary.expires_unix,now/1000+3600);});
 await click({dataset:{review:'batch'}});
 ok('Review scope is escaped and unchecked before consent',()=>{assert(node('#review').open);assert(node('#reviewbody').innerHTML.includes('&lt;untrusted&gt;'));assert.equal(node('#consent').checked,false);});
 const before=posts.length;await click(node('#confirm'));ok('Unchecked batch consent cannot execute',()=>assert.equal(posts.length,before));
 node('#consent').checked=true;await click(node('#confirm'));
 ok('Approval carries the exact review and selected run',()=>{const b=posts.at(-1).body;assert.equal(b.digest,'exact-review');assert.equal(b.run,'new-workflow-1');assert.equal(b.action,'approve-review');});
 let s=active('AWAITING_OWNER_REVIEW');s.service.actual_at_outcome.candidate_percent=20;paint();
 ok('Active handoff keeps manager and owner approvals separate',()=>{const h=node('#runbody').innerHTML;assert(h.includes('Manager handoff review'));assert(h.includes('Customer owner handoff review'));assert(h.includes('customer job stays protected'));});
 await click({dataset:{review:'failover-manager'}});assert(node('#review').open);
 const expiry=s.expires_unix;now=expiry*1000+1;paint();
 ok('Expired open review closes without an approval or renewal',()=>{assert(!node('#review').open);assert.equal(run('pending'),null);assert.equal(s.expires_unix,expiry);assert.equal(run('timeLeft(fixture.runs[0].summary)'),'0:00');});
 ok('Expired state removes effects but keeps evidence and teardown',()=>{const h=node('#runbody').innerHTML;assert(h.includes('This run has expired'));for(const x of ['data-review=','data-job=','data-action="advance"','data-action="supervise"','id="infer"'])assert(!h.includes(x),x);assert(h.includes('View outcome report'));assert(h.includes('data-action="stop"'));assert(h.includes('data-action="stop-supervisor"'));assert(node('#app').innerHTML.includes('Create a workflow'));});
 s=active('AWAITING_BATCH_APPROVAL');s.lease_elapsed=true;paint();ok('Server expiry remains authoritative even before the client clock limit',()=>assert(!node('#runbody').innerHTML.includes('data-review="batch"')));
 s.lease_elapsed=false;s.historical_operations=true;paint();ok('Historical source cannot expose new effects',()=>{assert(!node('#runbody').innerHTML.includes('data-review="batch"'));assert(node('#runbody').innerHTML.includes('earlier version'));});
 s=active('MONITORING');ok('Current next step is distinct from earlier notifications',()=>{const h=node('#runbody').innerHTML;assert(h.includes('Use Review development fault'));assert(h.includes('<summary>Earlier notifications</summary>'));});
 rule=async(path)=>path==='/api/review'?reply({code:'MISSION_EXPIRED_OR_STOPPED',error:'No new effect after lease expiry or teardown.'},410):null;
 await click({dataset:{review:'fault-candidate'}});
 ok('HTTP expiry reason is displayed instead of a generic fetch error',()=>{assert(node('#notice').innerHTML.includes('lease expiry'));assert(!node('#notice').innerHTML.includes('Failed to fetch'));assert(!node('#review').open);});
 rule=async(path)=>path==='/api/review'?reply({code:'BATCH_STATE_CHANGED',error:'State changed <script>bad</script>. Open a fresh review.'},409):null;
 await click({dataset:{review:'fault-candidate'}});
 ok('Policy conflict reason is escaped and shown without resubmitting',()=>{assert(node('#notice').innerHTML.includes('&lt;script&gt;'));assert(!node('#notice').innerHTML.includes('<script>'));});
 rule=null;s=active();await click({dataset:{review:'batch'}});node('#consent').checked=true;
 let dropped=0;rule=async(path,body)=>{if(path==='/api/action'&&body.action==='approve-review'){dropped++;throw Error('MODELLED disconnected reply');}return null;};
 await click(node('#confirm'));await run('refresh()');await run('refresh()');
 ok('Lost approval reply is unconfirmed and never automatically repeated',()=>{assert.equal(dropped,1);assert(node('#notice').innerHTML.includes('result is unconfirmed'));assert.equal(run('pending'),null);assert(!node('#review').open);});
 const postCount=posts.length;await click(node('#confirm'));
 ok('An uncertain approval needs a new exact review before another submission',()=>assert.equal(posts.length,postCount));
 rule=null;active('MONITORING');node('#ticket').value='Urgent route outage';for(const fn of listeners.input||[])await fn({target:node('#ticket')});
 let taskDrops=0;rule=async(path)=>{if(path==='/api/infer'&&taskDrops++===0)throw Error('MODELLED lost inference reply');return null;};
 await click(node('#infer'));const id=posts.at(-1).body.id;await run('refresh()');const inferenceCount=posts.filter(p=>p.path==='/api/infer').length;await click(node('#infer'));
 ok('Manual inference reconciliation retains its original request ID',()=>{assert.equal(inferenceCount,1);assert.equal(posts.at(-1).body.id,id);});
 rule=null;fixture.principal.roles=['viewer'];paint();ok('Viewer retains reports without account or effect controls',()=>{assert(!node('#app').innerHTML.includes('Manage users'));assert(!node('#app').innerHTML.includes('Create a workflow'));assert(!node('#runbody').innerHTML.includes('data-action="advance"'));assert(!node('#runbody').innerHTML.includes('id="infer"'));assert(node('#runbody').innerHTML.includes('View outcome report'));});
 // Independent fixtures exercise evidence semantics, not CSS or element counts.
 ctx.sample={basis:{state:{stable_units:40,deployments:{'candidate-v2':{allocated_units:20,dependencies:[{id:'kept-job',owner_run:'route-C'}]}},reservations:{old:{units:9,status:'RELEASED'},pending:{units:5,status:'PENDING'}},capacity_limit:70,routing:{'candidate-v2':20}}},choice:{actions:[{intent_id:'reserve-one',actor:'A',run_id:'deploy-A',tool:'reserve_capacity',args:{units:1,budget_version:2}}],accepted:['reserve-one'],waiting:[{id:'too-large',code:'CAPACITY_EXCEEDED',reason:'Needs <script>more</script> capacity',next_step:'Obtain a fresh approval.',blocking_facts:{requested_units:20,available_headroom:5}}],final_state:{stable_units:40,deployments:{'candidate-v2':{allocated_units:20}},reservations:{next:{units:6,status:'PENDING'}},capacity_limit:70,routing:{'candidate-v2':20}}},comparison:{algorithm:'bounded',orders_compared:2,global_optimum_proven:false}};
 ctx.reviewData={proposal:ctx.sample,digest:'immutable-digest',scope:'Only <exact> selected arguments'};
 const rawSample=JSON.stringify(ctx.sample),beforePreviews=posts.length;
 const preview=run("reviewBody('batch',reviewData)");
 ok('Capacity preview counts stable, allocated and only pending reservations',()=>{assert.equal(run('capacityOf(sample.basis.state)'),65);assert(preview.includes('65 units'));assert(preview.includes('66 units'));});
 ok('Forecast and exact parameters are visible without granting waiting requests',()=>{for(const text of ['not a completed effect','does not approve the waiting requests','budget_version','immutable-digest','Full approval record'])assert(preview.includes(text),text);});
 ok('Waiting explanations and full scope escape untrusted content',()=>{assert(preview.includes('&lt;script&gt;more&lt;/script&gt;'));assert(preview.includes('&lt;exact&gt;'));assert(!preview.includes('<script>'));});
 ok('Opening previews neither mutates proposals nor sends effects',()=>{assert.equal(JSON.stringify(ctx.sample),rawSample);assert.equal(posts.length,beforePreviews);});
 ok('Absent policy measures are unknown rather than zero',()=>{const h=run("reviewBody('batch',{proposal:{choice:{actions:[],waiting:[]}},scope:'missing',digest:'empty'})");assert(h.includes('Not recorded'));assert(!h.includes('0 units'));assert(!h.includes('0%'));});
 ok('Boolean and incomplete capacity records are not treated as numbers',()=>{assert.equal(run("capacityOf({stable_units:true,deployments:{},reservations:{}})"),null);assert.equal(run("capacityOf({stable_units:40,deployments:{x:{}},reservations:{}})"),null);});
 ok('Confirmed request outcomes require both completion and a successful result',()=>{assert.equal(run("requestState({id:'x',status:'COMPLETED',result:{status:'SUCCEEDED'}},sample).label"),'Completed');assert.equal(run("requestState({id:'x',status:'COMPLETED'},sample).label"),'Needs reconciliation');});
 ok('Selected requests remain visibly incomplete',()=>assert.equal(run("requestState({id:'reserve-one',status:'SUBMITTED'},sample).label"),'Selected · not completed'));
 ok('Ambiguous effect receipts override a waiting-plan label',()=>assert.equal(run("requestState({id:'too-large',status:'SUBMITTED',result:{status:'OUTCOME_UNKNOWN'}},sample).label"),'Needs reconciliation'));
 ok('A waiting request retains its actual policy reason',()=>{assert.equal(run("requestState({id:'too-large',status:'SUBMITTED'},sample).label"),'Waiting');assert(preview.includes('Capacity exceeded'));assert(preview.includes('Obtain a fresh approval.'));});
 ctx.intentsForTable={intents:[{id:'too-large',actor:'A',tool:'reserve_capacity',status:'SUBMITTED'},{id:'reserve-one',actor:'A',tool:'reserve_capacity',status:'COMPLETED',result:{status:'SUCCEEDED'}}]};
 ok('Request table uses proposed execution order rather than submission order',()=>{const h=run('requestTable(intentsForTable,sample)');assert(h.indexOf('<b>reserve-one</b>')<h.indexOf('<b>too-large</b>'));assert(h.includes('Completed'));assert(h.includes('Waiting'));});
 ctx.handoffData={proposal:{state:ctx.sample.basis.state,action:{actor:'C',run_id:'route-C',tool:'set_route',args:{candidate_percent:0,route_version:2}}},digest:'same-plan',scope:'Preserve exact customer work.'};
 const managerView=run("reviewBody('failover-manager',handoffData)"),ownerView=run("reviewBody('failover-owner_C',handoffData)");
 ok('Separate handoff roles share exact scope while exposing distinct approval roles',()=>{assert(managerView.includes('Manager approval'));assert(ownerView.includes('Customer owner approval'));for(const h of [managerView,ownerView]){assert(h.includes('Both role approvals'));assert(h.includes('Fresh fallback checks'));assert(h.includes('65 units'));assert(h.includes('0%'));assert(h.includes('same-plan'));}});
 ctx.jobData={proposal:{dependency_id:'finished-job',state:ctx.sample.basis.state},digest:'job-plan',scope:'Release only the exact job and eligible cleanup.'};
 ok('Job release preserves other dependencies without inventing freed capacity',()=>{const h=run("reviewBody('job',jobData)");assert(h.includes('finished-job'));assert(h.includes('kept-job'));assert(h.includes('Capacity freed is not forecast'));assert(h.includes('deployment is not resumed'));});
 ctx.recoveryData={proposal:{incident:{id:'incident-x'},decision:{decision:'recover',reason:'Preserve dependencies.'}},digest:'incident-digest',scope:'Bounded cleanup only.'};
 ok('Recovery does not promise automatic traffic handoff or renewed deployment',()=>{const h=run("reviewBody('recovery',recoveryData)");assert(h.includes('require their own reviews'));assert(h.includes('not predicted here'));assert(h.includes('does not resume'));assert(h.includes('incident-x'));});
 ok('Development fault warns of local interruption without claiming cloud failure',()=>{const h=run("reviewBody('fault-candidate',{proposal:{},digest:'fault',scope:'local'})");assert(h.includes('deliberately stops'));assert(h.includes('interrupt local candidate requests'));assert(h.includes('does not simulate an actual cloud outage'));});
 ctx.eventsForTimeline={events:[{sequence:1,kind:'FAILOVER_CONSENT_RECORDED',at:'2026-10-25T10:00:00+00:00',data:{principal:{name:'<owner>'},role:'owner_C'}}]};
 ok('An approval event never appears as a confirmed handoff in the timeline',()=>{const h=run('activityTimeline(eventsForTimeline)');assert(h.includes('Handoff approval recorded'));assert(!h.includes('Traffic handoff confirmed'));assert(h.includes('&lt;owner&gt;'));assert(h.includes('Customer owner'));});
 ctx.eventsForTimeline.events.push({sequence:2,kind:'EMERGENCY_HANDOFF_CONFIRMED',at:'2026-10-25T10:01:00+00:00',data:{}});
 ok('Timeline shows confirmed effects only after their actual milestone record',()=>{const h=run('activityTimeline(eventsForTimeline)');assert(h.includes('Traffic handoff confirmed'));assert(h.indexOf('Event 2')<h.indexOf('Event 1'));});
 ctx.eventsForTimeline={events:[{sequence:1,kind:'MISSION_TEARDOWN_RECORDED',at:'invalid date',data:{all_workers_stopped:false}}]};
 ok('Incomplete shutdown and invalid timestamps remain explicitly unknown',()=>{const h=run('activityTimeline(eventsForTimeline)');assert(h.includes('Shutdown result unconfirmed'));assert(h.includes('Time not recorded'));assert(!h.includes('Invalid Date'));assert(!h.includes('All owned workers confirmed stopped'));});
 ok('Pure render helpers add zero automatic HTTP or model requests',()=>assert.equal(posts.length,beforePreviews));
 active('AWAITING_BATCH_APPROVAL');await click({dataset:{review:'batch'}});
 await listeners.cancel[0]({target:{id:'review'}});
 ok('Native dialog cancellation clears pending approval authority',()=>assert.equal(run('pending'),null));
 const sFocus=active('MONITORING');fixture.principal.roles=['consumer'];
 node('#ticket').value='Draft must survive';for(const fn of listeners.input||[])await fn({target:node('#ticket')});
 let focused=0,selection=null;ctx.document.activeElement={id:'ticket',selectionStart:6,selectionEnd:10};node('#ticket').focus=()=>focused++;node('#ticket').setSelectionRange=(a,b)=>selection=[a,b];paint();
 ok('Polling preserves ticket text, keyboard focus and selection without resubmission',()=>{assert.equal(node('#ticket').value,'Draft must survive');assert.equal(focused,1);assert.deepEqual(selection,[6,10]);});
 ctx.document.activeElement=undefined;


 ctx.document.activeElement=undefined;fixture.principal.roles=['manager'];fixture.live_proposals_enabled=true;fixture.proposal_jobs={};
 let liveState=active('DRAFT');liveState.live=true;liveState.calls=[];liveState.model_calls=0;liveState.settings={max_tokens:2048};liveState.usage={reported_tokens:0,missing_usage_calls:0};paint();
 ok('Live draft exposes only the next explicitly paid planning review',()=>{const h=node('#runbody').innerHTML;assert(h.includes('data-review="proposal-factory"'));assert(!h.includes('data-review="proposal-governance"'));assert(!h.includes('data-action="advance"'));assert(h.includes('2048 maximum output tokens'));});
 await click(node('#create'));ok('Configured NVIDIA mode is selected and scripted rehearsal remains available',()=>{const h=node('#reviewbody').innerHTML;assert(h.includes('NVIDIA Nemotron through Nebius'));assert(h.includes('value="live" selected'));assert(h.includes('value="scripted"'));assert.equal(node('#proposal_mode').value,'live');assert(h.includes('Judges do not enter an API key'));});await click(node('#cancel'));
 ctx.paidReview={proposal:{role:'factory',model:'NVIDIA test model',endpoint:'https://example.invalid',attempt_number:1,maximum_attempts:3,settings:{max_tokens:2048}},scope:'One <paid> request only',digest:'paid-digest'};
 ok('Paid proposal preview states scope, model, response cap and no chained requests',()=>{const h=run("reviewBody('proposal-factory',paidReview)");for(const text of ['one paid','NVIDIA test model','2048','No automatic retries','&lt;paid&gt;'])assert(h.includes(text),text);});
 rule=async(path)=>path==='/api/review'?reply(ctx.paidReview):null;
 await click({dataset:{review:'proposal-factory'}});const paidPosts=posts.length;await click(node('#confirm'));
 ok('Paid proposal cannot be submitted without separate checkbox consent',()=>{assert.equal(posts.length,paidPosts);assert.equal(node('#confirm').textContent,'Approve one paid attempt');});
 node('#consent').checked=true;await click(node('#confirm'));
 ok('Paid proposal approval binds its exact run, role and digest',()=>{const b=posts.findLast(p=>p.path==='/api/action').body;assert.equal(b.kind,'proposal-factory');assert.equal(b.digest,'paid-digest');assert.equal(b.run,run('current'));});
 rule=null;fixture.proposal_jobs[run('current')]={role:'factory',status:'RUNNING'};liveState.phase='FACTORY_REQUESTING';paint();
 const beforePoll=posts.length;await run('refresh()');await run('refresh()');
 ok('Background proposal polling cannot repeat paid requests or reconcile in-flight work',()=>{assert.equal(posts.length,beforePoll);const h=node('#runbody').innerHTML;assert(!h.includes('data-review="proposal-factory"'));assert(!h.includes('data-action="advance"'));assert(h.includes('A live proposal is running'));});
 fixture.proposal_jobs={};paint();ok('Interrupted saved attempt exposes explicit reconciliation without a new paid call',()=>{const h=node('#runbody').innerHTML;assert(h.includes('data-action="advance"'));assert(!h.includes('data-review="proposal-factory"'));});
 liveState.phase='QUEUE_READY';fixture.live_proposals_enabled=false;paint();ok('Workspace without live credentials cannot offer a paid proposal',()=>{const h=node('#runbody').innerHTML;assert(!h.includes('data-review="proposal-governance"'));assert(h.includes('Judges do not enter an API key'));assert(!h.includes('Supply the key locally'));});
 fixture.live_proposals_enabled=true;fixture.principal.roles=['viewer'];paint();ok('Viewer can inspect live attempt evidence but cannot request paid proposals',()=>assert(!node('#runbody').innerHTML.includes('data-review="proposal-')));

 // Workflow mode selection never collects a judge key or authorizes a paid call.
 fixture.principal.roles=['manager'];fixture.live_proposals_enabled=false;paint();
 const noKeyPosts=posts.length;await click(node('#create'));
 ok('Unconfigured NVIDIA option stays visible but disabled without a key field',()=>{const h=node('#reviewbody').innerHTML;assert(h.includes('value="live" disabled'));assert(h.includes('owner setup required'));assert(h.includes('value="scripted" selected'));assert.equal(node('#proposal_mode').value,'scripted');assert(!h.includes('type="password"'));assert.equal(posts.length,noKeyPosts);});
 ok('Downloadable live-access guidance retains a developer contact without collecting a key',()=>{const h=node('#reviewbody').innerHTML;assert(h.includes('For live evaluation access, contact the developer'));assert(h.includes('https://github.com/williamf48'));assert(h.includes('API credentials are not bundled'));assert(!h.includes('type="password"'));});
 ok('Judge setup guidance asks the owner rather than the judge to configure access',()=>{const h=node('#reviewbody').innerHTML;assert(h.includes('Judges do not enter an API key'));assert(!h.includes('NEBIUS_API_KEY'));assert(!h.includes('Workspace owner setup</summary>'));});
 node('#proposal_mode').value='live';node('#consent').checked=true;await click(node('#confirm'));
 ok('Forcing an unavailable live choice cannot submit a creation or paid request',()=>{assert.equal(posts.length,noKeyPosts);assert(node('#notice').innerHTML.includes('workspace-owner setup'));});await click(node('#cancel'));
 fixture.principal.roles=['admin','manager'];paint();await click(node('#create'));
 ok('Administrator sees one-time server configuration guidance with no secret input',()=>{const h=node('#reviewbody').innerHTML;assert(h.includes('NEBIUS_API_KEY'));assert(h.includes('OWNER_LIVE_SETUP.md'));assert(!h.includes('type="password"'));});await click(node('#cancel'));
 fixture.live_proposals_enabled=true;fixture.principal.roles=['manager'];paint();await click(node('#create'));
 node('#duration_minutes').value='60';node('#profile').value='balanced';node('#customworkflow').value='';node('#consent').checked=true;
 const beforeLiveCreate=posts.length;await click(node('#confirm'));
 ok('NVIDIA selection creates one live workflow without a key or chained proposal request',()=>{const requests=posts.slice(beforeLiveCreate);assert.equal(requests.length,1);assert.deepEqual(JSON.parse(JSON.stringify(requests[0].body)),{action:'create',proposal_mode:'live',duration_minutes:60,profile:'balanced'});});
 await click(node('#create'));node('#proposal_mode').value='scripted';node('#consent').checked=true;await click(node('#confirm'));
 ok('Configured NVIDIA workspace still lets a judge explicitly choose a free rehearsal',()=>assert.equal(posts.at(-1).body.proposal_mode,'scripted'));
 // Authentication transitions must clear only old authentication feedback.
 rule=async(path)=>path==='/api/login'?reply({error:'Account or password was not accepted.'},403):null;
 run('login(false)');node('#username').value='Test manager';node('#password').value='Fixture password only!';
 await node('#loginform').onsubmit({preventDefault(){}});
 ok('A failed login shows its reason and enables another explicit attempt',()=>{assert(node('#notice').innerHTML.includes('Account or password'));assert.equal(node('#signin').disabled,false);assert.equal(run('state'),null);});
 rule=async(path)=>path==='/api/login'?reply({csrf:fixture.csrf,principal:fixture.principal}):null;
 await node('#loginform').onsubmit({preventDefault(){}});
 ok('Successful login removes the earlier password warning and clears the password field',()=>{assert.equal(node('#notice').innerHTML,'');assert.equal(node('#password').value,'');assert(node('#account').innerHTML.includes(fixture.principal.name));});
 let releaseLogin;rule=async(path)=>path==='/api/login'?new Promise(resolve=>{releaseLogin=()=>resolve(reply({csrf:fixture.csrf,principal:fixture.principal}));}):null;
 run('login(false)');node('#username').value='Test manager';node('#password').value='Fixture password only!';
 const loggingIn=node('#loginform').onsubmit({preventDefault(){}});await Promise.resolve();
 const loginPosts=posts.filter(p=>p.path==='/api/login').length;
 await node('#loginform').onsubmit({preventDefault(){}});releaseLogin();await loggingIn;
 ok('A double login submission sends one request',()=>assert.equal(posts.filter(p=>p.path==='/api/login').length,loginPosts));
 rule=null;run("notice('Earlier account warning');pending={action:'approve-review'}");await click(node('#logout'));
 ok('Sign out clears the warning, principal, CSRF and pending effect review',()=>{assert.equal(node('#notice').innerHTML,'');assert.equal(run('state'),null);assert.equal(run('csrf'),'');assert.equal(run('pending'),null);assert(!node('#review').open);});
 rule=async(path)=>path==='/api/state'?reply({error:'Sign in again.'},401):path==='/api/public-status'?reply({setup_required:false}):null;
 await run('refresh()');
 ok('Session expiry returns to login while preserving saved evidence on the server',()=>{assert(node('#app').innerHTML.includes('Sign in'));assert.equal(run('state'),null);assert.equal(run('pending'),null);assert(node('#app').innerHTML.includes('30 minutes'));});
 rule=null;fixture.principal.roles=['viewer'];let exportedState=active('MONITORING');
 ok('Viewer can download a read-only archive for the selected run',()=>{const h=node('#runbody').innerHTML;assert(h.includes('Download evidence ZIP'));assert(h.includes('/api/evidence?run='+encodeURIComponent(run('current'))));assert(h.includes('no credentials or raw prompts'));});
 exportedState.lifecycle='STOPPED';paint();
 ok('Stopped and historical evidence keeps download access without effect controls',()=>{assert(node('#runbody').innerHTML.includes('Download evidence ZIP'));assert(!node('#runbody').innerHTML.includes('data-review="batch"'));exportedState.historical_operations=true;paint();assert(node('#runbody').innerHTML.includes('Download evidence ZIP'));});
 const oldExportPosts=posts.length;await run('refresh()');await run('refresh()');
 ok('Dashboard polling does not automatically export evidence or submit an operation',()=>{assert.equal(posts.length,oldExportPosts);assert.equal(gets.filter(p=>p.startsWith('/api/evidence')).length,0);});
 const savedId=fixture.runs[0].id;fixture.runs[0].id='unsafe"><script>fixture</script>';run('current=fixture.runs[0].id');paint();
 ok('The download query encodes the selected identifier as data',()=>{const h=node('#runbody').innerHTML;assert(h.includes('/api/evidence?run='+encodeURIComponent(fixture.runs[0].id)));assert(!h.includes('<script>fixture</script>'));});
 fixture.runs[0].id=savedId;
 fixture.principal.roles=['manager'];await click(node('#create'));
 ok('Creation explains run time separately from credits and login sessions',()=>{const h=node('#reviewbody').innerHTML;assert(h.includes('Run duration'));assert(h.includes('value="60" selected'));assert(h.includes('value="30"'));assert(h.includes('value="15"'));assert(h.includes('not Nebius credits'));assert(h.includes('signing back in preserves this run'));});
 node('#duration_minutes').value='30';node('#profile').value='balanced';node('#customworkflow').value='';node('#consent').checked=true;await click(node('#confirm'));
 ok('Chosen duration reaches the create request and saved countdown',()=>{const b=posts.findLast(p=>p.path==='/api/action'&&p.body.action==='create').body;assert.equal(b.duration_minutes,30);assert.equal(run('timeLeft(fixture.runs[0].summary)'),'30:00');});
 const fixedDeadline=fixture.runs[0].summary.expires_unix;now+=10000;await run('refresh()');await run('refresh()');
 ok('Polling never renews a chosen run duration',()=>assert.equal(fixture.runs[0].summary.expires_unix,fixedDeadline));
 // Explicit session-key entry; the fake network only captures nonsecret fixtures.
 const sampleKey='fixture-nebius-key-123456';
 fixture.principal={name:'Key test manager',subject:'key-owner-fixture',roles:['manager']};
 fixture.proposal_access={browser_key_entry:true};fixture.live_proposals_enabled=false;fixture.proposal_jobs={};
 paint();const beforeKeyDialog=posts.length;await click(node('#create'));
 ok('Manual-key capability leaves the free default and makes NVIDIA selectable',()=>{const h=node('#reviewbody').innerHTML;assert(h.includes('value="live" >'));assert(!h.includes('value="live" disabled'));assert.equal(node('#proposal_mode').value,'scripted');assert.equal(node('#nvidiaentry').hidden,true);assert.equal(posts.length,beforeKeyDialog);});
 node('#proposal_mode').value='live';for(const fn of listeners.change||[])await fn({target:node('#proposal_mode')});
 ok('Selecting NVIDIA reveals a masked key field without a network operation',()=>{assert.equal(node('#nvidiaentry').hidden,false);assert.equal(node('#nvidiakeyfields').hidden,false);const h=node('#reviewbody').innerHTML;assert(h.includes('id="nebius_api_key"'));assert(h.includes('type="password"'));assert(h.includes('autocomplete="off"'));assert.equal(posts.length,beforeKeyDialog);});
 ok('Key-entry text explains memory lifetime and the running server boundary',()=>{const h=node('#reviewbody').innerHTML;for(const text of ['running app server','held in memory','not saved in workflows or exported evidence','Password masking only hides it','session expiry or app restart'])assert(h.includes(text),text);});
 node('#consent').checked=true;await click(node('#confirm'));
 ok('NVIDIA creation needs a nonempty key when workspace access is unavailable',()=>{assert.equal(posts.length,beforeKeyDialog);assert(node('#notice').innerHTML.includes('Enter the Nebius API key'));});
 node('#nebius_api_key').value=sampleKey;node('#consent').checked=false;await click(node('#confirm'));
 ok('Entering a key does not bypass workflow-creation consent',()=>assert.equal(posts.length,beforeKeyDialog));
 node('#consent').checked=true;node('#duration_minutes').value='60';node('#profile').value='balanced';node('#customworkflow').value='';
 let clearBeforeSend=false;rule=async(path,body)=>{if(path==='/api/action'&&body.action==='create')clearBeforeSend=node('#nebius_api_key').value==='';return null;};
 await click(node('#confirm'));rule=null;
 ok('A key is sent only in the explicit create request and the input clears before sending',()=>{assert(clearBeforeSend);const added=posts.slice(beforeKeyDialog);assert.equal(added.length,1);assert.equal(added[0].path,'/api/action');assert.equal(added[0].body.nebius_api_key,sampleKey);assert.equal(added[0].body.proposal_mode,'live');assert.equal(node('#nebius_api_key').value,'');assert.equal(run('pending'),null);});
 await click(node('#create'));node('#proposal_mode').value='live';node('#nvidia_access').value='key';node('#nebius_api_key').value=sampleKey;
 node('#proposal_mode').value='scripted';for(const fn of listeners.change||[])await fn({target:node('#proposal_mode')});
 ok('Switching back to free rehearsal clears and hides the key',()=>{assert.equal(node('#nebius_api_key').value,'');assert.equal(node('#nvidiaentry').hidden,true);});
 node('#nebius_api_key').value=sampleKey;await click(node('#cancel'));
 ok('Cancelling creation removes the entered key',()=>{assert.equal(node('#nebius_api_key').value,'');assert.equal(run('pending'),null);});
 await click(node('#create'));node('#nebius_api_key').value=sampleKey;for(const fn of listeners.cancel||[])await fn({target:{id:'review'}});
 ok('Escape dismissal removes the entered key without a request',()=>{assert.equal(node('#nebius_api_key').value,'');assert.equal(run('pending'),null);});
 fixture.live_proposals_enabled=true;paint();await click(node('#create'));
 ok('Configured workspace access remains the default with an optional session-key choice',()=>{assert.equal(node('#proposal_mode').value,'live');assert.equal(node('#nvidia_access').value,'workspace');assert.equal(node('#nvidiakeyfields').hidden,true);});
 node('#nvidia_access').value='key';for(const fn of listeners.change||[])await fn({target:node('#nvidia_access')});node('#nebius_api_key').value=sampleKey;
 node('#nvidia_access').value='workspace';for(const fn of listeners.change||[])await fn({target:node('#nvidia_access')});node('#consent').checked=true;await click(node('#confirm'));
 ok('Choosing workspace access clears the optional key and excludes it from the request',()=>{assert.equal(node('#nebius_api_key').value,'');assert(!Object.hasOwn(posts.at(-1).body,'nebius_api_key'));});
 fixture.live_proposals_enabled=false;const personalState=active('DRAFT');personalState.live=true;personalState.calls=[];personalState.model_calls=0;
 personalState.operations.browser_access={owner:fixture.principal.subject,kind:'SESSION_KEY'};fixture.proposal_credentials={};paint();
 ok('A disconnected personal workflow offers reconnection instead of a paid action',()=>{const h=node('#runbody').innerHTML;assert(h.includes('Reconnect NVIDIA access'));assert(!h.includes('data-review="proposal-factory"'));});
 const beforeReconnect=posts.length;await click(node('#connectnvidia'));node('#nebius_api_key').value=sampleKey;node('#consent').checked=true;
 rule=async(path)=>{if(path==='/api/proposal-access'){fixture.proposal_credentials[run('current')]=true;return reply({status:'CONNECTED',model_calls:0});}return null;};
 await click(node('#confirm'));rule=null;
 ok('Reconnect sends one key-configuration request and never a paid approval',()=>{const added=posts.slice(beforeReconnect);assert.equal(added.length,1);assert.equal(added[0].path,'/api/proposal-access');assert.equal(added[0].body.nebius_api_key,sampleKey);assert.equal(node('#nebius_api_key').value,'');assert(node('#runbody').innerHTML.includes('data-review="proposal-factory"'));});
 await click(node('#create'));node('#proposal_mode').value='live';node('#nvidia_access').value='key';node('#nebius_api_key').value=sampleKey;node('#consent').checked=true;
 rule=async(path)=>{if(path==='/api/action')throw Error(sampleKey);return null;};await click(node('#confirm'));rule=null;
 ok('A lost key-creation reply clears the field without printing the key or retrying',()=>{assert.equal(node('#nebius_api_key').value,'');assert(!node('#notice').innerHTML.includes(sampleKey));assert(node('#notice').innerHTML.includes('unconfirmed'));assert.equal(run('pending'),null);});
 fixture.proposal_credentials={};personalState.phase='DRAFT';personalState.lease_elapsed=false;personalState.expires_unix=now/1000+100;paint();
 await click(node('#connectnvidia'));node('#nebius_api_key').value=sampleKey;personalState.expires_unix=now/1000-1;paint();
 ok('A lease expiry closes the key-reconnection dialog and clears its input',()=>{assert.equal(node('#nebius_api_key').value,'');assert.equal(run('pending'),null);assert(!node('#review').open);});
 await click(node('#create'));node('#nebius_api_key').value=sampleKey;run('login(false)');
 ok('Returning to login clears any remaining API-key input',()=>assert.equal(node('#nebius_api_key').value,''));
 // The overview reads saved evidence and never grants or executes work.
 const savedFixture=structuredClone(fixture),postsBeforeOverview=posts.length;
 fixture.principal={name:'judge-admin',subject:'judge-fixture',roles:['admin','manager','owner_C','deployer','cost','traffic','consumer','viewer']};
 fixture.evaluation={account:'judge-admin',single_account_available:true};fixture.runs=[];run("current=''");paint();
 ok('An empty evaluation starts with a full dashboard instead of a blank run panel',()=>{const h=node('#app').innerHTML;assert(h.includes('Evaluation dashboard'));assert(h.includes('Workflows in this view'));assert(h.includes('Create a guided workflow'));});
 ok('The empty dashboard explains all three agents without claiming they ran',()=>{const h=node('#app').innerHTML;for(const label of ['Scenario planning','Governance','Recovery'])assert(h.includes(label));assert(!h.includes('badge complete'));assert(node('#runbody').innerHTML.includes('No pending approvals'));assert(!node('#runbody').innerHTML.includes('data-review='));});
 ok('Full judge permissions are disclosed as single-account evaluation access',()=>{const h=node('#app').innerHTML;assert(h.includes('Complete evaluation access'));assert(h.includes('single-account evaluation'));assert(h.includes('Manage users'));assert(h.includes('Create a workflow'));});
 await click(node('#guidedstart'));
 ok('Guided start opens the main-app form with NVIDIA key entry',()=>{assert(node('#review').open);const h=node('#reviewbody').innerHTML;assert(h.includes('NVIDIA Nemotron through Nebius'));assert(h.includes('nebius_api_key'));assert(h.includes('value="60" selected'));assert(h.includes('Balanced'));});
 ok('Opening the dashboard and guided form sends zero operations',()=>assert.equal(posts.length,postsBeforeOverview));
 await click(node('#cancel'));fixture.principal.roles=['admin','viewer'];paint();
 ok('Administration-only accounts get instructions without workflow permissions',()=>{const h=node('#app').innerHTML;assert(h.includes('This account cannot create workflows'));assert(h.includes('judge-admin'));assert(!h.includes('id="guidedstart"'));assert(!h.includes('id="create"'));});
 fixture.principal.roles=['viewer'];paint();
 ok('The overview never adds administrative authority to a viewer',()=>{const h=node('#app').innerHTML;assert(!h.includes('Manage users'));assert(!h.includes('Complete evaluation access'));assert(!h.includes('id="guidedstart"'));});
 fixture.principal.roles=['admin','manager','owner_C','deployer','cost','traffic','consumer','viewer'];
 const dashboardRun=structuredClone(savedFixture.runs[0]);dashboardRun.id='overview-current';
 Object.assign(dashboardRun.summary,{phase:'AWAITING_OWNER_REVIEW',lifecycle:'LEASED',expires_unix:now/1000+600,historical_operations:false,lease_elapsed:false,live:false});
 dashboardRun.summary.operations.browser_access=undefined;dashboardRun.summary.operations.emergency=null;dashboardRun.summary.operations.jobs=[];dashboardRun.summary.calls=[];dashboardRun.summary.events=[];dashboardRun.summary.health=[];dashboardRun.summary.recovery=null;dashboardRun.summary.incident=null;
 const stoppedRun=structuredClone(dashboardRun);stoppedRun.id='overview-stopped';stoppedRun.summary.lifecycle='STOPPED';
 const historicalRun=structuredClone(dashboardRun);historicalRun.id='overview-historical';historicalRun.summary.historical_operations=true;
 fixture.runs=[dashboardRun,stoppedRun,historicalRun];run("current='overview-current'");paint();
 ok('The workflow overview counts only active current-source runs',()=>{const h=run('workspaceOverview(fixture,fixture.runs[0].summary)');assert(h.includes('<strong>3</strong>'));assert.equal((h.match(/<strong>1<\/strong>/g)||[]).length,2);});
 ok('Saved statuses remain separate from successful agent or effect claims',()=>{const h=run('workspaceOverview(fixture,fixture.runs[0].summary)');assert(h.includes('Waiting for proposal'));assert(h.includes('Not executed'));assert(h.includes('0 health windows'));assert(h.includes('0 of 2 role reviews'));assert(!h.includes('Confirmed'));});
 ok('All evaluation stages are visible together from the saved workflow',()=>{const h=node('#app').innerHTML;for(const text of ['Selected workflow at a glance','Batch execution','Health and recovery','Customer handoff','Outcome and evidence','All workflows in this view'])assert(h.includes(text),text);});
 await click({dataset:{openrun:'overview-stopped'}});
 ok('Opening a saved workflow changes selection without executing work',()=>{assert.equal(run('current'),'overview-stopped');assert.equal(posts.length,postsBeforeOverview);assert(node('#runbody').innerHTML.includes('View outcome report'));});
 dashboardRun.id='<img src=x onerror=bad>';dashboardRun.summary.phase='<script>bad</script>';paint();
 ok('The workflow overview escapes untrusted identifiers and phase text',()=>{const h=node('#app').innerHTML;assert(h.includes('&lt;img'));assert(h.includes('&lt;script&gt;'));assert(!h.includes('<img src=x'));assert(!h.includes('<script>bad'));});
 await click({dataset:{openrun:'not-a-saved-run'}});
 ok('An unknown workflow shortcut is rejected without an API mutation',()=>{assert.equal(posts.length,postsBeforeOverview);assert(node('#notice').innerHTML.includes('Select a saved workflow'));});
 fixture.evaluation=null;paint();
 ok('The main app shares the richer overview without claiming a judge workspace',()=>{const h=node('#app').innerHTML;assert(h.includes('Agent coordination overview'));assert(!h.includes('Evaluation dashboard'));assert(!h.includes('Complete evaluation access'));});
 const overview=run('workspaceOverview(fixture,fixture.runs[0].summary)');
 ok('Dashboard status is a saved snapshot rather than renewed authority',()=>{assert(overview.includes('Status comes from the saved workflow'));assert.equal(posts.length,postsBeforeOverview);});
 Object.assign(fixture,savedFixture);paint();
 ok('The complete dashboard preserves separate exact-review controls',()=>{const current=active('AWAITING_OWNER_REVIEW');current.service.actual_at_outcome.candidate_percent=20;current.service.policy_snapshot.state.deployments['candidate-v2'].dependencies=[{id:'C-ui-fixture',owner_run:'route-C'}];fixture.principal.roles=['admin','manager','owner_C'];paint();const h=node('#runbody').innerHTML;assert(h.includes('data-review="failover-manager"'));assert(h.includes('data-review="failover-owner_C"'));assert(h.includes('Review release'));});
 // Phase-aware guidance is presentation only and reuses the exact review path.
 const guided=active('AWAITING_BATCH_APPROVAL'),beforeGuidance=posts.length;
 guided.mission_id=fixture.runs[0].id;guided.live=false;guided.operations.emergency=null;guided.operations.jobs=[];
 fixture.principal.roles=['admin','manager','owner_C'];fixture.proposal_jobs={};fixture.supervision={};paint();
 ok('The guided next step points to the exact unapproved batch',()=>{const g=run('guideFor(fixture.runs[0].summary,fixture)');assert.equal(g.review,'batch');assert(!g.action);assert(node('#app').innerHTML.includes('Continue your walkthrough'));assert.equal(posts.length,beforeGuidance);});
 await click({dataset:{review:'batch'}});
 ok('Guided review uses the existing preview and starts without consent',()=>{assert.equal(posts.at(-1).path,'/api/review');assert.equal(posts.at(-1).body.kind,'batch');assert.equal(node('#consent').checked,false);assert.equal(run('pending').kind,'batch');});
 await click(node('#cancel'));const guidedPosts=posts.length;
 guided.phase='MONITORING';paint();
 ok('A monitored phase starts observation before suggesting a controlled fault',()=>{assert.equal(run('guideFor(fixture.runs[0].summary,fixture)').action,'supervise');fixture.supervision[run('current')]={status:'RUNNING'};assert.equal(run('guideFor(fixture.runs[0].summary,fixture)').review,'fault-candidate');assert.equal(posts.length,guidedPosts);});
 guided.phase='INCIDENT_RECORDED';paint();
 ok('A scripted incident prepares a recommendation separately from recovery consent',()=>{assert.equal(run('guideFor(fixture.runs[0].summary,fixture)').action,'advance');guided.phase='AWAITING_RECOVERY_APPROVAL';assert.equal(run('guideFor(fixture.runs[0].summary,fixture)').review,'recovery');});
 guided.phase='AWAITING_OWNER_REVIEW';guided.service.actual_at_outcome.candidate_percent=20;guided.operations.emergency={grants:{}};paint();
 ok('Guidance requests manager and customer handoff as separate operations',()=>{assert.equal(run('guideFor(fixture.runs[0].summary,fixture)').review,'failover-manager');guided.operations.emergency.grants.manager={};assert.equal(run('guideFor(fixture.runs[0].summary,fixture)').review,'failover-owner_C');});
 fixture.principal.roles=['manager'];paint();
 ok('Recorded manager approval never grants the missing customer role',()=>{const g=run('guideFor(fixture.runs[0].summary,fixture)');assert(!g.review);assert(g.note.includes('customer-owner account'));});
 guided.operations.emergency.grants.owner_C={};paint();
 ok('Guidance does not repeat an uncertain handoff after both grants',()=>{const g=run('guideFor(fixture.runs[0].summary,fixture)');assert(!g.review);assert(!g.action);assert(g.note.includes('Both role approvals'));});
 fixture.principal.roles=['manager','owner_C'];guided.service.actual_at_outcome.candidate_percent=0;
 guided.service.policy_snapshot.state.deployments['candidate-v2'].dependencies=[{id:'exact-job',owner_run:'route-C'},{id:'other-job',owner_run:'route-C'}];paint();
 ok('Job-release guidance identifies one exact dependency and preserves others',()=>{const g=run('guideFor(fixture.runs[0].summary,fixture)');assert.equal(g.job,'exact-job');assert(g.note.includes('only when it is finished'));assert.equal(guided.service.policy_snapshot.state.deployments['candidate-v2'].dependencies.length,2);});
 guided.lifecycle='STOPPED';guided.teardown={all_workers_stopped:false};paint();
 ok('A stopped snapshot does not falsely claim confirmed shutdown',()=>{const g=run('guideFor(fixture.runs[0].summary,fixture)');assert(g.href.includes('/outcome?run='));assert(g.note.includes('before assuming'));assert(!g.action);});
 guided.lifecycle='LEASED';guided.phase='DRAFT';guided.live=true;guided.operations.browser_access={owner:fixture.principal.subject,kind:'SESSION_KEY'};fixture.proposal_credentials={};fixture.proposal_access={browser_key_entry:false};paint();
 ok('Guidance respects disabled entered-key capability',()=>assert(!run('guideFor(fixture.runs[0].summary,fixture)').id));
 fixture.proposal_access.browser_key_entry=true;fixture.proposal_jobs[run('current')]={status:'RUNNING'};paint();
 ok('An in-flight proposal never gains a duplicate paid shortcut',()=>{const g=run('guideFor(fixture.runs[0].summary,fixture)');assert(!g.review);assert(!g.id);assert(g.note.includes('already in progress'));});
 fixture.proposal_jobs={};
 ok('A disconnected original manager can use the existing reconnection review',()=>assert.equal(run('guideFor(fixture.runs[0].summary,fixture)').id,'guideconnect'));
 fixture.session={expires_unix:200000,server_now_unix:198500,duration_seconds:1800,renewed_by_reads:false};paint();
 ok('Login timing uses the server interval independently of the browser clock',()=>{const h=node('#app').innerHTML;assert(h.includes('Login session: 25:00 remaining'));assert(h.includes('separate from each workflow timer'));});
 fixture.session.server_now_unix=199750;paint();
 ok('Expiring login explains saved progress and key reconnection without renewal',()=>{const h=node('#app').innerHTML;assert(h.includes('4:10 remaining'));assert(h.includes('saved workflows stay available'));assert(h.includes('NVIDIA key must be reconnected'));assert.equal(fixture.session.expires_unix,200000);});
 const panels=[{dataset:{viewkey:'evidence'},open:true},{dataset:{viewkey:'notifications'},open:false}];
 ctx.document.querySelectorAll=selector=>selector.endsWith('[open]')?panels.filter(p=>p.open):panels;
 run('renderedRun=current;rememberExpanded()');panels.forEach(p=>p.open=false);run('restoreExpanded()');
 ok('Expanded evidence survives a refresh while closed notifications remain closed',()=>{assert(panels[0].open);assert(!panels[1].open);});
 const selected=run('current');run("current='different-workflow';restoreExpanded()");
 ok('Expansion state is scoped to the selected workflow',()=>assert(panels.every(p=>!p.open)));
 run('current='+JSON.stringify(selected)+';restoreExpanded()');
 ok('Returning to a workflow restores only its prior expanded panels',()=>{assert(panels[0].open);assert(!panels[1].open);});
 let scrolled=null;ctx.scrollY=480;ctx.scrollTo=(x,y)=>{scrolled=[x,y];};paint();
 ok('Same-workflow polling preserves the current page position',()=>assert.deepEqual(scrolled,[0,480]));
 ok('Guidance timing and expansion state create no automatic operations',()=>assert.equal(posts.length,guidedPosts));
 const selectionPosts=posts.length,selectedLive='live-selection-regression';
 const newLive=structuredClone(summary);Object.assign(newLive,{mission_id:selectedLive,live:true,phase:'FACTORY_REQUESTING',lifecycle:'LEASED',historical_operations:false,expires_unix:now/1000+3600,ttl:3600,calls:[{role:'factory',status:'REQUEST_STARTED',live:true}],model_calls:1});
 fixture.principal.roles=['manager'];fixture.live_proposals_enabled=true;fixture.proposal_jobs={};fixture.runs=[{id:selectedLive,summary:newLive},{id:'old-scripted-evidence',summary:structuredClone(summary)}];
 run('current='+JSON.stringify(selectedLive));paint();
 const temporarilyMissing=fixture.runs.shift();paint();
 ok('A missing refresh snapshot keeps the exact selected workflow',()=>{assert.equal(run('current'),selectedLive);assert(node('#app').innerHTML.includes('Unavailable in this refresh'));assert(node('#runbody').innerHTML.includes('Your workflow remains selected'));});
 ok('Missing selected evidence cannot expose another workflow or actionable controls',()=>{const h=node('#runbody').innerHTML;assert(!h.includes('Free rehearsal'));assert(!h.includes('data-review'));assert(!h.includes('data-action'));assert.equal(node('#overview').innerHTML,'');assert.equal(posts.length,selectionPosts);});
 fixture.runs.unshift(temporarilyMissing);paint();
 ok('Returning evidence restores the same NVIDIA request without another call',()=>{assert.equal(run('current'),selectedLive);assert(node('#runbody').innerHTML.includes('NVIDIA Nemotron'));assert.equal(posts.length,selectionPosts);});
 run("pending={action:'approve-review',run:current,kind:'batch'}");node('#review').open=true;node('#nebius_api_key').value='Fixture key cleared on missing evidence';fixture.runs.shift();paint();
 ok('Missing reviewed evidence closes pending consent and clears entered credentials',()=>{assert.equal(run('pending'),null);assert(!node('#review').open);assert.equal(node('#nebius_api_key').value,'');});
 run('current='+JSON.stringify('<script>missing-id</script>'));paint();
 ok('Unavailable selected identifiers are escaped in the workflow selector',()=>{assert(node('#app').innerHTML.includes('&lt;script&gt;missing-id&lt;/script&gt;'));assert(!node('#app').innerHTML.includes('<script>missing-id'));});
 fixture.runs.unshift(temporarilyMissing);run('current='+JSON.stringify(selectedLive));newLive.phase='STOPPED_FOR_REVIEW';newLive.calls[0].status='INVALID';newLive.calls[0].validation={status:'REJECTED',code:'FACTORY_METADATA_CHANGED',message:'Changed <script>untrusted</script> authority'};paint();
 ok('Stopped proposal shows an escaped specific reason without paid or advance controls',()=>{const h=node('#runbody').innerHTML;assert(h.includes('FACTORY_METADATA_CHANGED'));assert(h.includes('Changed &lt;script&gt;untrusted&lt;/script&gt; authority'));assert(!h.includes('<script>untrusted'));assert(!h.includes('data-review="proposal-factory"'));assert(!h.includes('data-action="advance"'));assert(node('#app').innerHTML.includes('Inspect Proposal needs attention'));});
 delete newLive.calls[0].validation;paint();
 ok('Older stopped attempts retain an honest generic diagnostic without an invented cause',()=>{assert(node('#runbody').innerHTML.includes('validation or local initialization'));assert(!node('#runbody').innerHTML.includes('FACTORY_METADATA_CHANGED'));});
 newLive.phase='QUEUE_READY';newLive.calls[0].status='VALIDATED';newLive.calls[0].validation={status:'PASSED',authority_source:'REGISTERED_WORKFLOW',readonly_echoes_verified:[]};paint();
 ok('Validated readonly echoes expose only the next separately reviewed Governance request',()=>{const h=node('#runbody').innerHTML;assert(h.includes('data-review="proposal-governance"'));assert(!h.includes('aria-label="Proposal failure"'));assert.equal(posts.length,selectionPosts);});

 newLive.phase='GOVERNANCE_DEFERRED';newLive.decisions={...(newLive.decisions||{}),governance:{decision:'defer',reason:'Conflicts <script>untrusted</script> require review.'}};paint();
 ok('Validated deferrals display the recorded escaped reason without claiming correctness',()=>{const h=node('#runbody').innerHTML;assert(h.includes('aria-label="Agent deferral"'));assert(h.includes('Conflicts &lt;script&gt;untrusted&lt;/script&gt;'));assert(!h.includes('<script>untrusted'));assert(h.includes('explanation has not been proved correct'));});
 ok('Deferral guidance exposes no continuation paid retry or batch approval',()=>{const h=node('#runbody').innerHTML;assert(!h.includes('data-action="advance"'));assert(!h.includes('data-review="proposal-governance"'));assert(!h.includes('data-review="batch"'));assert(!node('#app').innerHTML.includes('Use Continue saved phase'));assert(!h.includes('Use Continue saved phase'));assert.equal(posts.length,selectionPosts);});
 delete newLive.decisions.governance;paint();
 ok('An absent saved deferral reason is labelled missing rather than invented',()=>{assert(node('#runbody').innerHTML.includes('No reason is present in this saved snapshot'));assert.equal(posts.length,selectionPosts);});
 newLive.phase='QUEUE_READY';paint();
 const navPosts=posts.length;
 ok('The sidebar exposes separate operational sections and a Safety Lab link',()=>{const h=node('#app').innerHTML;for(const view of ['overview','workflows','agents','approvals','monitoring','evidence'])assert(h.includes('data-nav="'+view+'"'));assert(h.includes('id="safetynav"'));});
 for(const view of ['workflows','agents','approvals','monitoring','evidence','overview'])await click({dataset:{nav:view}});
 ok('Section navigation makes no HTTP operations and keeps the workflow selected',()=>{assert.equal(posts.length,navPosts);assert.equal(run('current'),selectedLive);assert.equal(run('activeView'),'overview');});
 ok('Each view is separately marked and the inactive views start hidden',()=>{const h=node('#runbody').innerHTML;for(const view of ['workflows','agents','approvals','monitoring','evidence'])assert(h.includes('data-workspace-view="'+view+'" hidden'));assert(node('#overview').innerHTML.includes('data-workspace-view="overview"'));});
 run("pending={action:'approve-review',run:current,kind:'proposal-governance'}");node('#review').open=true;node('#nebius_api_key').value='Fixture nav private key';await click({dataset:{nav:'agents'}});
 ok('Changing section cancels unsubmitted consent and clears its key',()=>{assert.equal(run('pending'),null);assert(!node('#review').open);assert.equal(node('#nebius_api_key').value,'');assert.equal(posts.length,navPosts);});
 paint();ok('Polling preserves the selected workspace section',()=>assert.equal(run('activeView'),'agents'));
 node('#workspace-content').scrollTop=222;await click({dataset:{nav:'evidence'}});node('#workspace-content').scrollTop=77;await click({dataset:{nav:'agents'}});
 ok('Each workflow section restores its own content scroll position',()=>assert.equal(node('#workspace-content').scrollTop,222));
 fixture.principal.roles=['viewer'];paint();await click({dataset:{nav:'users'}});
 ok('A viewer cannot navigate into user administration or create consent',()=>{assert.equal(run('activeView'),'agents');assert(!node('#app').innerHTML.includes('data-nav="users"'));assert(!node('#runbody').innerHTML.includes('Named workspace accounts'));assert.equal(posts.length,navPosts);});
 await click({dataset:{nav:'unknown'}});
 ok('Unknown section names are rejected without an operation',()=>{assert.equal(run('activeView'),'agents');assert.equal(posts.length,navPosts);});
 fixture.principal.roles=['admin','manager'];paint();await click({dataset:{nav:'users'}});
 ok('Administrators have a dedicated named-account section',()=>{assert.equal(run('activeView'),'users');assert(node('#runbody').innerHTML.includes('Named workspace accounts'));});
 const actualPanels=Object.keys(run('VIEWS')).map(v=>({dataset:{workspaceView:v},hidden:false})),navButtons=Object.keys(run('VIEWS')).map(v=>({dataset:{nav:v},setAttribute(k,v){this[k]=v;},removeAttribute(k){delete this[k];}}));
 const originalQueries=ctx.document.querySelectorAll;ctx.document.querySelectorAll=sel=>sel==='[data-workspace-view]'?actualPanels:sel==='[data-nav]'?navButtons:[];
 for(const view of ['overview','agents','approvals','monitoring','evidence']){run('navigate('+JSON.stringify(view)+')');assert.equal(actualPanels.filter(p=>!p.hidden).length,1);assert.equal(actualPanels.find(p=>!p.hidden).dataset.workspaceView,view);}
 ok('Exactly one workspace section remains visible when navigating',()=>assert.equal(actualPanels.filter(p=>!p.hidden).length,1));
 ok('The selected navigation button is identified for assistive technology',()=>{assert.equal(navButtons.find(b=>b.dataset.nav==='evidence')['aria-current'],'page');assert.equal(navButtons.filter(b=>b['aria-current']).length,1);});
 const hashHistory=[];ctx.history={replaceState:(s,t,h)=>hashHistory.push(h)};ctx.location={hash:'#view=agents&run=live-selection-regression'};ctx.URLSearchParams=URLSearchParams;
 run("navigate('agents')");ok('The URL stores only section and workflow navigation state',()=>{assert(hashHistory.at(-1).includes('view=agents'));assert(hashHistory.at(-1).includes('run=live-selection-regression'));assert(!hashHistory.at(-1).includes('Fixture'));});
 ok('A reloaded route can restore the selected section and workflow',()=>{const route=run('readRoute()');assert.equal(route.view,'agents');assert.equal(route.run,selectedLive);});
 ctx.document.querySelectorAll=originalQueries;
 run('login(false)');
 ok('Signing out clears per-login expansion state',()=>{assert.equal(Object.keys(run('expandedViews')).length,0);assert.equal(run('renderedRun'),'');});
 const result={status:'PASSED',checks_passed:checks.length,checks,scope:'Node syntax and minimal-DOM execution. No rendered-browser or native-Windows claim; no actual provider request.',rendered_browser:'LAYOUT_PREVIEW_RECORDED_SEPARATELY',source_sha256:crypto.createHash('sha256').update(originalSource).digest('hex'),model_calls:0};
 fs.writeFileSync(process.argv[2]||path.join(__dirname,'ui_smoke.json'),JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));
})().catch(e=>{console.error(e);process.exit(1);});
"""

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('ui_checks.json'))
    args = parser.parse_args()
    node = shutil.which('node')
    if not node:
        parser.error('These optional UI checks require Node.js. Python acceptance does not require it.')
    output = args.output.absolute()
    environment = {k:v for k,v in os.environ.items() if k not in {'NEBIUS_API_KEY','OPENAI_API_KEY'}}
    environment['AGENT_REHEARSAL_UI_ROOT'] = str(Path(__file__).resolve().parent)
    with tempfile.TemporaryDirectory(prefix='agent rehearsal ui checks ') as temporary:
        script = Path(temporary)/'checks.js'
        script.write_text(SCRIPT, encoding='utf-8')
        return subprocess.run([node,str(script),str(output)],env=environment,check=False).returncode

if __name__ == '__main__':
    raise SystemExit(main())
