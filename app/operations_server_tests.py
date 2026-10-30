import json,tempfile,threading,time,unittest
from pathlib import Path
from urllib.request import Request,urlopen,build_opener,HTTPCookieProcessor
from urllib.error import HTTPError
from http.cookiejar import CookieJar
from unittest import mock
import operations_server as s
import operations_workspace as o
import named_access
from types import SimpleNamespace
class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='ops web ');self.root=Path(self.tmp.name);self.server=s.Server(self.root);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.base='http://127.0.0.1:'+str(self.server.server_port);self.client=build_opener(HTTPCookieProcessor(CookieJar()));self.csrf=''
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(5)
        for path in self.root.iterdir():
            if path.is_dir() and not path.name.startswith('.'):
                try:
                    with o.opened(path) as run:run.stop()
                except (ValueError,OSError):pass
        self.tmp.cleanup()
    def get(self,path='/api/state',client=None):
        with (client or self.client).open(self.base+path,timeout=30) as response:return json.load(response)
    def post(self,path,body,client=None,csrf=None,origin=None):
        headers={'Content-Type':'application/json','Origin':origin or self.base,'X-CSRF-Token':self.csrf if csrf is None else csrf}
        with (client or self.client).open(Request(self.base+path,data=json.dumps(body).encode(),method='POST',headers=headers),timeout=40) as response:return json.load(response)
    def setup(self):
        value=self.post('/api/setup',{'name':'william','password':'Long local password!','bootstrap':self.server.bootstrap});self.csrf=value['csrf'];return value['principal']
    def test_session_timing_exposes_only_the_current_deadline_without_renewal(self):
        self.setup()
        auth=named_access.Access(self.root)
        try:
            stored=auth.db.execute('SELECT token,expires FROM sessions').fetchone()
        finally:auth.close()
        first=self.get();second=self.get()
        self.assertEqual(set(first['session']),{'expires_unix','server_now_unix','duration_seconds','renewed_by_reads'})
        self.assertEqual(first['session']['expires_unix'],stored[1])
        self.assertEqual(second['session']['expires_unix'],stored[1])
        self.assertEqual(first['session']['duration_seconds'],1800)
        self.assertFalse(first['session']['renewed_by_reads'])
        self.assertNotIn(stored[0],json.dumps(first))
        self.assertNotIn('Long local password!',json.dumps(first))
    def test_login_and_workflow_deadlines_are_distinct_and_state_reads_extend_neither(self):
        self.setup();run=self.post('/api/action',{'action':'create','duration_minutes':60})['run']
        before=o.read(self.root/run)['expires_unix'];state=self.get()
        self.assertLess(state['session']['expires_unix'],before)
        remaining=state['session']['expires_unix']-state['session']['server_now_unix']
        self.assertGreater(remaining,1700);self.assertLessEqual(remaining,1800)
        self.get();self.assertEqual(o.read(self.root/run)['expires_unix'],before)
        self.assertEqual(self.get()['session']['expires_unix'],state['session']['expires_unix'])
        self.post('/api/action',{'action':'stop','run':run})
    def test_expired_login_cannot_obtain_session_metadata_or_reopen_a_saved_workflow(self):
        self.setup();run=self.post('/api/action',{'action':'create'})['run'];before=(self.root/run/'summary.json').read_bytes()
        auth=named_access.Access(self.root)
        try:auth.db.execute('UPDATE sessions SET expires=0')
        finally:auth.close()
        with self.assertRaises(HTTPError) as caught:self.get()
        self.assertEqual(caught.exception.code,401)
        self.assertNotIn('session',json.load(caught.exception))
        self.assertEqual((self.root/run/'summary.json').read_bytes(),before)
    def test_new_browser_run_defaults_to_one_hour_and_reads_do_not_renew_it(self):
        self.setup();run=self.post('/api/action',{'action':'create'})['run'];path=self.root/run
        saved=o.read(path);self.assertEqual(saved['ttl'],3600);deadline=saved['expires_unix']
        for _ in range(2):self.get()
        self.assertEqual(o.read(path)['expires_unix'],deadline)
        self.assertEqual(self.post('/api/action',{'action':'stop','run':run})['phase'],o.read(path)['phase'])
        self.assertTrue(o.read(path)['teardown']['all_workers_stopped'])
    def test_selected_durations_bind_parent_requests_and_workers_without_extension(self):
        self.setup()
        for minutes in (15,30,60):
            with self.subTest(minutes=minutes):
                run=self.post('/api/action',{'action':'create','duration_minutes':minutes})['run'];path=self.root/run
                saved=o.read(path);self.assertEqual(saved['ttl'],minutes*60)
                child=o.sessions.read_file(path/'service'/saved['child'],'session.json')
                self.assertLessEqual(child['expires_unix'],saved['expires_unix'])
                self.assertTrue(all(i['expires_unix']<=saved['expires_unix'] for i in saved['service']['intents']))
                self.post('/api/action',{'action':'stop','run':run})
                self.assertTrue(o.read(path)['teardown']['all_workers_stopped'])
    def test_invalid_duration_is_rejected_before_creating_any_run(self):
        self.setup()
        with mock.patch.object(o,'create',side_effect=AssertionError('No run may start for invalid duration')):
            for minutes in (None,0,14,61,900,'60',True,30.0,{},[]):
                with self.subTest(minutes=minutes),self.assertRaises(HTTPError) as caught:
                    self.post('/api/action',{'action':'create','duration_minutes':minutes})
                self.assertEqual(caught.exception.code,400)
        self.assertEqual(self.get()['runs'],[])
    def test_hour_run_accepts_review_after_fifteen_minutes_but_blocks_at_its_original_deadline(self):
        self.setup();run=self.post('/api/action',{'action':'create'})['run'];path=self.root/run;saved=o.read(path)
        with mock.patch.object(o.m,'time',SimpleNamespace(time=lambda:saved['expires_unix']-2699)):
            self.assertIn('digest',self.post('/api/review',{'run':run,'kind':'batch'}))
        with mock.patch.object(o.m,'time',SimpleNamespace(time=lambda:saved['expires_unix']+1)):
            with self.assertRaises(HTTPError) as caught:self.post('/api/review',{'run':run,'kind':'batch'})
            self.assertEqual(caught.exception.code,410)
        self.assertEqual(o.read(path)['expires_unix'],saved['expires_unix'])
    def test_first_setup_session_cookie_and_private_files_are_not_exposed(self):
        with self.assertRaises(HTTPError):self.get()
        self.setup();state=self.get();self.assertEqual(state['principal']['name'],'william')
        for path in ('/.control_access/accounts.sqlite3','/operations_runs/other/mission.sqlite3','/api/missing'):
            with self.assertRaises(HTTPError):self.get(path)
        with self.assertRaises(HTTPError):self.post('/api/setup',{'name':'other','password':'Long local password!','bootstrap':'wrong'})
        self.post('/api/logout',{})
        with self.assertRaises(HTTPError):self.get()
    def test_origin_csrf_and_viewer_roles_cannot_create_or_approve(self):
        self.setup()
        with self.assertRaises(HTTPError) as caught:self.post('/api/action',{'action':'create'},csrf='wrong')
        self.assertEqual(caught.exception.code,403)
        with self.assertRaises(HTTPError):self.post('/api/action',{'action':'create'},origin='https://other.example')
        self.post('/api/account',{'name':'viewer','password':'Long local password!','roles':['viewer']})
        client=build_opener(HTTPCookieProcessor(CookieJar()));logged=self.post('/api/login',{'name':'viewer','password':'Long local password!'},client,csrf='');
        with self.assertRaises(HTTPError):self.post('/api/action',{'action':'create'},client,csrf=logged['csrf'])
        self.assertEqual(self.get()['runs'],[])
    def test_real_browser_api_batch_approval_holds_exact_snapshot_and_named_actor(self):
        self.setup();created=self.post('/api/action',{'action':'create','profile':'balanced'});run=created['run'];review=self.post('/api/review',{'run':run,'kind':'batch'})
        with self.assertRaises(HTTPError):self.post('/api/action',{'action':'approve-review','run':run,'kind':'batch','digest':'old','phrase':review['phrase']})
        self.assertEqual(o.read(self.root/run)['service']['metrics']['committed'],60)
        self.post('/api/action',{'action':'approve-review','run':run,'kind':'batch','digest':review['digest'],'phrase':review['phrase']})
        value=o.read(self.root/run);self.assertEqual(value['phase'],'MONITORING');self.assertEqual(value['batch_consent']['principal']['name'],'william')
        task=self.post('/api/infer',{'run':run,'id':'web-task','text':'Urgent route timeout'});self.assertEqual(task['status'],'SUCCEEDED');self.assertEqual(self.get()['inference_enabled'],False)
    def test_expired_review_returns_json_denial_without_effect_or_lease_renewal(self):
        self.setup();run=self.post('/api/action',{'action':'create'})['run'];path=self.root/run
        before=(path/'summary.json').read_bytes();saved=o.read(path)
        with mock.patch.object(o.m,'time',SimpleNamespace(time=lambda:saved['expires_unix']+1)),mock.patch.object(o.durable.Runtime,'state',side_effect=AssertionError('No runtime effect/read for an expired review')):
            requests=[('/api/review',{'run':run,'kind':'batch'}),
                ('/api/review',{'run':run,'kind':'failover-manager'}),
                ('/api/action',{'action':'approve-review','run':run,'kind':'failover-manager','digest':'old'})]
            for endpoint,body in requests:
                with self.subTest(endpoint=endpoint,kind=body['kind']):
                    with self.assertRaises(HTTPError) as caught:self.post(endpoint,body)
                    self.assertEqual(caught.exception.code,410)
                    value=json.load(caught.exception);self.assertEqual(value['code'],'MISSION_EXPIRED_OR_STOPPED')
                    self.assertIn('expiry',value['error'])
        self.assertEqual((path/'summary.json').read_bytes(),before)
        self.assertEqual(o.read(path)['expires_unix'],saved['expires_unix'])
        self.assertEqual(o.read(path)['service']['policy_snapshot'],saved['service']['policy_snapshot'])
    def test_policy_conflict_returns_its_reason_without_changing_the_plan(self):
        self.setup();run=self.post('/api/action',{'action':'create'})['run'];path=self.root/run
        before=(path/'summary.json').read_bytes()
        with mock.patch.object(o.Mission,'batch_proposal',side_effect=o.sim.Denied('BATCH_STATE_CHANGED','The batch state changed. Open a fresh review.')):
            with self.assertRaises(HTTPError) as caught:self.post('/api/review',{'run':run,'kind':'batch'})
        self.assertEqual(caught.exception.code,409)
        value=json.load(caught.exception);self.assertEqual(value,{'code':'BATCH_STATE_CHANGED','error':'The batch state changed. Open a fresh review.'})
        self.assertEqual((path/'summary.json').read_bytes(),before)
    def test_stopped_run_denies_effects_as_json_but_keeps_saved_evidence_readable(self):
        self.setup();run=self.post('/api/action',{'action':'create'})['run'];path=self.root/run
        self.post('/api/action',{'action':'stop','run':run});before=(path/'summary.json').read_bytes()
        with self.assertRaises(HTTPError) as caught:self.post('/api/review',{'run':run,'kind':'batch'})
        self.assertEqual(caught.exception.code,410)
        self.assertEqual(json.load(caught.exception)['code'],'MISSION_EXPIRED_OR_STOPPED')
        saved=next(r for r in self.get()['runs'] if r['id']==run)['summary']
        self.assertEqual(saved['lifecycle'],'STOPPED');self.assertEqual((path/'summary.json').read_bytes(),before)
    def test_account_role_revocation_applies_to_active_browser_sessions(self):
        self.setup();user=self.post('/api/account',{'name':'manager','password':'Long local password!','roles':['manager']})['user']
        client=build_opener(HTTPCookieProcessor(CookieJar()));logged=self.post('/api/login',{'name':'manager','password':'Long local password!'},client,csrf='')
        self.post('/api/account-change',{'subject':user['subject'],'roles':['viewer']})
        with self.assertRaises(HTTPError):self.post('/api/action',{'action':'create'},client,csrf=logged['csrf'])
    def test_client_cannot_inject_live_flags_custom_tools_or_other_output_paths(self):
        self.setup()
        for extra in ({'live':True},{'output':'../outside'},{'action':'shell'},{'enable_inference':True}):
            with self.assertRaises(HTTPError):self.post('/api/action',{'action':'create',**extra})
        self.assertEqual(self.get()['runs'],[])
    def test_separate_named_browser_approvals_handoff_dead_candidate_and_preserve_customer_job(self):
        self.setup()
        self.post('/api/account',{'name':'operator','password':'Long local password!','roles':['manager','viewer']})
        self.post('/api/account',{'name':'customer','password':'Long local password!','roles':['owner_C','viewer']})
        manager=build_opener(HTTPCookieProcessor(CookieJar()));owner=build_opener(HTTPCookieProcessor(CookieJar()))
        mt=self.post('/api/login',{'name':'operator','password':'Long local password!'},manager,csrf='')['csrf']
        ct=self.post('/api/login',{'name':'customer','password':'Long local password!'},owner,csrf='')['csrf']
        run=self.post('/api/action',{'action':'create'},manager,csrf=mt)['run']
        def approve(kind,client,csrf,**extra):
            review=self.post('/api/review',{'run':run,'kind':kind,**extra},client,csrf=csrf)
            return self.post('/api/action',{'action':'approve-review','run':run,'kind':kind,'digest':review['digest'],'phrase':review['phrase'],**extra},client,csrf=csrf)
        approve('batch',manager,mt);approve('fault-candidate',manager,mt)
        auth=named_access.Access(self.root)
        try:
            with o.opened(self.root/run,auth=auth) as mission:mission.observe();mission.observe();mission.advance()
        finally:auth.close()
        approve('recovery',manager,mt)
        with self.assertRaises(HTTPError):self.post('/api/review',{'run':run,'kind':'failover-owner_C'},manager,csrf=mt)
        approve('failover-manager',manager,mt)
        self.assertEqual(o.read(self.root/run)['service']['actual_at_outcome']['candidate_percent'],20)
        approve('failover-owner_C',owner,ct)
        saved=o.read(self.root/run);emergency=saved['operations']['emergency']
        self.assertEqual(emergency['availability'],{'requests':20,'successful':20})
        self.assertNotEqual(emergency['grants']['manager']['principal']['subject'],emergency['grants']['owner_C']['principal']['subject'])
        self.assertEqual(saved['service']['policy_snapshot']['state']['deployments']['candidate-v2']['dependencies'][0]['id'],'C-mission-job')
        approve('job',owner,ct,job='C-mission-job')
        self.assertEqual(o.read(self.root/run)['service']['metrics']['committed'],40)
        self.assertEqual(o.verify(self.root/run)['status'],'CONSISTENT')
    def test_outcome_reports_require_login_and_viewers_can_read_without_effects(self):
        path,_=o.demo(self.root)
        before=(path/'summary.json').read_bytes()
        with self.assertRaises(HTTPError) as caught:self.get('/api/outcome?run='+path.name)
        self.assertEqual(caught.exception.code,401)
        self.setup();self.post('/api/account',{'name':'report-viewer','password':'Long local password!','roles':['viewer']})
        viewer=build_opener(HTTPCookieProcessor(CookieJar()))
        self.post('/api/login',{'name':'report-viewer','password':'Long local password!'},viewer,csrf='')
        report=self.get('/api/outcome?run='+path.name,viewer)
        self.assertEqual(report['run'],path.name);self.assertEqual(report['model_calls_to_build_report'],0)
        with viewer.open(self.base+'/outcome?run='+path.name,timeout=30) as response:
            page=response.read().decode();self.assertIn('text/html',response.headers['Content-Type'])
            self.assertIn('What this rehearsal demonstrated',page);self.assertNotIn('<script',page)
        self.assertEqual((path/'summary.json').read_bytes(),before)
        self.assertFalse((path/'outcome_report.json').exists())
    def test_outcome_query_rejects_paths_duplicates_and_unregistered_options(self):
        self.setup()
        for query in ('','run=','run=../outside','run=%2Ftmp','run=a&run=b','run=a&extra=b','run=a&baseline=b&extra=c'):
            with self.subTest(query=query):
                with self.assertRaises(HTTPError) as caught:self.get('/api/outcome?'+query)
                self.assertEqual(caught.exception.code,400)
    def test_outcome_api_comparison_and_account_revocation(self):
        self.setup();first,_=o.demo(self.root);second,_=o.demo(self.root)
        report=self.get('/api/outcome?run='+second.name+'&baseline='+first.name)
        self.assertEqual(report['comparison']['status'],'DESCRIPTIVE_COMPARISON')
        user=self.post('/api/account',{'name':'revocable-viewer','password':'Long local password!','roles':['viewer']})['user']
        viewer=build_opener(HTTPCookieProcessor(CookieJar()))
        self.post('/api/login',{'name':'revocable-viewer','password':'Long local password!'},viewer,csrf='')
        self.post('/api/account-change',{'subject':user['subject'],'active':False})
        with self.assertRaises(HTTPError) as caught:self.get('/api/outcome?run='+first.name,viewer)
        self.assertEqual(caught.exception.code,401)
if __name__=='__main__':unittest.main()
