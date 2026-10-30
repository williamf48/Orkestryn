"""Inline, dependency-free UI for dashboard.py's local team workspace."""

TEAM_STYLE = r'''
.teamroles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:15px;margin:20px 0}
.teamroles .card{border-top:2px solid var(--teal)}.teamroles .role9{border-top-color:var(--blue)}.teamroles .role12{border-top-color:var(--amber)}
.teamroles h2{margin:8px 0}.teamroles .reason{min-height:45px;margin:12px 0;color:var(--muted);font-size:12px}
.teamflow{font-size:22px;letter-spacing:-.5px;margin:12px 0;font-variant-numeric:tabular-nums}
.teamselect{flex:1;min-width:220px;max-width:600px}.teamselect select{width:100%;min-width:0}
.teamlabel{display:block;font-size:11px;color:var(--muted);margin-bottom:5px}.teamsection{margin-top:22px}
.teamconsent{display:flex;align-items:flex-start;gap:9px;margin:15px 0;font-size:12px;color:var(--muted)}
.teamconsent input{accent-color:var(--teal);margin-top:4px}.teamreview-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:15px}
.team-history{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:14px 0}.team-history select{max-width:100%;flex:1;min-width:210px}
.teamstatus{min-height:24px;margin:12px 0}.team-details{margin-top:20px}.team-details>summary{font-size:14px;padding:15px 0}
.team-note{width:100%;background:var(--surface2);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:12px;font:inherit}
.teamroute{display:flex;gap:18px;align-items:center;flex-wrap:wrap}.teamroute strong{color:var(--text)}
@media(max-width:1050px){.teamroles{grid-template-columns:1fr}.teamroles .reason{min-height:0}}
'''

TEAM_SCRIPT = r'''
let teamTimer=null,teamData=null,teamBranchData=null,teamSignature="",teamSubmitting=false,teamHandledJob=null;
const teamDecisionLabel={retain:"Keep C's work",withdraw_route:"Withdraw C's route",release_dependency:"Release C's dependency"};
const teamBusy=()=>teamSubmitting||["QUEUED","RUNNING"].includes(catalog?.lab?.job?.status);
const teamEnabled=()=>catalog?.lab?.enabled&&!teamBusy();
function teamRunOptions(){return (catalog.runs.team||[]).map(r=>`<option value="${esc(r.id)}" ${r.id===selected.team?"selected":""}>${esc(stamp(r.id))} · ${esc(human(r.summary.scenario))} · ${live(r)?"live Nemotron":"free scripted"} · ${esc(human(r.summary.status))}</option>`).join("");}
function teamRole(role,part,name,description,data){
 const call=(data.summary.calls||[]).find(c=>c.role===role),decision=data.summary.decisions?.[role];
 const tokens=call?.metadata?.total_tokens;
 return `<article class="card role${part}"><div class="eyebrow">Problem ${part}</div><h2>${name}</h2>${badge(call?human(call.status):"Not requested",call?.status==="ERROR"?"warn":"info")}<p class="reason">${description}</p><p class="small">${esc(decision?.reason||"No validated decision recorded yet.")}</p><div class="divider"></div><span class="small">${call?.live?`${typeof tokens==="number"?fmt(tokens):"Unreported"} tokens · paid request`:call?"Scripted response · zero model calls":"Waiting for the coordinator"}</span>${decision?evidence("Validated role decision",decision):""}</article>`;
}
function teamCapacity(data){
 const s=data.service||{},m=data.metrics,b=data.before_metrics,c=s.state?.deployments?.["candidate-v2"],max=Math.max(m.limit||0,m.committed||0,1),pct=n=>Math.max(0,Math.min(100,n/max*100));
 return `<div class="card"><h2>Capacity and shared work</h2><p class="small">Recorded commitments before the revision, after execution, and at the policy outcome.</p><div class="teamflow">${esc(fmt(b.limit==null?null:b.committed))} → ${esc(fmt(s.after_execution_units))} → ${esc(fmt(m.limit==null?null:m.committed))}<span class="small"> / ${esc(fmt(m.limit))} units</span></div><div class="meter" aria-label="Saved capacity allocation"><span class="stable" style="width:${pct(m.stable)}%"></span><span class="allocated" style="width:${pct(m.allocated)}%"></span><span class="pending" style="width:${pct(m.pending)}%"></span></div><div class="legend"><span><i class="swatch stable"></i>Stable ${esc(fmt(m.stable))}</span><span><i class="swatch allocated"></i>Candidate ${esc(fmt(m.allocated))}</span><span><i class="swatch pending"></i>Pending ${esc(fmt(m.pending))}</span></div><div class="divider"></div><h3>Traffic at saved outcome</h3><div class="teamroute"><span>Stable <strong>${esc(fmt(m.routing?.["stable-v1"]))}%</strong></span><span>Candidate <strong>${esc(fmt(m.routing?.["candidate-v2"]))}%</strong></span><span class="small">Owner: ${esc(m.route_owner||"Not recorded")} · version ${esc(fmt(m.route_version))}</span></div><p class="small readmore">Candidate dependencies: ${esc((c?.dependencies||[]).map(d=>`${d.id} (${d.owner_run})`).join(", ")||"None recorded")}</p></div>`;
}
function teamApproval(data){
 const s=data.service||{},consent=[...(data.handoffs||[])].reverse().find(e=>e.kind==="exact_consent_recorded"),waiting=data.summary.status==="AWAITING_EXACT_MANAGER_CONSENT",proposal=s.proposal||data.summary.decisions?.governance,probe=s.approval_probe;
 const label=waiting?"Waiting for exact terminal consent":consent?.payload?.approved===true?"Exact consent recorded":consent?"Consent declined":"No consent recorded";
 return `<div class="card"><h2>Approval and execution</h2>${badge(label,waiting?"warn":consent?.payload?.approved?"good":"info")}<p class="small readmore">${waiting?`Review the exact ${esc(fmt(proposal?.additional_units))}-unit proposal in the live terminal. Enter the requested phrase there to continue.`:"The model proposes; the coordinator checks the state and applies only the separately approved action."}</p><div class="divider"></div><div class="row"><span class="small">Isolated revision rehearsal</span>${badge(s.rehearsal?s.rehearsal.passed?"Passed":"Failed":"Not recorded",s.rehearsal?.passed?"good":"")}</div><div class="row readmore"><span class="small">Probe after review</span><strong>${probe?esc(fmt(probe.samples?.length))+" HTTP samples":"Not recorded"}</strong></div><div class="row readmore"><span class="small">Execution</span>${badge(s.execution?.status||"Not recorded",s.execution?.status==="SUCCEEDED"?"good":"")}</div><div class="row readmore"><span class="small">Workflow deploy-A</span>${badge(s.state?.paused_runs?.includes("deploy-A")?"Paused":"No pause recorded","info")}</div>${evidence("Exact proposal, grant and probe summary",{proposal,approval:s.approval,probe:data.summary.approval_revalidation})}</div>`;
}
function paintTeam(data){
 stopPlay();activeReplay=null;
 const s=data.summary,r=data.service||{},o=s.outcome||{},usage=s.usage||{},status=r.recovery?.status,closed=r.teardown?.all_workers_stopped;
 $("#teamlive").innerHTML=`<div class="row"><div class="pillrow">${badge(s.mode||"Saved team run","info")}${badge(data.active?human(s.status):status==="RECOVERY_REVIEW_REQUIRED"?"Run finished · owner review required":human(s.status),data.active||status==="RECOVERY_REVIEW_REQUIRED"?"warn":"info")}</div><button data-team-export>Export team view</button></div><p class="small readmore">${esc(data.id)} · ${esc(data.active?"Showing saved snapshots; a separate terminal owns the active run.":"Saved outcome; the workflow is not resumed by this view.")}</p>${s.error?`<div class="callout warning">${esc(s.error)}</div>`:""}${data.warnings.length?`<div class="callout warning">${data.warnings.map(esc).join(" ")}</div>`:""}<div class="teamroles">${teamRole("factory",6,"Scenario factory","Creates bounded capacity and conflict parameters. Its validated case becomes the governance input.",data)}${teamRole("governance",9,"Governance","Proposes a smaller change, rehearsed before exact consent. It cannot approve its own request.",data)}${teamRole("recovery",12,"Recovery","Requests bounded cleanup after confirmed execution and a durable workflow pause.",data)}</div><div class="grid g4">${metric("Paid role requests",fmt(s.model_calls),`${s.role_requests??0} / 3 total role requests`)}${metric("Reported tokens",fmt(usage.reported_tokens),`${usage.missing_usage_calls??0} paid calls without usage`)}${metric("Saved HTTP availability",ratio(r.availability?.successful,r.availability?.requests),"Local arithmetic requests before teardown")}${metric("Recovery result",status==="RECOVERY_REVIEW_REQUIRED"?"Needs owner":status==="VERIFIED_RECOVERED"?"Recovered":"Pending",status?human(status):"No recovery outcome yet")}</div>${closed?'<div class="callout">The runner stopped its workers after capturing this outcome. Capacity and traffic below describe that saved moment. Owner decisions are simulated in separate snapshot branches.</div>':data.active?'<div class="callout">This view polls saved evidence every two seconds. Keep the live terminal open for any exact manager consent. Dashboard launch buttons run free scripted cases.</div>':'<div class="callout warning">No complete worker teardown record is available. Inspect the saved run before making a recovery claim.</div>'}<div class="grid g2">${teamCapacity(data)}${teamApproval(data)}</div><div class="card"><h2>Why recovery stopped</h2>${(r.recovery?.issues||[]).length?`<ul class="checks">${r.recovery.issues.map(issue=>`<li><span class="checkfail">!</span><span><strong>${esc(issue.code||issue.status)}</strong><br>${esc(issue.reason)}</span></li>`).join("")}</ul>`:'<p class="small">No unresolved recovery issue is recorded in this snapshot.</p>'}${evidence("Recorded cleanup steps",r.recovery?.steps||[])}</div><details class="team-details"><summary>Agent handoffs and checks (${data.handoffs.length} handoffs)</summary><div class="card tablewrap"><table><thead><tr><th>#</th><th>From → to</th><th>Handoff</th><th>Evidence</th></tr></thead><tbody>${data.handoffs.map(h=>`<tr><td>${esc(h.sequence)}</td><td>${esc(h.sender)} → ${esc(h.recipient)}</td><td>${esc(human(h.kind))}</td><td>${evidence("Recorded payload",h.payload)}</td></tr>`).join("")}</tbody></table>${evidence("Saved team checks",s.checks||{})}</div></details><details class="team-details" id="teamreplaydetails"><summary>Execution journal, capacity and traffic replay</summary><div id="teamreplay"></div></details>`;
 $("#teamreplaydetails").addEventListener("toggle",()=>{if($("#teamreplaydetails")?.open&&!$("#teamreplay").hasChildNodes())renderReplay(data,"#teamreplay");});
}
function teamBranches(){
 const rows=(catalog.runs.team_review||[]).filter(r=>r.summary.root_team_run===selected.team);
 if(!rows.some(r=>r.id===selected.team_review))selected.team_review="";
 $("#teamhistory").innerHTML=`<h2>Owner review rehearsals</h2><p class="small">Each decision saves a new simulation branch linked to the source digest. The original run remains available above.</p><div class="team-history"><label for="teambranch" class="small">Review source</label><select id="teambranch"><option value="">Original team outcome</option>${rows.map(r=>`<option value="${esc(r.id)}" ${selected.team_review===r.id?"selected":""}>${esc(stamp(r.id))} · ${esc(human(r.summary.recovery_outcome))}</option>`).join("")}</select><span class="small">${rows.length} saved ${rows.length===1?"branch":"branches"}</span></div><div id="teambranchbody"></div><div id="teamreviewform"></div>`;
}
function teamComparison(branch){
 const r=branch.report,b=r.source_state,a=r.final_state,m=branch.saved_final_metrics;
 return `<div class="callout warning"><strong>Simulated snapshot rehearsal.</strong> New route, stop and release records describe simulated policy effects. This branch made zero model calls, started no workers and collected no new HTTP evidence.</div><div class="card"><div class="panelhead"><h2>${esc(teamDecisionLabel[r.decision.choice]||human(r.decision.choice))}</h2>${badge(r.decision.result.status,r.decision.result.status==="BLOCKED"?"warn":"info")}</div><p class="small">${esc(r.decision.note)}</p><p>${esc(r.decision.result.reason)}</p><div class="tablewrap"><table><thead><tr><th>Snapshot value</th><th>Before decision</th><th>After simulated cleanup</th></tr></thead><tbody>${[["Committed units",b.stable_units+Object.values(b.deployments).reduce((n,d)=>n+d.allocated_units,0)+Object.values(b.reservations).filter(x=>x.status==="PENDING").reduce((n,x)=>n+x.units,0),m.committed],["Capacity limit",b.capacity_limit,a.capacity_limit],["Candidate traffic",`${b.routing["candidate-v2"]}%`,`${a.routing["candidate-v2"]}%`],["Route owner",b.route_owner,a.route_owner],["Candidate allocation",b.deployments["candidate-v2"].allocated_units,a.deployments["candidate-v2"].allocated_units],["Dependencies",b.deployments["candidate-v2"].dependencies.map(d=>`${d.id} (${d.owner_run})`).join(", ")||"None",a.deployments["candidate-v2"].dependencies.map(d=>`${d.id} (${d.owner_run})`).join(", ")||"None"]].map(row=>`<tr>${row.map(cell=>`<td>${esc(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div><p class="small readmore">Simulated recovery: ${esc(human(r.recovery_outcome))}. Workflow deploy-A remains paused.</p>${evidence("Parent, decision and preservation checks",{parent:r.parent,decision:r.decision,assertions:r.assertions,scope:r.scope})}<button class="linkbutton" data-team-branch-export>Export this simulated branch →</button></div>`;
}
function paintTeamReview(){
 const data=teamBranchData||teamData,choices=data?.review?.available_decisions||[];
 if(!data)return;
 const can=teamEnabled()&&catalog.lab?.available?.team_review;
 $("#teamreviewform").innerHTML=`<div class="card teamsection"><h2>Record C's decision in a rehearsal</h2><p class="small">Source: ${esc(data.id)} · ${teamBranchData?"simulated branch":"saved team outcome"}.</p><p class="small">${esc(data.review.reason)}</p>${choices.length?`<label class="teamlabel" for="teamreason">Reason for this simulated owner decision</label><textarea id="teamreason" class="team-note" rows="2" maxlength="300" placeholder="Explain whether C still needs this route or dependency."></textarea><label class="teamconsent"><input id="teamconfirm" type="checkbox"><span>I understand this creates a simulated snapshot branch. It does not change a live service or authenticate operator C.</span></label><div class="teamreview-actions">${choices.map(choice=>`<button class="team-action" data-team-decision="${choice}" ${can?"":"disabled"}>${esc(teamDecisionLabel[choice])}</button>`).join("")}</div>`:'<div class="callout">No owner decision is currently available for this snapshot.</div>'}${!catalog.lab?.enabled?'<p class="small readmore">Read-only mode: launch and decision controls are disabled.</p>':""}</div>`;
}
function updateTeamJob(){
 const target=$("#teamjob");if(!target)return;const j=catalog?.lab?.job;
 target.innerHTML=j?`<div class="callout ${["FAILED","STOPPED"].includes(j.status)?"warning":""}"><strong>${esc(human(j.status))}</strong> · ${esc(human(j.kind))}${j.elapsed_seconds!=null?` · ${esc(j.elapsed_seconds)} s`:""}${j.error?`<p class="readmore">${esc(j.error)}</p>`:""}${j.log?evidence("Runner log",j.log):""}</div>`:"";
 document.querySelectorAll(".team-action").forEach(b=>{b.disabled=!teamEnabled()||!catalog.lab?.available?.[b.dataset.teamStart?"team":"team_review"];});
}
async function loadTeamBranch(ticket){
 teamBranchData=null;
 if(selected.team_review){
  const data=await api(`/api/run?kind=team_review&id=${encodeURIComponent(selected.team_review)}`);
  if(ticket!==generation||tab!=="team")return;
  if(data.report.root_team_run!==selected.team)throw new Error("This review branch belongs to another team run.");
  teamBranchData=data;$("#teambranchbody").innerHTML=teamComparison(data);
 }
 paintTeamReview();updateTeamJob();
}
async function pollTeam(ticket){
 if(ticket!==generation||tab!=="team")return;
 try{
  const next=await api("/api/catalog");if(ticket!==generation||tab!=="team")return;catalog=next;
  const job=catalog.lab?.job;
  if(job?.status==="COMPLETED"&&["team","team_review"].includes(job.kind)&&teamHandledJob!==job.id){
   teamHandledJob=job.id;
   if(job.kind==="team"){selected.team=job.result.run;selected.team_review="";}
   else{selected.team=job.result.root_team_run;selected.team_review=job.result.run;}
   await render();return;
  }
  const select=$("#teamrun");if(select)select.innerHTML=teamRunOptions();
  if(!selected.team&&(catalog.runs.team||[]).length){await render();return;}
  if(selected.team){
   const data=await api(`/api/run?kind=team&id=${encodeURIComponent(selected.team)}`);
   if(ticket!==generation||tab!=="team")return;
   const signature=JSON.stringify(data);
   if(signature!==teamSignature){teamData=data;teamSignature=signature;paintTeam(data);teamBranches();await loadTeamBranch(ticket);}
  }
  updateTeamJob();
 }catch(e){if(ticket===generation)notice("Workspace refresh: "+e.message+" The last readable snapshot stays visible.");}
 if(ticket===generation&&tab==="team")teamTimer=setTimeout(()=>pollTeam(ticket),2000);
}
async function teamWorkspace(ticket){
 teamData=null;teamBranchData=null;teamSignature="";choose("team");
 $("#content").innerHTML=title("Factory 6 · Governance 9 · Recovery 12","Three agents. One shared service.","Follow the validated case, exact approval, and bounded recovery. Review shared work using a separate simulation branch.","Local team workspace")+`<div class="toolbar"><div class="teamselect"><label class="teamlabel" for="teamrun">Saved or active team run</label><select id="teamrun">${teamRunOptions()||'<option value="">No team runs yet</option>'}</select></div><button class="team-action" data-team-start="dependency" ${teamEnabled()&&catalog.lab?.available?.team?"":"disabled"}>Free dependency run</button><button class="team-action" data-team-start="routing" ${teamEnabled()&&catalog.lab?.available?.team?"":"disabled"}>Free routing run</button></div><p class="small">The free buttons use scripted role replies over real local workers and HTTP. Live Nemotron runs start in your terminal and appear here automatically.</p><div id="teamjob" class="teamstatus" role="status" aria-live="polite"></div><div id="teamlive">${selected.team?'<div class="loading">Reading team evidence…</div>':'<div class="empty"><h2>Start a free team rehearsal</h2><p>Choose a scenario above to create your first three-role run, or keep a live terminal run open and watch its saved evidence appear here.</p></div>'}</div><section id="teamhistory" class="teamsection"></section>`;
 if(selected.team){
  try{
   const data=await api(`/api/run?kind=team&id=${encodeURIComponent(selected.team)}`);if(ticket!==generation)return;
   teamData=data;teamSignature=JSON.stringify(data);paintTeam(data);teamBranches();await loadTeamBranch(ticket);
  }catch(e){if(ticket===generation)$("#teamlive").innerHTML=`<div class="callout warning">${esc(e.message)} Waiting for the next complete saved snapshot…</div>`;}
 }
 updateTeamJob();if(ticket===generation&&tab==="team")teamTimer=setTimeout(()=>pollTeam(ticket),2000);
}
async function startTeamJob(payload){
 if(!teamEnabled())return;
 teamSubmitting=true;const ticket=generation;notice("");updateTeamJob();
 try{const data=await api("/api/jobs",{method:"POST",headers:{"Content-Type":"application/json","X-Rehearsal-Token":LAB_TOKEN},body:JSON.stringify(payload)});catalog.lab.job=data.job;}
 catch(e){if(ticket===generation)notice(e.message);}
 finally{teamSubmitting=false;if(ticket===generation)updateTeamJob();}
}
document.addEventListener("click",async e=>{
 const button=e.target.closest("button");if(!button||tab!=="team")return;
 if(button.dataset.teamStart)await startTeamJob({kind:"team",scenario:button.dataset.teamStart});
 else if(button.dataset.teamDecision){
  const source=teamBranchData||teamData,note=$("#teamreason")?.value||"",confirmed=$("#teamconfirm")?.checked===true;
  if(!note.trim()||note.length>300){notice("Record a reason of 1 to 300 characters.");return;}
  if(!confirmed){notice("Confirm that this is a simulated snapshot review before recording C's decision.");return;}
  await startTeamJob({kind:"team_review",source_kind:source.kind,source_run:source.id,source_digest:source.review.source_digest,decision:button.dataset.teamDecision,note,confirm_snapshot:true});
 }else if(button.hasAttribute("data-team-export")&&teamData)download(teamData,"orkestryn-team-view.json");
 else if(button.hasAttribute("data-team-branch-export")&&teamBranchData)download(teamBranchData,"orkestryn-simulated-owner-review.json");
});
document.addEventListener("change",async e=>{
 if(e.target.id==="teamrun"){selected.team=e.target.value;selected.team_review="";await render();}
 else if(e.target.id==="teambranch"){selected.team_review=e.target.value;await render();}
});
'''
