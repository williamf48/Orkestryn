"""Optional Node minimal-DOM checks; no browser, SDK or real model calls."""
from pathlib import Path
import argparse
import json
import os
import shutil
import subprocess
import tempfile

SCRIPT=r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const fixture=JSON.parse(fs.readFileSync(process.env.ORK_SAFETY_FIXTURE,'utf8'));
const nodes=new Map(),handlers={},posts=[],gets=[],checks=[];let rule=null;
function node(key){if(!nodes.has(key))nodes.set(key,{id:key.slice(1),dataset:{},attrs:{},setAttribute(k,v){this.attrs[k]=v;},removeAttribute(k){delete this.attrs[k];},value:'',innerHTML:'',textContent:'',hidden:false,disabled:false,checked:false,open:false,href:'',listeners:{},showModal(){this.open=true;},close(){this.open=false;},addEventListener(k,f){this.listeners[k]=f;}});return nodes.get(key);}
const views=['comparison','cases','models','results','methods'];const panes=views.map(v=>{const n=node('#page-'+v);n.dataset.labPage=v;return n;}),navs=views.map(v=>{const n=node('#nav-'+v);n.dataset.labNav=v;return n;}),opsLinks=['overview','workflows','approvals'].map(v=>{const n=node('#ops-'+v);n.dataset.opsView=v;return n;});
function reply(value,status=200){return {ok:status<400,status,json:async()=>structuredClone(value)};}
const ctx={console,structuredClone,URLSearchParams,location:{hash:'#workspace=sample-workflow'},document:{querySelector:node,querySelectorAll:s=>s==='[data-lab-page]'?panes:s==='[data-lab-nav]'?navs:s==='[data-ops-view]'?opsLinks:[],addEventListener:(k,f)=>handlers[k]=f},fetch:async(path,opt)=>{
 const body=opt?.body?JSON.parse(opt.body):null;if(body)posts.push({path,body});else gets.push(path);
 if(rule){const value=await rule(path,body);if(value)return value;}
 if(path==='/api/safety'&&!body)return reply(fixture.state);
 if(path.startsWith('/api/safety/case?'))return reply({public_context:fixture.view});
 if(path.startsWith('/api/safety/report?'))return reply(fixture.report);
 if(body?.action==='evaluate'){fixture.state.runs=[{id:fixture.report.id,created:fixture.report.created_at,config:fixture.report.config,case_count:fixture.report.results.cases.length}];return reply(fixture.report);}
 if(body?.action==='paid-review')return reply({digest:'frozen-request',request:{model:'Nemotron',endpoint:'https://api.tokenfactory.nebius.com/v1/chat/completions',settings:{max_tokens:1024},context:fixture.view},scope:'One <untrusted> request'});
 if(body?.action==='import-candidate')return reply({case_id:fixture.state.catalog[0].id});
 return reply({status:'REPLIED',metadata:{total_tokens:42}});
}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.env.ORK_SAFETY_JS,'utf8'),ctx);
const run=s=>vm.runInContext(s,ctx),idle=async()=>{for(let i=0;i<15;i++)await Promise.resolve();};
async function click(id){const b=node('#'+id);b.closest=()=>b;await handlers.click({target:b});}
function ok(name,test){test();checks.push(name);}
(async()=>{
 await idle();node('#case').value=fixture.state.catalog[0].id;node('#split').value='development';node('#output_cap').value='1024';node('#visibility').value='full_context';
 ok('Startup only loads authenticated research state and makes no model POST',()=>{assert.equal(posts.length,0);assert(gets.every(v=>v==='/api/safety'));});
 ok('Account name is rendered with textContent',()=>assert.equal(node('#account').textContent,fixture.state.principal.name));
 await click('evaluate');
 ok('Free evaluation is distinct from paid requests',()=>{assert.equal(posts[0].body.action,'evaluate');assert(!posts[0].body.nebius_api_key);assert.equal(posts[0].body.split,'development');});
 ok('The result labels action and model-token units separately',()=>assert(node('#results').innerHTML.includes('inspected actions')));
 ok('Result links point to exact authenticated report and ZIP routes',()=>{assert(node('#reportlink').href.includes('/safety/report?run=s-'));assert(node('#exportlink').href.includes('/api/safety/evidence?run=s-'));});
 ok('Missing rates render as unmeasured rather than perfect safety',()=>assert(node('#results').innerHTML.includes('Not measured')));
 await click('inspect');ok('Monitor input uses textContent rather than HTML',()=>assert.equal(node('#context').textContent,JSON.stringify(fixture.view,null,2)));
 const before=posts.length;await click('monitorreview');
 ok('Paid preview sends no key or paid attempt',()=>{assert.equal(posts.length,before+1);assert.equal(posts.at(-1).body.action,'paid-review');assert(!posts.at(-1).body.nebius_api_key);});
 ok('Preview is escaped and consent starts unchecked',()=>{assert(node('#paid').open);assert(node('#paidbody').innerHTML.includes('&lt;untrusted&gt;'));assert.equal(node('#consent').checked,false);});
 node('#research_key').value='LOCAL_PRIVATE_KEY_SENTINEL';await click('confirm');
 ok('Unchecked consent cannot send a paid request',()=>assert.equal(posts.length,before+1));
 node('#consent').checked=true;let resolvePaid;
 rule=async(path,body)=>body?.action==='paid-attempt'?await new Promise(resolve=>resolvePaid=resolve):null;
 const first=click('confirm');await idle();
 ok('Key clears before the paid request finishes',()=>assert.equal(node('#research_key').value,''));
 ok('Frozen case, cap, fingerprint and explicit consent bind the one request',()=>{const b=posts.at(-1).body;assert.equal(b.action,'paid-attempt');assert.equal(b.review_digest,'frozen-request');assert.equal(b.output_cap,1024);assert.equal(b.confirmed,true);assert.equal(b.nebius_api_key,'LOCAL_PRIVATE_KEY_SENTINEL');});
 const at=posts.length;await click('confirm');ok('Double confirmation does not create another attempt',()=>assert.equal(posts.length,at));
 resolvePaid(reply({status:'REPLIED'}));await first;rule=null;
 ok('A completed request does not chain a candidate, monitor or service change',()=>assert.equal(posts.length,at));
 await click('policyreview');node('#research_key').value='CANCEL_SENTINEL';await click('cancel');
 ok('Cancel removes the key and pending review',()=>{assert.equal(node('#research_key').value,'');assert.equal(run('pending'),null);assert(!node('#paid').open);});
 await click('monitorreview');node('#research_key').value='ESCAPE_SENTINEL';node('#paid').listeners.cancel();
 ok('Dialog Escape removes the key and pending review',()=>{assert.equal(node('#research_key').value,'');assert.equal(run('pending'),null);});
 await click('monitorreview');node('#consent').checked=true;node('#research_key').value='NETWORK_SENTINEL';
 rule=async(path,body)=>{if(body?.action==='paid-attempt')throw Error('network loss');return null;};
 const old=posts.length;await click('confirm');rule=null;
 ok('Connection loss records one browser attempt and never retries',()=>assert.equal(posts.length,old+1));
 ok('Unconfirmed result instructs inspection rather than claiming success',()=>assert(node('#notice').innerHTML.includes('Refresh the saved attempt journal')));
 ok('The key is cleared on a failed request',()=>assert.equal(node('#research_key').value,''));
 fixture.state.principal.roles=['viewer'];await click('refresh');
 ok('Viewer controls cannot evaluate import or authorize calls',()=>assert(['evaluate','evaluatecase','importcandidate','policyreview','monitorreview'].every(id=>node('#'+id).disabled)));
 fixture.state.principal.roles=['manager','viewer'];await click('refresh');
 node('#candidate').value='not json';const beforeImport=posts.length;await click('importcandidate');
 ok('Invalid imported JSON makes no server operation',()=>assert.equal(posts.length,beforeImport));
 node('#candidate').value=JSON.stringify({summary:'fixture',actions:[{actor:'deployment',tool:'observe',args:{},note:''}]});node('#model_label').value='self reported';await click('importcandidate');
 ok('Import is explicitly separate from provider-generated provenance',()=>{assert.equal(posts.at(-1).body.action,'import-candidate');assert(node('#notice').innerHTML.includes('self-reported'));});
 node('#framing').checked=true;await click('evaluate');
 ok('Paired framing is an explicit free evaluation option',()=>{assert.equal(posts.at(-1).body.framing,true);assert(!posts.at(-1).body.nebius_api_key);});
 node('#visibility').value='actions_only';await click('monitorreview');
 ok('Monitor visibility is frozen into the reviewed request',()=>{assert.equal(posts.at(-1).body.visibility,'actions_only');assert.equal(run('pending.visibility'),'actions_only');});
 await click('cancel');await click('policyreview');
 ok('Policy generation does not inherit the monitor visibility ablation',()=>assert.equal(posts.at(-1).body.visibility,'full_context'));
 await click('cancel');fixture.state.model_requests_enabled=false;await click('refresh');
 ok('Owner-disabled paid requests stay disabled while free comparisons remain enabled',()=>{assert(node('#policyreview').disabled);assert(node('#monitorreview').disabled);assert(!node('#evaluate').disabled);});
 fixture.state.model_requests_enabled=true;
 rule=async(path,body)=>!body?reply({error:'<img src=x onerror=alert(1)>'},401):null;
 node('#research_key').value='EXPIRED_SENTINEL';await click('refresh');rule=null;
 ok('Session denial clears keys and closes a pending review',()=>{assert.equal(node('#research_key').value,'');assert.equal(run('pending'),null);assert(!node('#paid').open);});
 ok('Server errors are escaped in notices',()=>{assert(node('#notice').innerHTML.includes('&lt;img'));assert(!node('#notice').innerHTML.includes('<img'));});
 const code=fs.readFileSync(process.env.ORK_SAFETY_JS,'utf8');
 ok('No browser storage or automatic timer starts paid requests',()=>{assert(!/localStorage|sessionStorage|setInterval/.test(code));});
 const calls=posts.length+gets.length;await click('nav-cases');
 ok('Section navigation does not send a server operation',()=>assert.equal(posts.length+gets.length,calls));
 ok('Only the selected Safety Lab section remains visible',()=>{assert.equal(panes.filter(n=>!n.hidden).length,1);assert(!node('#page-cases').hidden);});
 ok('Safety Lab tabs expose the current section to assistive tools',()=>{assert.equal(node('#nav-cases').attrs['aria-current'],'page');assert(!node('#nav-models').attrs['aria-current']);});
 run("pending={digest:'fixture'}");node('#research_key').value='NAVIGATION_SENTINEL';node('#paid').showModal();await click('nav-models');
 ok('Changing a research section cancels unsent consent and clears its key',()=>{assert.equal(run('pending'),null);assert.equal(node('#research_key').value,'');assert(!node('#paid').open);});
 ok('Operations section links retain only the selected workflow identifier',()=>assert.equal(node('#ops-approvals').href,'/#view=approvals&run=sample-workflow'));
 run('showResult('+JSON.stringify(fixture.report)+')');
 ok('Completed evaluations open Results and exports directly',()=>{assert.equal(run('labView'),'results');assert(!node('#page-results').hidden);assert.equal(panes.filter(n=>!n.hidden).length,1);});
 ok('User administration is hidden for a research viewer',()=>assert(node('#safetyusers').hidden));
 fixture.state.attempts=[{status:'FAILED_OR_UNCONFIRMED',kind:'policy',case_id:'c-fixture',output_cap:2048,id:'a-fixture',metadata:{finish_reason:'stop',total_tokens:924},diagnostics:{code:'REPLY_CONTRACT_INVALID',visible_reply:'</pre><script>bad()</script>'}}];await click('refresh');
 ok('Saved diagnostics distinguish response rejection and escape visible model output',()=>{const h=node('#attempts').innerHTML;assert(h.includes('REPLY_CONTRACT_INVALID'));assert(h.includes('&lt;script&gt;'));assert(!h.includes('<script>'));assert(h.includes('finish_reason'));});
 fixture.state.attempts[0].diagnostics={code:'LEGACY_DETAILS_UNAVAILABLE'};await click('refresh');
 ok('Earlier failures stay counted without invented diagnostic details',()=>assert(node('#attempts').innerHTML.includes('detailed diagnostics were not retained')));
 await click('policyreview');node('#consent').checked=true;node('#research_key').value='LAST_PRIVATE_KEY_SENTINEL';
 rule=async(path,body)=>body?.action==='paid-attempt'?reply({status:'FAILED_OR_UNCONFIRMED',diagnostics:{code:'REPLY_JSON_INVALID'}}):null;
 const diagnosticCalls=posts.length;await click('confirm');rule=null;
 ok('A failed paid reply shows the saved category without retrying or keeping its key',()=>{assert.equal(posts.length,diagnosticCalls+1);assert(node('#notice').innerHTML.includes('not one valid JSON document'));assert.equal(node('#research_key').value,'');});
 console.log(JSON.stringify({status:'PASSED',checks_passed:checks.length,checks,scope:'Node minimal DOM and mocked HTTP only; not a rendered-browser or real provider test.'}));
})().catch(e=>{console.error(e);process.exitCode=1;});'''


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args(argv)
    node=shutil.which('node')
    if not node:raise ValueError('Install Node.js to run these optional interface checks.')
    import safety_ui,safety_lab
    with tempfile.TemporaryDirectory(prefix='safety UI ') as temporary:
        root=Path(temporary)
        with safety_lab.Lab(root) as lab:
            state=lab.state();state.update(principal={'name':'Research <user>','roles':['manager','viewer']},csrf='fixture-csrf',model_requests_enabled=True)
            record=lab.evaluate(case_id=state['catalog'][0]['id'],budgets=[1,8])
            case=lab.case(state['catalog'][0]['id'])
        import safety_environment as env
        (root/'fixture.json').write_text(json.dumps({'state':state,'report':record,'view':env.public_context(case)}))
        (root/'interface.js').write_text(safety_ui.PAGE.split('<script>')[1].split('</script>')[0])
        (root/'checks.cjs').write_text(SCRIPT)
        environment=os.environ.copy();environment.update(ORK_SAFETY_JS=str(root/'interface.js'),ORK_SAFETY_FIXTURE=str(root/'fixture.json'))
        result=subprocess.run([node,str(root/'checks.cjs')],env=environment,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=60)
        if result.returncode:print(result.stdout);return result.returncode
        record=json.loads(result.stdout);args.output.write_text(json.dumps(record,indent=2)+'\n');print(result.stdout.strip());return 0


if __name__=='__main__':raise SystemExit(main())
