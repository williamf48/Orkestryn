"""Inline local-dashboard views for the frozen three-agent evaluation."""

EVALUATION_STYLE = r'''
.sidebar{overflow-y:auto}.evalstatus{margin:15px 0}.evalheader{display:flex;align-items:end;gap:14px;flex-wrap:wrap;margin:20px 0}
.evalheader label{display:block;flex:1;min-width:240px;max-width:680px}.evalheader select{width:100%;margin-top:7px}
.evalchecks{margin-top:15px}.evalchecks summary{padding:10px 0}.evalcompare th,.evalcompare td{white-space:nowrap}
.evalcase{border-top:2px solid var(--teal);margin:20px 0}.evalrunmeta{overflow-wrap:anywhere}.evalscore{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
'''

EVALUATION_SCRIPT = r'''
let teamEvalTimer=null,teamEvalData=null,teamEvalCaseData=null,teamEvalSignature="",teamEvalSubmitting=false,teamEvalHandledJob=null,teamEvalCaseGeneration=0;
const evalIsLive=s=>String(s?.mode||"").startsWith("LIVE ");
function evalRunOptions(){return (catalog.runs.team_evaluation||[]).map(r=>`<option value="${esc(r.id)}" ${r.id===selected.team_evaluation?"selected":""}>${esc(stamp(r.id))} · ${evalIsLive(r.summary)?"live Nemotron":"scripted"} · ${esc(r.summary.split)} · ${esc(human(r.summary.status))}</option>`).join("");}
function evalBool(value){return value===true?badge("Yes","good"):value===false?badge("No","warn"):badge("Not scored");}
function evalComparable(a,b){return a?.benchmark_valid===true&&b?.benchmark_valid===true&&["set_sha256","protocol_digest","case_ids","split","request_profile"].every(k=>JSON.stringify(a[k])===JSON.stringify(b[k]));}
function evalComparison(data){
 const s=data.summary,match=(catalog.runs.team_evaluation||[]).find(r=>r.id!==data.id&&evalIsLive(r.summary)!==evalIsLive(s)&&evalComparable(s,r.summary));
 if(!match)return `<div class="callout">No matching ${evalIsLive(s)?"scripted":"live"} campaign is saved for this exact case selection, split, protocol and inference settings. Earlier standalone or free-choice team runs are not counted as this evaluation.</div>`;
 const other=match.summary,columns=evalIsLive(s)?[other,s]:[s,other];
 return `<div class="card"><h2>Matched scripted / live comparison</h2><p class="small">Same frozen inputs, selected-case denominator, protocol and declared settings. Scripted replies do not estimate model accuracy. A partial live run retains its unrun cases.</p><div class="tablewrap"><table class="evalcompare"><thead><tr><th>Measure</th><th>Scripted</th><th>Live Nemotron</th></tr></thead><tbody>${[["Tasks / selected",x=>ratio(x.task_completed,x.selected)],["Policy safe / scorable",x=>ratio(x.policy_safe,x.policy_scored)],["Unknown / not run",x=>`${fmt(x.unknown_evidence)} / ${fmt(x.not_run)}`],["Preservation",x=>ratio(x.preservation_passed,x.preservation_scored)],["Eligible cleanup",x=>ratio(x.cleanup_complete,x.cleanup_scored)],["Observed HTTP",x=>ratio(x.http_successful,x.http_requests)],["Model request attempts",x=>fmt(x.model_calls)],["Reported tokens",x=>fmt(x.reported_tokens)],["Requests without usage",x=>fmt(x.missing_usage_calls)]].map(([label,value])=>`<tr><td>${esc(label)}</td>${columns.map(x=>`<td>${esc(value(x))}</td>`).join("")}</tr>`).join("")}</tbody></table></div><p class="small readmore">Matched run: ${esc(match.id)}</p></div>`;
}
function paintTeamEvaluation(data){
 stopPlay();activeReplay=null;teamEvalCaseData=null;teamEvalCaseGeneration++;
 const s=data.summary,a=data.active_case;
 $("#teamevalbody").innerHTML=`<div class="row"><div class="pillrow">${badge(s.mode,"info")}${badge(human(s.status),s.status==="COMPLETED"&&s.task_completed===s.selected?"good":"warn")}${badge(s.split+" split","info")}</div><button data-eval-team-export>Export campaign</button></div><p class="small readmore evalrunmeta">${esc(data.id)} · ${esc(s.set_id)} · selected cases: ${esc(s.case_ids.join(", "))}</p>${!evalIsLive(s)?'<div class="callout"><strong>Scripted pipeline regression.</strong> These results verify the coordinator, evidence and scorer over real local workers/HTTP. They do not measure Nemotron accuracy.</div>':""}${!s.benchmark_valid?'<div class="callout warning">This campaign is active or its protocol changed. Its scores are not a completed valid comparison.</div>':""}${data.historical_protocol?'<div class="callout warning">These are recorded historical scores. Recomputing them requires the matching trusted evaluation source.</div>':""}${a?`<div class="callout warning"><strong>${esc(a.id)} · ${esc(human(a.status))}</strong><br>${a.status==="AWAITING_EXACT_MANAGER_CONSENT"?"Switch to the live terminal to review and enter the exact approval phrase.":"Reading active case snapshots; the terminal owns execution."}<br>Active case: ${esc(fmt(a.role_requests))} recorded role requests, ${esc(fmt(a.model_calls))} live-mode attempts. These are included in final campaign totals when the case finishes.</div>`:""}<div class="grid g4">${metric("Tasks / selected",ratio(s.task_completed,s.selected),"Errors and unrun cases stay in this denominator",true)}${metric("Recorded policy safe",ratio(s.policy_safe,s.policy_scored),`${s.policy_unsafe} unsafe · ${s.unknown_evidence} unknown · ${s.not_run} not run`)}${metric("Shared work preserved",ratio(s.preservation_passed,s.preservation_scored),"At policy outcome, before separate teardown")}${metric("Eligible cleanup complete",ratio(s.cleanup_complete,s.cleanup_scored),"Protected candidates remain allocated")}</div><div class="grid g4">${metric("Model request attempts",fmt(s.model_calls),`${s.role_requests} total role requests · cap ${s.call_limit}`)}${metric("Reported tokens",fmt(s.reported_tokens),`${s.missing_usage_calls} requests without usage`)}${metric("Observed HTTP",ratio(s.http_successful,s.http_requests),"Local arithmetic requests; not GPU throughput")}${metric("Unnecessary deferrals",fmt(s.unnecessary_defer),`${s.manager_declined} manager declines · ${s.error_requests} request errors · ${s.invalid_responses} invalid replies`)}</div><div class="card"><div class="panelhead"><h2>Every selected case</h2>${badge(`${s.attempted} attempted / ${s.selected} selected`,"info")}</div><div class="tablewrap"><table><thead><tr><th>Case / stage</th><th>Policy</th><th>Task</th><th>Preserved</th><th>Cleanup</th><th>Calls / tokens</th><th>Evidence</th></tr></thead><tbody>${data.results.map(row=>{const x=row.score;return `<tr><td><strong>${esc(row.id)}</strong><div class="small">${esc(human(row.status))}</div>${row.runner_error?`<div class="small">${esc(row.runner_error)}</div>`:""}</td><td>${badge(x?.safety||"Not scored",x?.safety==="SAFE"?"good":x?.safety==="UNSAFE"?"bad":"warn")}</td><td>${evalBool(x?.task_completed)}</td><td>${evalBool(x?.preservation)}</td><td>${evalBool(x?.eligible_cleanup_complete)}</td><td>${esc(fmt(x?.model_calls??0))} / ${esc(fmt(x?.reported_tokens??0))}</td><td><button class="linkbutton" data-eval-team-case="${esc(row.id)}" ${x?"":"disabled"}>Inspect →</button></td></tr>`;}).join("")}</tbody></table></div></div><div id="teamevalcase"></div>${evalComparison(data)}<details class="evalchecks"><summary>Protocol, denominator and scope</summary><div class="card"><p class="small">Factory receives four fixed numeric inputs; governance receives current state, and recovery receives exact approved execution after a durable pause. The two conflict templates are shared across splits. Confirmation reserves four numeric combinations, not unseen families or an independently blinded holdout.</p>${evidence("Saved protocol and counters",s)}</div></details>`;
}
function updateTeamEvalJob(){
 const target=$("#teamevaljob");if(!target)return;
 const j=catalog.lab?.job;
 target.innerHTML=j?`<div class="callout ${["FAILED","STOPPED"].includes(j.status)?"warning":""}"><strong>${esc(human(j.status))}</strong> · ${esc(human(j.kind))}${j.error?`<p>${esc(j.error)}</p>`:""}${j.log?evidence("Runner log",j.log):""}</div>`:"";
 const button=$("#startevaluation");if(button)button.disabled=teamEvalSubmitting||!catalog.lab?.enabled||!catalog.lab?.available?.team_evaluation||["QUEUED","RUNNING"].includes(j?.status);
}
async function pollTeamEvaluation(ticket){
 if(ticket!==generation||tab!=="team_evaluation")return;
 try{
  const next=await api("/api/catalog");if(ticket!==generation||tab!=="team_evaluation")return;catalog=next;
  const job=catalog.lab?.job;
  if(job?.status==="COMPLETED"&&job.kind==="team_evaluation"&&teamEvalHandledJob!==job.id){teamEvalHandledJob=job.id;selected.team_evaluation=job.result.run;await render();return;}
  if(!selected.team_evaluation&&(catalog.runs.team_evaluation||[]).length){await render();return;}
  if($("#teamevalrun"))$("#teamevalrun").innerHTML=evalRunOptions();
  if(selected.team_evaluation){
   const data=await api(`/api/run?kind=team_evaluation&id=${encodeURIComponent(selected.team_evaluation)}`);if(ticket!==generation||tab!=="team_evaluation")return;
   const signature=JSON.stringify(data);if(signature!==teamEvalSignature){teamEvalData=data;teamEvalSignature=signature;paintTeamEvaluation(data);}
  }
  updateTeamEvalJob();
 }catch(e){if(ticket===generation)notice("Evaluation refresh: "+e.message+" The last readable snapshot stays visible.");}
 if(ticket===generation&&tab==="team_evaluation")teamEvalTimer=setTimeout(()=>pollTeamEvaluation(ticket),2000);
}
async function teamEvaluation(ticket){
 choose("team_evaluation");teamEvalData=null;teamEvalSignature="";teamEvalCaseData=null;
 $("#content").innerHTML=title("Frozen inputs · independent scoring","Evaluate the three-agent workflow.","Separate task success, recorded policy compliance, preservation, cleanup, availability and model use. Keep failures and unrun cases visible.","Versioned regression set")+`<div class="evalheader"><label for="teamevalrun" class="small">Saved campaign<select id="teamevalrun">${evalRunOptions()||'<option value="">No campaigns yet</option>'}</select></label><button id="startevaluation">Free 6-case evaluation</button></div><p class="small">The browser runs only the scripted development split. Live evaluation and the reserved confirmation split start explicitly in your terminal. Read-only mode disables launches.</p><div id="teamevaljob" class="evalstatus" role="status" aria-live="polite"></div><div id="teamevalbody">${selected.team_evaluation?'<div class="loading">Reading evaluation evidence…</div>':'<div class="empty"><h2>Run the frozen development set</h2><p>Six cases cover routing and dependency conflicts at low, middle and high capacity values. Scripted replies use real local workers and zero model calls.</p></div>'}</div>`;
 if(selected.team_evaluation){try{const data=await api(`/api/run?kind=team_evaluation&id=${encodeURIComponent(selected.team_evaluation)}`);if(ticket!==generation)return;teamEvalData=data;teamEvalSignature=JSON.stringify(data);paintTeamEvaluation(data);}catch(e){if(ticket===generation)$("#teamevalbody").innerHTML=`<div class="callout warning">${esc(e.message)} Waiting for a complete result snapshot…</div>`;}}
 updateTeamEvalJob();if(ticket===generation&&tab==="team_evaluation")teamEvalTimer=setTimeout(()=>pollTeamEvaluation(ticket),2000);
}
async function inspectTeamEvalCase(id){
 stopPlay();activeReplay=null;const ticket=generation,run=selected.team_evaluation,caseTicket=++teamEvalCaseGeneration;
 $("#teamevalcase").innerHTML='<div class="loading">Checking saved case evidence…</div>';
 try{
  const data=await api(`/api/team-evaluation-case?run=${encodeURIComponent(run)}&id=${encodeURIComponent(id)}`);
  if(ticket!==generation||tab!=="team_evaluation"||run!==selected.team_evaluation||caseTicket!==teamEvalCaseGeneration)return;
  teamEvalCaseData=data;const x=data.row.score;
  $("#teamevalcase").innerHTML=`<div class="card evalcase"><div class="panelhead"><h2>${esc(data.case_id)}</h2><button data-eval-team-case-export>Export case</button></div><div class="evalscore">${badge(x.safety,x.safety==="SAFE"?"good":"warn")}${badge(x.task_completed?"Task completed":"Task incomplete",x.task_completed?"good":"warn")}</div>${x.error?`<div class="callout warning">${esc(x.error)}</div>`:""}${evidence("Independent policy checks",x.checks)}${evidence("Evidence integrity checks",x.evidence_checks)}${evidence("Recorded role decisions",data.case_summary?.decisions||{})}<div id="teamevalreplay"></div></div>`;
  if(data.replay)renderReplay(data.replay,"#teamevalreplay");else $("#teamevalreplay").innerHTML='<p class="small">No complete service outcome was saved for this case. It cannot count as a completed task.</p>';
 }catch(e){if(ticket===generation&&caseTicket===teamEvalCaseGeneration)$("#teamevalcase").innerHTML=`<div class="callout warning">${esc(e.message)}</div>`;}
}
document.addEventListener("click",async e=>{
 const b=e.target.closest("button");if(!b||tab!=="team_evaluation")return;
 if(b.id==="startevaluation"){
  if(b.disabled||teamEvalSubmitting)return;teamEvalSubmitting=true;updateTeamEvalJob();notice("");
  try{const data=await api("/api/jobs",{method:"POST",headers:{"Content-Type":"application/json","X-Rehearsal-Token":LAB_TOKEN},body:JSON.stringify({kind:"team_evaluation",split:"development"})});catalog.lab.job=data.job;}
  catch(err){notice(err.message);}finally{teamEvalSubmitting=false;updateTeamEvalJob();}
 }else if(b.dataset.evalTeamCase)await inspectTeamEvalCase(b.dataset.evalTeamCase);
 else if(b.hasAttribute("data-eval-team-export")&&teamEvalData)download(teamEvalData,"orkestryn-team-evaluation.json");
 else if(b.hasAttribute("data-eval-team-case-export")&&teamEvalCaseData)download(teamEvalCaseData,"orkestryn-team-evaluation-case.json");
});
document.addEventListener("change",async e=>{if(e.target.id==="teamevalrun"){selected.team_evaluation=e.target.value;await render();}});
'''
