#!/usr/bin/env python3
"""Record native acceptance and optionally bounded live evidence in one command.

Default: standard library only, zero model calls. --live adds the original
14-case evaluation (24 attempts maximum) and connected development (30 maximum).
Confirmation is a separate explicit selection; keys are entered locally hidden.
"""
from __future__ import annotations
import argparse
import hashlib
import http.client
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from getpass import getpass
from pathlib import Path
from zipfile import ZIP_DEFLATED,ZipFile

import agent_evaluation as original
import agent_team as team
import connected_evaluation as evaluation
import connected_team as connected
import durable_runtime as durable
import runtime_sessions as sessions
import service_agent as service
import simulator as sim
import team_workspace as workspace

ROOT=Path(__file__).resolve().parent
VERSION='agent-rehearsal-acceptance-v1'
PUBLIC_NAMES={'summary.json','results.json','report.json','team_summary.json','team_outcome.json','coordination.json',
    'challenge.json','source_baseline.json','factory_baseline.json','evaluation_set.json','case_definition.json',
    'completion.json','policy_snapshot.json','owner_outcome.json','score.json','request.json','response.json',
    'agent_calls.json','context.json','prompt.json','teardown.json','walkthrough.json','acceptance.json',
    'task_report.json','supervision.json','bundle_manifest.json','case_spec.json','index.html','verified_sources.json','connected_registration.json','reference.json','outcome.json','outcome_report.json','outcome_report.html'}
FORBIDDEN_PARTS={'runtime','workers','update_backups','.venv','library_helpers'}


def evidence_export(output,roots):
    """Whitelist public records only; never copy runtime control DBs or logs."""
    output=Path(output).absolute();members={}
    for index,root in enumerate(roots):
        root=Path(root).absolute()
        candidates=[root] if root.is_file() else root.rglob('*')
        for path in candidates:
            if not path.is_file() or path.is_symlink() or path.name not in PUBLIC_NAMES:continue
            relative=Path(path.name) if root.is_file() else path.relative_to(root)
            if any(part.startswith('.') or part in FORBIDDEN_PARTS for part in relative.parts):continue
            if path.resolve()!=path or path.stat().st_size>32*1024*1024:raise ValueError('Unsafe or oversized export record.')
            members['%02d-%s/'%(index,root.stem)+relative.as_posix()]=path
    with ZipFile(output,'w',ZIP_DEFLATED) as archive:
        for name,path in sorted(members.items()):archive.write(path,name)
    return {'file':output.name,'records':len(members),'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),
        'scope':'Public evidence whitelist. No runtime capability files, private runtime DBs, environment, credentials, service logs or original policy SQLite files.'}


def walkthrough(output):
    """Actually kill only Popen objects launched here, reattach and finish owners."""
    output=Path(output).absolute();output.mkdir(parents=True,exist_ok=True);rows=[]
    for family in ('routing','dependency'):
        path,summary=connected.run(output/'sessions',scenario=family,ttl=180)
        if not summary['full_three_role_flow_completed']:raise ValueError('Original connected team did not complete.')
        try:
            client=connected.Client(path);before=client.status();rawteam=connected.sha(path/'team_summary.json');rawoutcome=connected.sha(path/'team_outcome.json')
            retained=client.request('/review',connected.request_for(path,'retain'));kept=client.status()
            owner=durable.CHILDREN.get(before['supervisor_pid']);backend=durable.CHILDREN.get(before['actual_at_outcome']['supervisor_pid'])
            if owner is None or backend is None:raise ValueError('Cannot prove ownership of the test supervisors; no saved PID was terminated.')
            owner.kill();owner.wait(5);backend.kill();backend.wait(5)
            # HTTP routing is unavailable during supervisor downtime. Verify
            # independent worker service directly, without claiming zero outage.
            direct=[]
            for folder in (path/'runtime'/'workers').iterdir():
                facts=durable.observe(folder);direct.append(facts['healthy'] and facts['alive'])
            unavailable=False
            try:durable.Runtime(path/'runtime').infer()
            except (OSError,http.client.HTTPException):unavailable=True
            restarted,_=connected.restart_session(path)
            req=connected.request_for(path,'withdraw_route' if family=='routing' else 'release_dependency')
            result=connected.Client(path).request('/review',req);after=connected.Client(path).status()
            samples=[durable.Runtime(path/'runtime').infer() for _ in range(20)]
            checks={'three_roles_executed_before_review':summary['role_requests']==3,
                'retain_has_no_policy_effect':retained['status']=='SUCCEEDED' and kept['policy_snapshot']['state']==before['policy_snapshot']['state'],
                'retain_has_no_runtime_effect':kept['actual_at_outcome']==before['actual_at_outcome'],
                'both_owned_supervisors_actually_terminated':owner.returncode is not None and backend.returncode is not None,
                'workers_serve_while_supervisors_are_down':all(direct),'router_outage_recorded_honestly':unavailable,
                'same_runtime':before['runtime_id']==restarted['runtime_id'],
                'same_worker_lifetimes':before['actual_at_outcome']['workers']==restarted['actual_at_outcome']['workers'],
                'same_confirmed_policy':kept['policy_snapshot']==restarted['policy_snapshot'],
                'exact_owner_recovery':result.get('recovery',{}).get('status')=='VERIFIED_RECOVERED',
                'allocation_released_after_native_exit':after['actual_at_outcome']['workers']['candidate-v2']['exit_confirmed'] is True and after['metrics']['committed']==30,
                'A_remains_paused':'deploy-A' in after['policy_snapshot']['state']['paused_runs'],
                'stable_available_after_recovery':all(v.get('ok') is True for v in samples),
                'original_team_evidence_unchanged':connected.sha(path/'team_summary.json')==rawteam and connected.sha(path/'team_outcome.json')==rawoutcome}
        finally:
            try:cleanup=connected.stop(path)['teardown']
            except (OSError,ValueError,http.client.HTTPException):
                connected.restart_session(path);cleanup=connected.stop(path)['teardown']
        checks['separate_teardown_confirmed']=cleanup['all_workers_stopped']
        rows.append({'scenario':family,'session_id':path.name,'runtime_id':before['runtime_id'],'checks':checks,
            'original_workers':before['actual_at_outcome']['workers'],'after_restart_workers':restarted['actual_at_outcome']['workers'],
            'policy_after_owner':after['metrics'],'recovery':result['recovery'],'teardown':cleanup})
    record={'version':VERSION,'status':'PASSED' if all(all(r['checks'].values()) for r in rows) else 'FAILED',
        'platform':platform.system(),'python_version':platform.python_version(),'created_at':sessions.now(),
        'connected_digest':connected.implementation_digest(),'cases':rows,'cases_passed':sum(all(r['checks'].values()) for r in rows),
        'checks_passed':sum(sum(r['checks'].values()) for r in rows),'model_calls':0,
        'scope':'Actual native owned-process termination, original worker-lifetime reattachment and exact scripted fixture owner consent. Local arithmetic HTTP, not GPU/model inference or production identity. Router outage during downtime is explicitly observed.'}
    workspace.write_json(output/'walkthrough.json',record);return record


def verify_sources():
    import project
    current=project.source_hashes();manifest=workspace.read_json(ROOT/'bundle_manifest.json')
    mismatched=[name for name,value in current.items() if manifest['source_sha256'].get(name)!=value]
    return {'status':'MATCH' if not mismatched else 'EDITED_OR_MANIFEST_PENDING','mismatched':mismatched,'source_sha256':current,
            'original_set_sha256':original.sha(original.SET_PATH),'connected_set_sha256':evaluation.sha(evaluation.SET_PATH),
            'connected_protocol':evaluation.protocol_digest()}


def live_runs(directory,split='development',max_output_tokens=4096,thinking=True):
    if not team.terminal_guard.interactive_input():raise ValueError('Live acceptance requires an interactive terminal before accessing credentials or workers.')
    evaluation.load_set(split)
    if type(max_output_tokens) is not int or not 1024<=max_output_tokens<=10000:raise ValueError('Use 1024-10000 output tokens.')
    combined_limit=30 if split=='development' else 18
    print('LIVE ACCEPTANCE: original 14 cases, at most 24 paid attempts; connected '+split+', at most '+str(combined_limit)+
        '. Total ceiling '+str(24+combined_limit)+'. No automatic retries. Exact manager and separate C owner consent are required in this terminal.',flush=True)
    key=(os.environ.get('NEBIUS_API_KEY') or getpass('Paste Nebius key locally (hidden): ')).strip()
    if not key:raise ValueError('No key supplied; no live run started.')
    # Constructing both registered clients before starting avoids leaving a
    # partial benchmark when the optional SDK is not installed.
    provider=service.NemotronAgent(key,max_output_tokens,thinking)
    factory=lambda case:{role:team.RoleClient(role,key,max_output_tokens,thinking) for role in team.ROLES}
    factory({})
    first,old=original.run_campaign(directory/'original_live',provider,call_limit=24,consent=service.terminal_consent)
    second,new=evaluation.run_campaign(directory/'connected_live',split,live=True,providers_factory=factory,call_limit=combined_limit,
        consent=service.terminal_consent,owner_consent=evaluation.terminal_owner,max_output_tokens=max_output_tokens,thinking=thinking)
    return {'status':'COMPLETED' if old['selected_set_completed'] and new['status']=='COMPLETED' else 'PARTIAL_REVIEW_REQUIRED',
        'original':{'directory':str(first),'summary':old},'connected':{'directory':str(second),'summary':new},
        'attempt_ceiling':24+combined_limit,'actual_attempts':old['model_calls']+new['model_calls'],
        'scope':'Actual endpoint attempts recorded before requests; model quality is distinct from native acceptance. Failed/unrun cases remain visible. Tokens without provider usage remain unknown.'},[first,second]


def stream_command(command,log,environment,cwd):
    """Tee a child's bytes to the log and console as they arrive."""
    import codecs
    decoder=codecs.getincrementaldecoder('utf-8')('replace')
    with Path(log).open('wb') as output:
        with subprocess.Popen(command,cwd=cwd,env=environment,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,shell=False) as child:
            try:
                while True:
                    chunk=child.stdout.read1(8192)
                    if not chunk:break
                    output.write(chunk);output.flush();sys.stdout.write(decoder.decode(chunk));sys.stdout.flush()
                sys.stdout.write(decoder.decode(b'',final=True));sys.stdout.flush();return child.wait()
            except BaseException:
                # Terminate only this invocation's live Popen handle, never a saved PID.
                if child.poll() is None:child.terminate()
                child.wait(timeout=10);raise


def run(output,live=False,live_only=False,walkthrough_only=False,split='development',max_output_tokens=4096,thinking=True):
    if live_only:live=True
    if live and not team.terminal_guard.interactive_input():raise ValueError('Run --live from an interactive terminal before credentials or local processes are accessed.')
    directory=service.new_directory(Path(output));directory.mkdir(parents=True,exist_ok=False)
    summary={'version':VERSION,'created_at':sessions.now(),'platform':platform.system(),'python_version':platform.python_version(),
        'status':'RUNNING','mode':'NATIVE OFFLINE ACCEPTANCE; ZERO MODEL CALLS' if not live else 'NATIVE ACCEPTANCE WITH EXPLICIT BOUNDED LIVE EVALUATIONS',
        'native_windows':'NOT_RUN_LIVE_ONLY' if live_only else 'RUNNING' if platform.system()=='Windows' else 'NOT_RUN_ON_THIS_PLATFORM','steps':[],
        'live':{'status':'NOT_RUN'},'confirmation_executed':False,'scope':'Native platform is recorded, never inferred from dispatch mocks.'}
    roots=[directory];workspace.write_json(directory/'acceptance.json',summary)
    def step(label,command,folder):
        log=directory/(label+'.log');print('Acceptance: '+label,flush=True)
        began=time.time()
        env=durable.restart.child_env()
        returncode=stream_command([sys.executable,'-B','-u',str(ROOT/'project.py'),*command],log,env,ROOT)
        paths=sorted((p for p in (ROOT/folder).glob('*/summary.json') if p.stat().st_mtime>=began),key=lambda p:p.stat().st_mtime)
        record={'name':label,'exit_code':returncode,'log':log.name,'evidence':str(paths[-1].parent) if paths else None}
        summary['steps'].append(record);workspace.write_json(directory/'acceptance.json',summary)
        if paths:roots.append(paths[-1].parent)
        if returncode:raise ValueError(label+' failed; inspect '+str(log))
        if not paths:raise ValueError(label+' returned without fresh saved evidence; no acceptance pass is inferred.')
        if label=='offline-suite':
            proof=workspace.read_json(paths[-1])
            if proof.get('passed') is not True or proof.get('platform')!=platform.system() or proof.get('source_sha256')!=sources['source_sha256']:
                raise ValueError('Suite platform/source/evidence does not match this acceptance run.')
    try:
        sources=verify_sources();workspace.write_json(directory/'verified_sources.json',sources)
        if sources['status']!='MATCH' and not walkthrough_only:raise ValueError('Bundle source hashes differ from the manifest; acceptance cannot certify this package. Review verified_sources.json.')
        if not live_only:
            if not walkthrough_only:
                step('offline-suite',['check'],'check_runs')
                step('original-14-scripted',['agent-evaluation'],'agent_evaluation_runs')
                step('frozen-team-6-scripted',['team-evaluation'],'team_evaluation_runs')
                step('connected-10-scripted',['connected-evaluation'],'connected_evaluation_runs')
                step('coordination-12-policy',['coordination-evaluation'],'coordination_benchmark_runs')
                step('coordination-real-demo',['coordination','demo'],'coordination_runs')
                step('three-role-mission',['mission','demo'],'mission_runs')
                step('operations-end-to-end',['operations','demo'],'operations_runs')
            record=walkthrough(directory/'restart_walkthrough');summary['walkthrough']=record
            if record['status']!='PASSED':raise ValueError('Connected walkthrough failed.')
            summary['offline_status']='PASSED'
            summary['native_windows']='PASSED' if platform.system()=='Windows' and not walkthrough_only else 'WALKTHROUGH_ONLY' if platform.system()=='Windows' else 'NOT_RUN_ON_THIS_PLATFORM'
        if live:
            summary['live'],live_roots=live_runs(directory,split,max_output_tokens,thinking);roots+=live_roots
            summary['confirmation_executed']=split=='confirmation' and summary['live']['connected']['summary'].get('attempted',0)>0
        summary['status']='PASSED' if not live or summary['live']['status']=='COMPLETED' else 'PARTIAL_REVIEW_REQUIRED'
    except (Exception,KeyboardInterrupt) as exc:
        summary['status']='INTERRUPTED' if isinstance(exc,KeyboardInterrupt) else 'FAILED';summary['error']=type(exc).__name__+': '+str(exc)
        if platform.system()=='Windows' and summary['native_windows']=='RUNNING':summary['native_windows']='FAILED_OR_INCOMPLETE'
    finally:
        workspace.write_json(directory/'acceptance.json',summary)
        summary['export']=evidence_export(directory/'Orkestryn-Evidence.zip',roots)
        workspace.write_json(directory/'acceptance.json',summary)
    return directory,summary


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=ROOT/'acceptance_runs')
    parser.add_argument('--live',action='store_true');parser.add_argument('--live-only',action='store_true');parser.add_argument('--walkthrough-only',action='store_true')
    parser.add_argument('--split',choices=('development','confirmation'),default='development');parser.add_argument('--max-output-tokens',type=int,default=4096)
    parser.add_argument('--thinking',choices=('on','off'),default='on');args=parser.parse_args(argv)
    directory,summary=run(args.output,args.live,args.live_only,args.walkthrough_only,args.split,args.max_output_tokens,args.thinking=='on')
    print('Acceptance: '+summary['status']+' | native platform: '+summary['platform']+' | Windows: '+summary['native_windows'])
    print('Evidence: '+str(directory));print('Export: '+str(directory/'Orkestryn-Evidence.zip'))
    return 0 if summary['status']=='PASSED' else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError) as exc:print('ACCEPTANCE STOPPED: '+str(exc),file=sys.stderr);raise SystemExit(1)
