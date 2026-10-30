#!/usr/bin/env python3
"""Read-only live walkthrough evidence. Never requests a model or changes a run."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

import operations_workspace as ops
import operations_outcomes as outcomes
import release_check


def assess(summary, audit):
    calls=summary.get('calls',[])
    records=[]
    for call in calls:
        meta=call.get('metadata') or {}
        # Whitelist provider/accounting fields; prompts and reply text stay out.
        records.append({k:v for k,v in {
            'role':call.get('role'),'status':call.get('status'),'live':call.get('live'),
            'source':meta.get('source'),'returned_model':meta.get('model'),
            'response_id':meta.get('response_id'),'finish_reason':meta.get('finish_reason'),
            'refused':meta.get('refused'),'input_tokens':meta.get('input_tokens'),
            'output_tokens':meta.get('output_tokens'),'total_tokens':meta.get('total_tokens'),
            'latency_seconds':meta.get('latency_seconds')}.items()})
    exact_roles=[r.get('role') for r in records]==list(ops.ROLES)
    checks={
        'current_source_and_ledger':audit.get('status')=='CONSISTENT' and not summary.get('historical_operations',True),
        'live_workflow':summary.get('live') is True,
        'three_ordered_roles':exact_roles,
        'three_persisted_live_attempts':summary.get('model_calls')==3 and len(records)==3 and all(r['live'] is True for r in records),
        'all_three_replies_validated':len(records)==3 and all(r['status']=='VALIDATED' for r in records),
        'live_client_metadata_present':exact_roles and all(
            isinstance(r['source'],str) and r['source'] in {'LIVE NEBIUS MISSION '+r['role'].upper(),
                          'LIVE NVIDIA VIA NEBIUS TOKEN FACTORY; SESSION KEY'}
            and isinstance(r['response_id'],str) and bool(r['response_id'])
            and isinstance(r['returned_model'],str) and bool(r['returned_model'])
            and r['finish_reason']=='stop' and r['refused'] is False for r in records),
        'recovery_completed':summary.get('phase')=='RECOVERED_SERVICE_ACTIVE',
        'owned_workers_confirmed_stopped':summary.get('lifecycle')=='STOPPED' and summary.get('teardown',{}).get('all_workers_stopped') is True,
    }
    live_records=[r for r in records if r['live'] is True]
    usage=[r['total_tokens'] for r in live_records if type(r['total_tokens']) is int and r['total_tokens']>=0]
    return {'schema_version':'agent-rehearsal-live-walkthrough-v1',
        'status':'READY_FROM_SAVED_LIVE_RECORDS' if all(checks.values()) else 'NEEDS_LIVE_WALKTHROUGH',
        'checks':checks,'run':summary.get('mission_id'),'phase':summary.get('phase'),
        'lifecycle':summary.get('lifecycle'),'summary_digest':summary.get('evidence_digest'),
        'configured_model':ops.factory.inference.MODEL,
        'configured_endpoint':ops.factory.inference.BASE_URL,
        'roles':records,'reported_total_tokens':sum(usage),'attempts_with_unknown_usage':len(live_records)-len(usage),
        'model_calls_to_build_report':0,
        'scope':'Checks saved local records and their ledger/source consistency. This is not provider-signed attestation, a quality benchmark, a cloud GPU deployment claim, or proof of hosted availability.'}


def capture(path, output):
    path=ops.directory_for(path)
    destination=Path(output).absolute().resolve()
    if destination==path.resolve() or path.resolve() in destination.parents:
        raise ValueError('Export evidence outside the source run directory.')
    before=(path/'summary.json').read_bytes()
    summary=ops.read(path);audit=ops.verify(path);report=assess(summary,audit)
    report['created_at']=datetime.now(timezone.utc).isoformat()
    # A changing observer/run needs a fresh read; never mix different snapshots.
    outcome=outcomes.build(path)
    if (path/'summary.json').read_bytes()!=before:
        raise ValueError('The run changed during capture. Stop the observer and owned workers, then capture again.')
    report['source_summary_sha256']=hashlib.sha256(before).hexdigest()
    entries={'live_walkthrough.json':(json.dumps(report,indent=2)+'\n').encode(),
             'outcome_report.json':(json.dumps(outcome,indent=2)+'\n').encode(),
             'outcome_report.html':outcomes.render(outcome).encode()}
    findings=release_check.check_entries([(n,data,'100644') for n,data in entries.items()])
    if findings:raise ValueError('The public evidence contains a possible credential. Inspect it locally; no export was written.')
    directory=destination/('live-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
    directory.mkdir(parents=True,exist_ok=False)
    for name,data in entries.items():(directory/name).write_bytes(data)
    return directory,report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True,help='Path to the exact completed operations run.')
    parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parent/'live_evidence_checks')
    args=parser.parse_args(argv)
    try:directory,report=capture(args.run,args.output)
    except (ValueError,OSError,KeyError):
        print('Could not capture a consistent public report. Check the run, source version and stopped state. No model call was made.')
        return 1
    print('Live walkthrough: '+report['status'])
    print('Checks needing attention: '+(', '.join(k for k,v in report['checks'].items() if not v) or 'none'))
    print('Evidence: '+str(directory));print('No model call or workflow effect was made by this capture.')
    return 0 if report['status']=='READY_FROM_SAVED_LIVE_RECORDS' else 1


if __name__=='__main__':raise SystemExit(main())
