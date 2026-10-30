"""Public evidence privacy, integrity, snapshot consistency and authenticated HTTP."""
import copy
from contextlib import contextmanager
import hashlib
from http.cookiejar import CookieJar
from io import BytesIO
import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor
from zipfile import ZipFile, ZIP_DEFLATED, ZipInfo

import named_access
import operations_evidence as export
import operations_server
import operations_workspace as ops
import simulator as sim


class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='portable evidence ')
        cls.root = Path(cls.temp.name)
        cls.path, _ = ops.demo(cls.root/'runs')
        cls.summary = ops.read(cls.path)
        cls.audit = ops.verify(cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def files(self, summary=None, audit=None):
        return export.from_snapshot(copy.deepcopy(summary or self.summary), audit or self.audit)

    def zip(self, files, symlink=None):
        target = BytesIO()
        with ZipFile(target, 'w', ZIP_DEFLATED) as archive:
            for name, raw in files.items():
                if name == symlink:
                    info = ZipInfo(name);info.create_system=3;info.external_attr=(stat.S_IFLNK|0o777)<<16
                    archive.writestr(info, raw)
                else:archive.writestr(name, raw)
        return target.getvalue()

    def rehash(self, files):
        manifest = json.loads(files['bundle_manifest.json'])
        manifest['files'] = {name:{'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
                             for name,raw in files.items() if name != 'bundle_manifest.json'}
        files['bundle_manifest.json'] = json.dumps(manifest).encode()
        return files

    def tree(self):
        return {str(p.relative_to(self.path)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.path.rglob('*') if p.is_file()}

    @contextmanager
    def server(self):
        auth = named_access.Access(self.path.parent)
        try:
            if not auth.count():auth.create('evidence-viewer','Fixture viewer password!',['viewer'])
        finally:auth.close()
        server=operations_server.Server(self.path.parent)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        client=build_opener(HTTPCookieProcessor(CookieJar()))
        try:yield base,client
        finally:server.shutdown();server.server_close();thread.join(5)

    def login(self,base,client):
        request=Request(base+'/api/login',data=json.dumps({'name':'evidence-viewer','password':'Fixture viewer password!'}).encode(),
                        headers={'Content-Type':'application/json','Origin':base},method='POST')
        with client.open(request,timeout=10) as response:return json.load(response)['csrf']

    def test_complete_scripted_export_has_fixed_inventory_and_integrity_scope(self):
        raw,filename=export.build_bytes(self.path)
        self.assertTrue(filename.startswith('Orkestryn-Evidence-'))
        result=export.verify_bytes(raw)
        self.assertEqual(result['status'],'INTEGRITY_CHECKED');self.assertEqual(result['proposal_mode'],'SCRIPTED_FIXTURE')
        self.assertFalse(result['historical_source']);self.assertEqual(result['model_calls'],0)
        with ZipFile(BytesIO(raw)) as archive:self.assertEqual(set(archive.namelist()),export.MEMBERS)
        self.assertIn('No author, provider',result['scope'])

    def test_private_prompts_replies_task_inputs_and_extra_metadata_are_not_exported(self):
        summary=copy.deepcopy(self.summary)
        marker='PRIVATE_INPUT_FIXTURE_DO_NOT_EXPORT'
        summary.update(goal=marker,private_key=marker)
        for call in summary['calls']:
            call.update(system=marker,context={'private':marker},visible_content=marker)
            call['metadata']['private_extra']=marker
        summary['events'][0]['data']['private_input']=marker
        data=b''.join(self.files(summary).values())
        self.assertNotIn(marker.encode(),data)
        for forbidden in (b'accounts.sqlite3',b'.access.json',b'NEBIUS_API_KEY='):
            self.assertNotIn(forbidden,data)

    def test_export_cannot_open_a_runtime_request_a_model_or_mutate_saved_files(self):
        before=self.tree()
        with (mock.patch.object(ops,'opened',side_effect=AssertionError('No runtime')),
              mock.patch.object(ops.RoleClient,'ask',side_effect=AssertionError('No model'))):
            export.build_bytes(self.path)
        self.assertEqual(self.tree(),before)
        self.assertFalse((self.path/'outcome_report.html').exists())

    def test_scripted_run_is_not_a_live_provider_pass(self):
        files=self.files();agents=json.loads(files['agent_attempts.json'])
        self.assertEqual(agents['status'],'NEEDS_LIVE_WALKTHROUGH')
        self.assertFalse(agents['checks']['live_workflow'])
        self.assertIn(b'Scripted rehearsal',files['index.html'])
        self.assertIn(b'not replace judge access',files['README.txt'])

    def test_mocked_live_flag_does_not_certify_actual_provider_records(self):
        summary=copy.deepcopy(self.summary);summary['live']=True;summary['model_calls']=3
        for call in summary['calls']:call['live']=True;call['metadata']['source']='MOCK ONLY; NO NETWORK'
        agents=json.loads(self.files(summary)['agent_attempts.json'])
        self.assertEqual(agents['status'],'NEEDS_LIVE_WALKTHROUGH')
        self.assertFalse(agents['checks']['live_client_metadata_present'])

    def test_pending_run_preserves_unknown_measurements(self):
        summary=copy.deepcopy(self.summary)
        summary.update(service=None,recovery_grant=None,recovery=None,incident=None,health=[],calls=[],phase='DRAFT',lifecycle='LEASED')
        summary['operations'].update(emergency=None,jobs=[])
        files=self.files(summary);report=json.loads(files['outcome_report.json'])
        metric={row['key']:row['value'] for row in report['metrics']}
        self.assertIsNone(metric['completed']);self.assertIsNone(metric['handoff_seconds'])
        self.assertIn(b'Not recorded',files['index.html'])

    def test_approvals_are_not_labeled_as_confirmed_effects(self):
        records=json.loads(self.files()['activity.json'])['records']
        lookup={row['kind']:row for row in records}
        self.assertEqual(lookup['FAILOVER_CONSENT_RECORDED']['category'],'APPROVAL_RECORD')
        self.assertEqual(lookup['EXACT_RECOVERY_APPROVED']['category'],'APPROVAL_RECORD')
        self.assertEqual(lookup['EMERGENCY_HANDOFF_CONFIRMED']['category'],'EFFECT_RECORD')
        self.assertEqual(lookup['ROLE_REPLY_RECORDED']['category'],'OBSERVATION_OR_PROGRESS')
        self.assertNotIn('subject',lookup['FAILOVER_CONSENT_RECORDED']['actor'])

    def test_untrusted_actor_text_is_escaped_in_offline_html(self):
        summary=copy.deepcopy(self.summary)
        for row in summary['events']:
            if row.get('data',{}).get('principal'):row['data']['principal']['name']='</td><script>bad()</script>'
        page=self.files(summary)['index.html'].decode()
        self.assertNotIn('<script',page);self.assertIn('&lt;script&gt;',page)
        self.assertIn('Content-Security-Policy',page);self.assertNotIn('<script',self.files(summary)['outcome_report.html'].decode())

    def test_long_timelines_show_the_selected_and_original_counts(self):
        summary=copy.deepcopy(self.summary);summary['events']*=7
        timeline=export.activity(summary)
        self.assertEqual(timeline['included_events'],200)
        self.assertEqual(timeline['total_source_events'],len(summary['events']))
        self.assertIn('Selected fields only',timeline['scope'])

    def test_historical_run_exports_as_historical_without_rebinding_or_current_audit(self):
        with (mock.patch.object(ops,'source_digest',return_value='different-current-source'),
              mock.patch.object(ops,'verify',side_effect=AssertionError('Do not revalidate old source as current'))):
            raw,_=export.build_bytes(self.path)
        result=export.verify_bytes(raw);self.assertTrue(result['historical_source'])
        with ZipFile(BytesIO(raw)) as archive:self.assertIn(b'Historical source version',archive.read('index.html'))

    def test_export_detects_snapshot_change_without_repeating_any_stage(self):
        original=(self.path/'summary.json').read_bytes();build=export.from_snapshot
        def changed(*args):
            files=build(*args);(self.path/'summary.json').write_bytes(original+b'\n');return files
        try:
            with mock.patch.object(export,'from_snapshot',side_effect=changed) as capture:
                with self.assertRaises(export.SnapshotChanged):export.build_bytes(self.path)
                self.assertEqual(capture.call_count,1)
        finally:(self.path/'summary.json').write_bytes(original)

    def test_tampered_source_summary_is_rejected_before_a_bundle_is_returned(self):
        original=(self.path/'summary.json').read_bytes();summary=json.loads(original);summary['phase']='FORGED_PHASE'
        try:
            (self.path/'summary.json').write_text(json.dumps(summary))
            with self.assertRaises(ValueError):export.build_bytes(self.path)
        finally:(self.path/'summary.json').write_bytes(original)

    def test_modified_member_fails_even_with_valid_zip_crc(self):
        files=self.files();files['README.txt']+=b'changed'
        with self.assertRaises(ValueError):export.verify_bytes(self.zip(files))

    def test_extra_private_or_path_traversal_members_are_rejected_without_extraction(self):
        for name in ('../outside.txt','.control_access/accounts.sqlite3','extra.txt'):
            files=self.files();files[name]=b'fixture'
            with self.subTest(name=name),self.assertRaises(ValueError):export.verify_bytes(self.zip(files))
        files=self.files();files['../README.txt']=files.pop('README.txt')
        with self.assertRaises(ValueError):export.verify_bytes(self.zip(files))

    def test_duplicate_members_and_file_links_are_rejected(self):
        raw=self.zip(self.files());stream=BytesIO(raw)
        with self.assertWarns(UserWarning):
            with ZipFile(stream,'a') as archive:archive.writestr('README.txt',b'duplicate')
        with self.assertRaises(ValueError):export.verify_bytes(stream.getvalue())
        with self.assertRaises(ValueError):export.verify_bytes(self.zip(self.files(),symlink='README.txt'))

    def test_oversized_compressed_and_uncompressed_inputs_are_rejected(self):
        raw=self.zip(self.files())
        with mock.patch.object(export,'MAX_TOTAL',len(raw)-1),self.assertRaises(ValueError):export.verify_bytes(raw)
        files=self.files();files['README.txt']=b'a'*10000
        with mock.patch.object(export,'MAX_MEMBER',9000),self.assertRaises(ValueError):export.verify_bytes(self.zip(files))

    def test_rehashed_mixed_snapshots_still_fail_cross_file_checks(self):
        files=self.files();report=json.loads(files['outcome_report.json']);report['run']='different-run'
        report['report_digest']=sim.digest({k:v for k,v in report.items() if k!='report_digest'})
        files['outcome_report.json']=json.dumps(report).encode()
        with self.assertRaises(ValueError):export.verify_bytes(self.zip(self.rehash(files)))

    def test_duplicate_json_fields_and_invalid_archive_are_rejected(self):
        files=self.files();files['activity.json']=b'{"run":"first","run":"second"}'
        with self.assertRaises(ValueError):export.verify_bytes(self.zip(self.rehash(files)))
        for raw in (b'not-a-zip',b''):
            with self.assertRaises(ValueError):export.verify_bytes(raw)

    def test_common_credential_pattern_is_blocked_before_export(self):
        summary=copy.deepcopy(self.summary)
        summary['calls'][0]['metadata']['response_id']='sk-'+'X'*40
        with self.assertRaises(ValueError):self.files(summary)

    def test_generated_export_folders_cannot_be_force_staged_as_source(self):
        self.assertIsNotNone(export.release_check.path_problem('evidence_exports/export-1/outcome_report.json'))

    def test_viewer_download_requires_login_has_attachment_headers_and_is_read_only(self):
        before=self.tree()
        with self.server() as (base,client):
            url=base+'/api/evidence?run='+self.path.name
            with self.assertRaises(HTTPError) as denied:client.open(url,timeout=10)
            self.assertEqual(denied.exception.code,401)
            self.login(base,client)
            with client.open(url,timeout=20) as response:
                raw=response.read();self.assertEqual(response.headers['Content-Type'],'application/zip')
                self.assertIn('attachment;',response.headers['Content-Disposition']);self.assertIn('no-store',response.headers['Cache-Control'])
                self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')
            self.assertEqual(export.verify_bytes(raw)['run'],self.path.name)
        self.assertEqual(self.tree(),before)

    def test_bad_queries_origins_and_paths_cannot_reach_the_exporter(self):
        with self.server() as (base,client):
            self.login(base,client)
            with mock.patch.object(export,'build_bytes',side_effect=AssertionError('Must deny before export')):
                for query in ('','run=','run=../outside','run=%2Ftmp','run=a&run=b','run=a&baseline=b'):
                    with self.subTest(query=query),self.assertRaises(HTTPError) as denied:client.open(base+'/api/evidence?'+query,timeout=10)
                    self.assertEqual(denied.exception.code,400)
                with self.assertRaises(HTTPError) as denied:
                    client.open(Request(base+'/api/evidence?run='+self.path.name,headers={'Origin':'https://attacker.example'}),timeout=10)
                self.assertEqual(denied.exception.code,403)

    def test_http_snapshot_change_has_a_specific_error_and_no_automatic_retry(self):
        with self.server() as (base,client):
            self.login(base,client)
            with mock.patch.object(export,'build_bytes',side_effect=export.SnapshotChanged('private fixture details')) as capture:
                with self.assertRaises(HTTPError) as denied:client.open(base+'/api/evidence?run='+self.path.name,timeout=10)
                self.assertEqual(denied.exception.code,409)
                value=json.load(denied.exception);self.assertEqual(value['code'],'EVIDENCE_CHANGED')
                self.assertNotIn('private fixture details',json.dumps(value));self.assertEqual(capture.call_count,1)

    def test_signed_out_session_can_no_longer_export(self):
        with self.server() as (base,client):
            csrf=self.login(base,client)
            with client.open(Request(base+'/api/logout',data=b'{}',method='POST',headers={'Content-Type':'application/json','Origin':base,'X-CSRF-Token':csrf}),timeout=10) as response:
                self.assertEqual(response.status,200)
            with mock.patch.object(export,'build_bytes',side_effect=AssertionError('No export for signed-out sessions')):
                with self.assertRaises(HTTPError) as denied:client.open(base+'/api/evidence?run='+self.path.name,timeout=10)
                self.assertEqual(denied.exception.code,401)
