#!/usr/bin/env python3
"""Portable, read-only Operations evidence. No models, workers or account exports."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import html
from io import BytesIO
import json
from pathlib import Path
import re
import stat
import uuid
import zlib
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED, ZIP_STORED

import live_readiness
import operations_outcomes as outcomes
import operations_workspace as ops
import release_check
import simulator as sim
import team_workspace as ws

VERSION = 'agent-rehearsal-portable-evidence-v1'
MEMBERS = {'index.html', 'outcome_report.html', 'outcome_report.json',
           'agent_attempts.json', 'activity.json', 'README.txt', 'bundle_manifest.json'}
MAX_MEMBER = 4 * 1024 * 1024
MAX_TOTAL = 8 * 1024 * 1024
MAX_EVENTS = 200
APPROVALS = {'BATCH_CONSENT_INTENT', 'EXACT_RECOVERY_APPROVED',
             'FAILOVER_CONSENT_RECORDED', 'C_JOB_RELEASE_INTENT'}
EFFECTS = {'BATCH_EXECUTION_RECORDED', 'BOUNDED_RECOVERY_RECORDED',
           'EMERGENCY_HANDOFF_CONFIRMED', 'C_JOB_EFFECT_CONFIRMED',
           'C_JOB_RELEASE_COMPLETED', 'MISSION_TEARDOWN_RECORDED'}


class SnapshotChanged(ValueError):
    pass


def _json(value):
    return (json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + '\n').encode()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def activity(summary):
    """A whitelisted timeline, not the original journal or private event payloads."""
    records = []
    for event in reversed(summary['events'][-MAX_EVENTS:]):
        data = event.get('data') or {}
        principal = data.get('principal') or {}
        kind = event['kind']
        if not isinstance(kind, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', kind):
            kind = 'UNRECOGNIZED_EVENT'
        status = 'APPROVAL_RECORD' if kind in APPROVALS else 'EFFECT_RECORD' if kind in EFFECTS else 'OBSERVATION_OR_PROGRESS'
        item = {'sequence': event['sequence'], 'at': event['at'], 'kind': kind,
                'category': status, 'source_event_digest': event['digest']}
        if principal:
            item['actor'] = {'name': principal.get('name'), 'role': data.get('role'),
                             'mode': 'SCRIPTED_FIXTURE' if str(principal.get('subject', '')).startswith('fixture:') else 'NAMED_WORKSPACE_ACCOUNT'}
        if kind == 'MISSION_TEARDOWN_RECORDED':
            item['all_workers_stopped'] = data.get('all_workers_stopped')
        records.append(item)
    result = {'run': summary['mission_id'], 'summary_digest': summary['evidence_digest'],
              'total_source_events': len(summary['events']), 'included_events': len(records),
              'selection': 'Latest at most 200 events, newest first', 'records': records,
              'scope': 'Selected fields only. Source-event digests are references; omitted private event bodies cannot be reconstructed or independently authenticated from this timeline.'}
    result['activity_digest'] = sim.digest(result)
    return result


def render_index(report, agents, timeline):
    esc = lambda value: html.escape(str(value if value is not None else 'Not recorded'), quote=True)
    human = lambda value: str(value).replace('_', ' ').lower().capitalize()
    mode = 'Live proposal records' if report['proposal_mode'] == 'LIVE_PROVIDER' else 'Scripted rehearsal'
    source = 'Historical source version' if report['evidence']['historical_source'] else 'Current source at export'
    metrics = {item['key']: item for item in report['metrics']}
    metric_rows = ''.join('<tr><th>' + esc(metrics[key]['label']) + '</th><td>' + esc(metrics[key]['value']) + '</td></tr>'
                          for key in ('submitted', 'completed', 'waiting', 'dependencies_unconfirmed', 'live_role_attempts', 'role_tokens'))
    role_rows = ''.join('<tr><th>' + esc(human(role['role'])) + '</th><td>' + esc(human(role['status'])) + '</td><td>'
                        + esc(role['total_tokens']) + '</td></tr>' for role in agents['roles'])
    event_rows = ''.join('<tr><td>' + esc(row['sequence']) + '</td><td>' + esc(row['at']) + '</td><td>'
                         + esc(human(row['kind'])) + '</td><td>' + esc(human(row['category'])) + '</td><td>'
                         + esc((row.get('actor') or {}).get('name')) + '</td></tr>' for row in timeline['records'])
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
            '<title>Orkestryn evidence</title><style>:root{color-scheme:dark;font:16px/1.55 system-ui,Arial,sans-serif;background:#0b1220;color:#eaf2fa}'
            'body{margin:0}main{max-width:1080px;margin:auto;padding:36px 24px 60px}h1{font-size:clamp(28px,5vw,42px);line-height:1.15}'
            'h2{font-size:22px}p{color:#b5c5d5}.tag{display:inline-block;border:1px solid #35566b;border-radius:22px;padding:5px 12px;margin:4px 8px 4px 0}'
            'section{background:#131f30;border:1px solid #304359;border-radius:12px;padding:24px;margin:22px 0}.scroll{overflow:auto}'
            'table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:12px;text-align:left;border-bottom:1px solid #304359;vertical-align:top}'
            'a{color:#91e2ce}code{overflow-wrap:anywhere}footer{color:#a9bacb}nav{display:flex;gap:20px;flex-wrap:wrap}</style>'
            '<main><header><p>ORKESTRYN · PORTABLE EVIDENCE</p><h1>A saved run, ready for review.</h1>'
            '<p>This file replays recorded results. It cannot approve actions, contact a provider or run the service.</p>'
            '<span class="tag">' + esc(mode) + '</span><span class="tag">' + esc(source) + '</span><span class="tag">'
            + esc(human(report['lifecycle'])) + '</span><p>Run <code>' + esc(report['run']) + '</code></p></header>'
            '<nav><a href="outcome_report.html">Detailed outcome report</a><a href="agent_attempts.json">Agent records</a>'
            '<a href="activity.json">Activity records</a><a href="README.txt">How to review</a></nav>'
            '<section><h2>Recorded outcome</h2><p>' + esc(human(report['phase'])) + '</p><table>' + metric_rows + '</table>'
            '<p>Unknown measurements remain “Not recorded”. Capacity, ownership and workloads are local fixtures; these are not cloud GPU measurements.</p></section>'
            '<section><h2>Three-agent evidence</h2><p>Saved-record assessment: ' + esc(human(agents['status'])) + '.</p>'
            '<table><tr><th>Role</th><th>Saved status</th><th>Reported tokens</th></tr>' + role_rows + '</table>'
            '<p>Scripted decisions, live attempts and validated replies are different records. Missing usage is unknown. '
            'This export does not attest that a provider response is authentic or that a hosted demo is available.</p></section>'
            '<section><h2>Approval and execution timeline</h2><p>' + esc(timeline['included_events']) + ' of '
            + esc(timeline['total_source_events']) + ' source events included, newest first. Approval records do not prove execution; '
            'an effect record must be inspected in the detailed report to assess its outcome.</p><div class="scroll"><table>'
            '<tr><th>Event</th><th>Recorded time</th><th>Milestone</th><th>Record type</th><th>Actor</th></tr>' + event_rows + '</table></div></section>'
            '<footer><p>Source summary digest: <code>' + esc(report['evidence']['summary_digest']) + '</code></p>'
            '<p>No model call or service effect was made to create this package. Local hashes detect changed bytes; they are not signatures or external attestation.</p></footer></main></html>')


def from_snapshot(summary, audit):
    report = outcomes.from_summary(summary)
    report['report_digest'] = sim.digest(report)
    agents = live_readiness.assess(summary, audit)
    timeline = activity(summary)
    files = {'index.html': render_index(report, agents, timeline).encode(),
             'outcome_report.html': outcomes.render(report).encode(),
             'outcome_report.json': _json(report), 'agent_attempts.json': _json(agents),
             'activity.json': _json(timeline),
             'README.txt': (
                 'ORKESTRYN - PORTABLE EVIDENCE\n\nExtract the whole ZIP, then open index.html in a browser. No Python or source installation is needed for this read-only replay.\n\n'
                 'The outcome report, agent metadata and selected activity fields come from one saved summary. Raw prompts/replies, account databases, passwords, provider keys, task inputs and runtime capabilities are not exported. Review the files before sharing.\n\n'
                 'Approval is separate from execution. Scripted rehearsal is separate from live inference. A stopped run is saved evidence, not a running service. A historical-source run is not current-source validation. Missing measurements remain unknown.\n\n'
                 'Use python operations_evidence.py verify PATH_TO_ZIP to check the fixed file inventory, hashes and cross-file references. This verifies transport integrity, not authorship, provider attestation, complete private journals, current liveness or competition compliance. Only open archives from a source you trust.\n\n'
                 'A replay package supplements the working application. It does not replace judge access to a working demo or test build.\n').encode()}
    if any(len(raw) > MAX_MEMBER for raw in files.values()) or sum(map(len, files.values())) > MAX_TOTAL:
        raise ValueError('The selected evidence exceeds the portable export limit.')
    if release_check.check_entries([(name, raw, '100644') for name, raw in files.items()]):
        raise ValueError('The export contains a possible credential. Review it locally before sharing.')
    manifest = {'schema_version': VERSION, 'run': report['run'], 'summary_digest': report['evidence']['summary_digest'],
                'snapshot_at': report['snapshot_at'], 'historical_source': report['evidence']['historical_source'],
                'operations_source': report['evidence']['operations_source'],
                'files': {name: {'sha256': _sha(raw), 'bytes': len(raw)} for name, raw in sorted(files.items())},
                'effects': False, 'model_calls_to_export': 0,
                'scope': 'Read-only saved evidence. Hashes and redacted references are not signatures or external attestation.'}
    files['bundle_manifest.json'] = _json(manifest)
    if sum(map(len, files.values())) > MAX_TOTAL:
        raise ValueError('The complete portable export exceeds its size limit.')
    return files


def build_bytes(directory):
    directory = ops.directory_for(directory)
    raw = ws.read_bytes(directory / 'summary.json')
    summary = ops.read(directory)
    if summary['historical_operations']:
        audit = {'status': 'HISTORICAL_SOURCE_NOT_REVALIDATED'}
    else:
        audit = ops.verify(directory)
        if audit['status'] != 'CONSISTENT':
            raise ValueError('Current run verification is incomplete; inspect its saved evidence.')
    files = from_snapshot(summary, audit)
    if ws.read_bytes(directory / 'summary.json') != raw:
        raise SnapshotChanged('This run changed during export. Refresh and download a fresh snapshot.')
    stream = BytesIO()
    with ZipFile(stream, 'w', ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            archive.writestr(name, content)
    result = stream.getvalue()
    if len(result) > MAX_TOTAL:
        raise ValueError('The portable archive exceeds the export limit.')
    safe_id = re.sub(r'[^A-Za-z0-9._-]', '_', summary['mission_id'])[:100]
    return result, 'Orkestryn-Evidence-' + safe_id + '.zip'


def _decode(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate JSON field in evidence.')
            value[key] = item
        return value
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite evidence number.')))


def verify_bytes(raw):
    """Read bounded members in memory; never extract files or execute the HTML."""
    if not isinstance(raw, bytes) or len(raw) > MAX_TOTAL:
        raise ValueError('The archive exceeds the verification limit.')
    try:
        with ZipFile(BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) != len(MEMBERS) or {item.filename for item in infos} != MEMBERS:
                raise ValueError('The evidence archive must contain only its fixed public files.')
            if sum(item.file_size for item in infos) > MAX_TOTAL:
                raise ValueError('Uncompressed evidence exceeds the verification limit.')
            files = {}
            for item in infos:
                mode = stat.S_IFMT(item.external_attr >> 16)
                if (item.is_dir() or mode not in {0, stat.S_IFREG} or item.flag_bits & 1
                        or item.compress_type not in {ZIP_STORED, ZIP_DEFLATED}
                        or item.file_size > MAX_MEMBER):
                    raise ValueError('Unsupported evidence member or size.')
                with archive.open(item) as member:
                    content = member.read(MAX_MEMBER + 1)
                if len(content) != item.file_size or len(content) > MAX_MEMBER:
                    raise ValueError('Evidence member size did not match.')
                files[item.filename] = content
        manifest = _decode(files['bundle_manifest.json'])
        expected = MEMBERS - {'bundle_manifest.json'}
        keys={'schema_version','run','summary_digest','snapshot_at','historical_source',
              'operations_source','files','effects','model_calls_to_export','scope'}
        if set(manifest)!=keys or manifest['schema_version'] != VERSION or set(manifest['files']) != expected:
            raise ValueError('The evidence manifest has an unsupported inventory.')
        if (type(manifest['historical_source']) is not bool
                or not isinstance(manifest['summary_digest'],str)
                or not re.fullmatch(r'[0-9a-f]{64}',manifest['summary_digest'])):
            raise ValueError('The evidence snapshot reference is invalid.')
        for name in expected:
            record = manifest['files'][name]
            if record != {'sha256': _sha(files[name]), 'bytes': len(files[name])}:
                raise ValueError('The exported evidence bytes changed.')
        report = _decode(files['outcome_report.json'])
        agents = _decode(files['agent_attempts.json'])
        timeline = _decode(files['activity.json'])
        for document in (report, agents, timeline):
            digest = document['evidence']['summary_digest'] if document is report else document['summary_digest']
            if document['run'] != manifest['run'] or digest != manifest['summary_digest']:
                raise ValueError('The exported reports reference different snapshots.')
        if report['report_digest'] != sim.digest({k:v for k,v in report.items() if k != 'report_digest'}):
            raise ValueError('The outcome report digest changed.')
        if timeline['activity_digest'] != sim.digest({k:v for k,v in timeline.items() if k != 'activity_digest'}):
            raise ValueError('The activity report digest changed.')
        if (manifest.get('effects') is not False or type(manifest.get('model_calls_to_export')) is not int
                or manifest['model_calls_to_export'] != 0):
            raise ValueError('Unsupported export effect declaration.')
    except (BadZipFile, KeyError, TypeError, UnicodeError, RuntimeError, NotImplementedError, zlib.error, EOFError, RecursionError, OverflowError):
        raise ValueError('The evidence archive could not be verified.') from None
    return {'status': 'INTEGRITY_CHECKED', 'run': manifest['run'], 'files': len(files),
            'historical_source': manifest['historical_source'], 'proposal_mode': report['proposal_mode'],
            'effects': False, 'model_calls': 0,
            'scope': 'Fixed inventory, file hashes and cross-file references checked. No author, provider, current runtime, complete journal or hosting attestation.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create', help='Export one saved run without new effects or model calls.')
    create.add_argument('--run', type=Path, required=True)
    create.add_argument('--output', type=Path, default=Path(__file__).resolve().parent/'evidence_exports')
    verify = commands.add_parser('verify', help='Verify an archive in memory without extracting or running it.')
    verify.add_argument('archive', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'verify':
            if args.archive.stat().st_size > MAX_TOTAL:
                raise ValueError('The archive exceeds the verification limit.')
            with args.archive.open('rb') as source:
                raw=source.read(MAX_TOTAL+1)
            print(json.dumps(verify_bytes(raw)))
        else:
            run = ops.directory_for(args.run)
            destination = args.output.absolute().resolve()
            if destination == run.resolve() or run.resolve() in destination.parents:
                raise ValueError('Export outside the source run directory.')
            raw, filename = build_bytes(run)
            verify_bytes(raw)
            folder = destination/('export-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
            folder.mkdir(parents=True, exist_ok=False)
            with (folder/filename).open('xb') as stream:
                stream.write(raw)
            print('Evidence ZIP: ' + str(folder/filename))
            print('Read-only export; zero model calls and no workflow changes.')
    except (ValueError, OSError):
        print('Evidence operation stopped. Check the exact run/archive, source version and snapshot consistency. No automatic retry or model call was made.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
