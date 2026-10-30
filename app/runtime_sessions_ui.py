"""Persistent local session monitoring; effects and consent stay in the terminal."""

SESSION_STYLE = r'''
.sessioncontrols{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin:20px 0}.sessioncontrols label{display:block;min-width:190px;flex:1}.sessioncontrols select{width:100%;margin-top:7px}
.sessioncommands code{white-space:pre-wrap;overflow-wrap:anywhere;display:block;padding:14px;background:var(--bg);border-radius:10px;margin:10px 0}.sessionidentity{overflow-wrap:anywhere}
'''

SESSION_SCRIPT = r'''
let localSessionTimer=null,localSessionData=null,localSessionSignature="",localSessionSubmitting=false,localSessionHandledJob=null;
function sessionRunOptions(){return (catalog.runs.runtime_session||[]).map(r=>`<option value="${esc(r.id)}" ${r.id===selected.runtime_session?"selected":""}>${esc(stamp(r.id))} · ${esc(r.summary.scenario)} · ${esc(r.summary.lifecycle==="STOPPED"?"stopped":human(r.summary.status))}</option>`).join("");}
function sessionSignature(data){return JSON.stringify({...data.session,heartbeat_at:null,heartbeat_unix:null,liveness_scope:null});}
function updateSessionHeartbeat(){const s=localSessionData?.session,node=$("#sessionheartbeat");if(s&&node)node.textContent=`Last observation: ${s.heartbeat_at} · ${Math.max(0,Math.floor(Date.now()/1000-s.heartbeat_unix))} seconds ago. ${s.lifecycle==="STOPPED"?"Owned exits are recorded separately below.":"Saved observations do not prove current liveness; use terminal status to contact the supervisor."}`;}
function sessionCommands(s){
 const prefix='.\\.venv\\Scripts\\python.exe project.py '+(s.entrypoint||'runtime-sessions')+' ',id=s.session_id;
 const actions=s.lifecycle==="STOPPED"?[]:(s.available_decisions||[]);
 return `<div class="card sessioncommands"><h2>Continue from PowerShell</h2><p class="small">Run these inside your project folder. Owner decisions require the exact displayed phrase in an interactive terminal. Capacity and C's authority remain local fixtures.</p><strong>Contact the same supervisor</strong><code>${esc(prefix+"status --session "+id)}</code>${actions.map(action=>`<strong>${esc(human(action))}</strong><code>${esc(prefix+"review --session "+id+" --decision "+action)}</code>`).join("")}${s.entrypoint==="connected-team"&&s.lifecycle!=="STOPPED"?`<strong>Restart an interrupted supervisor with the same state</strong><code>${esc(prefix+"restart --session "+id)}</code>`:""}${(s.pending_reviews||[]).map(key=>`<strong>Reconcile an existing authorized effect</strong><code>${esc(prefix+"resume --session "+id+" --request "+key)}</code>`).join("")}${s.lifecycle!=="STOPPED"?`<strong>End the local session</strong><code>${esc(prefix+"stop --session "+id)}</code>`:""}</div>`;
}
function paintRuntimeSession(data){
 stopPlay();activeReplay=null;const s=data.session,state=s.policy_snapshot.state,actual=s.actual_at_outcome;
 const stopped=s.lifecycle==="STOPPED",stale=s.heartbeat_stale||s.historical_controller,deadline=new Date(s.expires_unix*1000).toLocaleString();
 $("#sessionbody").innerHTML=`<div class="row"><div class="pillrow">${badge(stopped?"Session stopped":human(s.status),stopped?"info":stale?"warn":"info")}${badge(`${s.model_calls||0} model attempts`,s.model_calls?"info":"good")}${badge(s.scenario+" conflict","info")}</div><button data-session-export>Export public evidence</button></div><p class="small sessionidentity">Session ${esc(s.session_id)} · supervisor ${esc(s.supervisor_pid)} · runtime ${esc(s.runtime_id)}</p><p id="sessionheartbeat" class="small"></p>${stale?'<div class="callout warning">The heartbeat is old or the controller changed. These are saved observations. Inspect terminal status before treating a session as available.</div>':""}${!s.physical_route_matches_policy?'<div class="callout warning">Recorded physical routing differs from confirmed policy. A pending effect must be reconciled; unexpected drift blocks new owner actions.</div>':""}${stopped?`<div class="callout"><strong>${esc(human(s.teardown?.reason||"Stopped"))}</strong> · ${s.teardown?.all_workers_stopped?"Owned process exits confirmed":"Some process exits remain unconfirmed"}. Session teardown is separate from policy recovery. Retained fixture commitments below are not relabelled as released.</div>`:`<div class="callout"><strong>Leased local workers.</strong> The supervisor continues if this dashboard or the launcher closes. Lease target: ${esc(deadline)}. New commands stop at expiry; teardown follows the current bounded operation.</div>`}<div class="grid g4">${metric("Policy commitments",`${s.metrics.committed} / ${s.metrics.limit}`,"Fixture units; stable allocation is preserved",true)}${metric("Candidate traffic",`${actual.candidate_percent}%`,`${100-actual.candidate_percent}% stable · ${actual.route_owner}`)}${metric("Policy recovery",human(s.recovery?.status||"Not recorded"),"Before separate session teardown")}${metric("Workflow A",state.paused_runs.includes("deploy-A")?"Paused":"Inspect state","Owner review cannot resume A")}</div><div class="grid g2">${Object.entries(actual.workers).map(([name,worker])=>{const exit=s.teardown?.workers?.find(w=>w.target===name&&w.pid===worker.pid);return `<div class="card"><div class="panelhead"><h2>${esc(name)}</h2>${badge(exit?.stopped?"Exited in teardown":worker.alive?"Alive at observation":"Exit observed",exit?.stopped?"info":worker.alive?"good":"info")}</div><p class="small">PID ${esc(worker.pid)} · identity ${esc(worker.identity)}</p><p>${esc(worker.healthy?"Healthy arithmetic worker at outcome":"Worker not healthy at outcome")}</p><p class="small">${esc(name==="candidate-v2"?`${state.deployments["candidate-v2"].allocated_units} fixture units; dependencies: ${JSON.stringify(state.deployments["candidate-v2"].dependencies)}`:"Stable work is outside A's cleanup scope.")}</p></div>`;}).join("")}</div>${sessionCommands(s)}<div class="card"><div class="panelhead"><h2>Recorded policy and owner review</h2><button data-session-replay>Inspect journal replay</button></div><p class="small">${s.entrypoint==="connected-team"?"This is the original three-agent service and policy database. Owner decisions continue the recorded execution; supervisor restart verifies the saved worker lifetimes before reattaching.":"This session starts a new scripted local fixture at paused recovery. Earlier three-agent archives remain separate; this does not reopen their stopped runtime."}</p>${evidence("Owner intent receipts",s.owner_reviews)}${evidence("Separate teardown evidence",s.teardown||{status:"Not requested; lease active"})}<div id="sessionreplay"></div></div>`;
 updateSessionHeartbeat();
}
function updateSessionJob(){
 const node=$("#sessionjob"),job=catalog.lab?.job;if(!node)return;
 node.innerHTML=job?`<div class="callout ${["FAILED","STOPPED"].includes(job.status)?"warning":""}"><strong>${esc(human(job.status))}</strong> · ${esc(human(job.kind))}${job.error?`<p>${esc(job.error)}</p>`:""}${job.log?evidence("Launcher log",job.log):""}</div>`:"";
 const button=$("#startsession");if(button)button.disabled=localSessionSubmitting||!catalog.lab?.enabled||!catalog.lab?.available?.runtime_session||["RUNNING","QUEUED"].includes(job?.status);
}
async function pollRuntimeSession(ticket){
 if(ticket!==generation||tab!=="runtime_session")return;
 try{
  const next=await api("/api/catalog");if(ticket!==generation||tab!=="runtime_session")return;catalog=next;
  const job=catalog.lab?.job;
  if(job?.kind==="runtime_session"&&job.status==="COMPLETED"&&localSessionHandledJob!==job.id){localSessionHandledJob=job.id;selected.runtime_session=job.result.run;await render();return;}
  if(!selected.runtime_session&&(catalog.runs.runtime_session||[]).length){await render();return;}
  if($("#sessionrun"))$("#sessionrun").innerHTML=sessionRunOptions();
  if(selected.runtime_session){const data=await api(`/api/run?kind=runtime_session&id=${encodeURIComponent(selected.runtime_session)}`);if(ticket!==generation||tab!=="runtime_session")return;localSessionData=data;const signature=sessionSignature(data);if(signature!==localSessionSignature){localSessionSignature=signature;paintRuntimeSession(data);}else updateSessionHeartbeat();}
  updateSessionJob();
 }catch(err){if(ticket===generation)notice("Session refresh: "+err.message+" Last readable evidence stays visible.");}
 if(ticket===generation&&tab==="runtime_session")localSessionTimer=setTimeout(()=>pollRuntimeSession(ticket),2000);
}
async function runtimeSessions(ticket){
 choose("runtime_session");localSessionData=null;localSessionSignature="";
 $("#content").innerHTML=title("Persistent local operations","Reconnect before you recover.","Keep the same local workers while you inspect paused recovery, retain C's work, or grant an exact withdrawal in the terminal.","Real local workers")+`<div class="sessioncontrols"><label class="small">Saved session<select id="sessionrun">${sessionRunOptions()||'<option value="">No sessions yet</option>'}</select></label><label class="small">New local fixture<select id="sessionscenario"><option value="routing">C owns a route</option><option value="dependency">C owns a dependency</option></select></label><button id="startsession">Start free 5-minute session</button></div><p class="small">Starts two local arithmetic workers. They remain active when this dashboard closes, until the lease ends or you stop the session. At most two unexpired sessions. Owner decisions and shutdown stay in an interactive terminal.</p><div id="sessionjob" role="status" aria-live="polite"></div><div id="sessionbody">${selected.runtime_session?'<div class="loading">Reading session evidence…</div>':'<div class="empty"><h2>Keep work available during review</h2><p>A new scripted routing or dependency fixture waits for C while real local workers continue. No API key or credits are needed.</p></div>'}</div>`;
 if(selected.runtime_session){try{const data=await api(`/api/run?kind=runtime_session&id=${encodeURIComponent(selected.runtime_session)}`);if(ticket!==generation||tab!=="runtime_session")return;localSessionData=data;localSessionSignature=sessionSignature(data);paintRuntimeSession(data);}catch(err){if(ticket===generation)$("#sessionbody").innerHTML=`<div class="callout warning">${esc(err.message)} Waiting for a complete snapshot…</div>`;}}
 updateSessionJob();if(ticket===generation&&tab==="runtime_session")localSessionTimer=setTimeout(()=>pollRuntimeSession(ticket),2000);
}
document.addEventListener("click",async e=>{
 const b=e.target.closest("button");if(!b||!["runtime_session","connected"].includes(tab))return;
 if(b.id==="startsession"){
  if(b.disabled||localSessionSubmitting)return;localSessionSubmitting=true;updateSessionJob();notice("");
  try{const data=await api("/api/jobs",{method:"POST",headers:{"Content-Type":"application/json","X-Rehearsal-Token":LAB_TOKEN},body:JSON.stringify({kind:"runtime_session",scenario:$("#sessionscenario").value})});catalog.lab.job=data.job;}
  catch(err){notice(err.message);}finally{localSessionSubmitting=false;updateSessionJob();}
 }else if(b.hasAttribute("data-session-export")&&localSessionData)download(localSessionData,"orkestryn-runtime-session.json");
 else if(b.hasAttribute("data-session-replay")&&localSessionData)renderReplay(localSessionData,"#sessionreplay");
});
document.addEventListener("change",async e=>{if(e.target.id==="sessionrun"){selected.runtime_session=e.target.value;await render();}});
'''
