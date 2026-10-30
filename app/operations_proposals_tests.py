"""Live-path tests use explicitly fake providers and spend no inference credits."""
import json,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.error import HTTPError
from urllib.request import build_opener,HTTPCookieProcessor
from http.cookiejar import CookieJar
import operations_workspace as ops
import operations_server as server
import operations_supervisor as observer
import operations_proposals as jobs
import named_access
from operations_server_tests import ServerTests

class FakeLive(ops.ScriptedRole):
    live=True
    def __init__(self,role):
        super().__init__(role);self.request_settings=ops.factory.inference.inference_settings(2048,False);self.calls=[];self.hook=None
    def ask(self,context):
        self.calls.append(context)
        if self.hook:self.hook()
        content,meta=super().ask(context)
        return content,{**meta,'total_tokens':100,'source':'FAKE LIVE PROVIDER FOR OFFLINE TESTS'}

class ProposalTests(unittest.TestCase):
    get=ServerTests.get;post=ServerTests.post;setup=ServerTests.setup
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='live proposal tests ');self.root=Path(self.tmp.name)
        self.providers={r:FakeLive(r) for r in ops.ROLES};self.release=threading.Event()
        self.server=server.Server(self.root,proposal_providers=self.providers)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port);self.client=build_opener(HTTPCookieProcessor(CookieJar()));self.csrf=''
        self.principal=self.setup()
    def tearDown(self):
        self.release.set()
        for t in self.server.proposals.threads:t.join(15)
        ServerTests.tearDown(self)
    def create(self):return self.post('/api/action',{'action':'create','proposal_mode':'live'})['run']
    def review(self,run,role):return self.post('/api/review',{'run':run,'kind':'proposal-'+role})
    def approve(self,run,role,p):return self.post('/api/action',{'action':'approve-review','run':run,'kind':'proposal-'+role,'digest':p['digest'],'phrase':None})
    def settled(self,run):
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            value=self.server.proposals.snapshot()[run]
            if value['status'] not in {'QUEUED','RUNNING'}:return value
            time.sleep(.02)
        self.fail('Proposal job did not settle within its test deadline.')
    def one(self,run,role):
        p=self.review(run,role);self.approve(run,role,p);j=self.settled(run);self.assertEqual(j['status'],'FINISHED',j);return ops.read(self.root/run)
    def effect(self,run,kind,**extra):
        p=self.post('/api/review',{'run':run,'kind':kind,**extra})
        return self.post('/api/action',{'action':'approve-review','run':run,'kind':kind,'digest':p['digest'],'phrase':p['phrase'],**extra})
    def test_three_roles_require_separate_consent_then_existing_exact_effect_reviews(self):
        run=self.create();s=ops.read(self.root/run);self.assertEqual(s['phase'],'DRAFT');self.assertEqual(s['calls'],[])
        s=self.one(run,'factory');self.assertEqual(s['phase'],'QUEUE_READY');self.assertEqual(s['model_calls'],1);self.assertEqual(self.providers['governance'].calls,[])
        s=self.one(run,'governance');self.assertEqual(s['phase'],'AWAITING_BATCH_APPROVAL');self.assertEqual(s['service']['metrics']['committed'],60)
        self.effect(run,'batch');self.assertEqual(ops.read(self.root/run)['service']['metrics']['committed'],65)
        self.effect(run,'fault-candidate')
        with ops.opened(self.root/run) as r:r.observe();r.observe()
        observer.step(self.root/run);self.assertEqual(self.providers['recovery'].calls,[])
        s=self.one(run,'recovery');self.assertEqual(s['phase'],'AWAITING_RECOVERY_APPROVAL');self.assertEqual(s['model_calls'],3)
        self.effect(run,'recovery');self.effect(run,'failover-manager');self.effect(run,'failover-owner_C')
        self.assertEqual(ops.read(self.root/run)['service']['metrics']['committed'],60)
        self.effect(run,'job',job='C-mission-job');s=ops.read(self.root/run)
        self.assertEqual(s['service']['metrics']['committed'],40);self.assertEqual(s['usage']['reported_tokens'],300)
        self.assertEqual(len([e for e in s['events'] if e['kind']=='PAID_PROPOSAL_CONSENT_RECORDED']),3)
        self.assertEqual(ops.verify(self.root/run)['status'],'CONSISTENT')
        self.assertEqual([len(p.calls) for p in self.providers.values()],[1,1,1])
    def test_duplicate_click_and_polling_cannot_repeat_inflight_call(self):
        entered=threading.Event()
        def hold():entered.set();self.assertTrue(self.release.wait(10))
        self.providers['factory'].hook=hold;run=self.create();p=self.review(run,'factory');first=self.approve(run,'factory',p)
        self.assertTrue(entered.wait(5));s=ops.read(self.root/run);self.assertEqual(s['calls'][0]['status'],'REQUEST_STARTED')
        self.assertEqual(observer.step(self.root/run),'FACTORY_REQUESTING')
        second=self.approve(run,'factory',p);self.assertEqual(first['job']['id'],second['job']['id'])
        self.assertEqual(self.get()['proposal_jobs'][run]['status'],'RUNNING');self.assertEqual(len(self.providers['factory'].calls),1)
        self.release.set();self.settled(run);third=self.approve(run,'factory',p)
        self.assertEqual(first['job']['id'],third['job']['id']);self.assertEqual(len(self.providers['factory'].calls),1)
    def test_stale_review_is_rejected_before_provider_attempt(self):
        run=self.create();p=self.review(run,'factory');p['digest']='0'*64
        with self.assertRaises(HTTPError) as caught:self.approve(run,'factory',p)
        self.assertEqual(caught.exception.code,409);self.assertEqual(ops.read(self.root/run)['calls'],[])
    def test_viewer_cannot_create_live_workflow_or_authorize_proposals(self):
        run=self.create();self.post('/api/account',{'name':'read-only','password':'Long local password!','roles':['viewer']})
        client=build_opener(HTTPCookieProcessor(CookieJar()));csrf=self.post('/api/login',{'name':'read-only','password':'Long local password!'},client,csrf='')['csrf']
        for endpoint,body in [('/api/action',{'action':'create','proposal_mode':'live'}),('/api/review',{'run':run,'kind':'proposal-factory'})]:
            with self.assertRaises(HTTPError) as caught:self.post(endpoint,body,client,csrf=csrf)
            self.assertEqual(caught.exception.code,403)
        self.assertEqual(self.providers['factory'].calls,[])
    def test_disabled_workspace_rejects_live_creation(self):
        self.server.proposals=jobs.Jobs(self.root)
        with self.assertRaises(HTTPError):self.create()
        self.assertEqual(self.get()['runs'],[]);self.assertFalse(self.get()['live_proposals_enabled'])
    def test_judge_sees_owner_funded_access_without_a_key_or_startup_request(self):
        for provider in self.providers.values():provider.private_key='PRIVATE_OWNER_TEST_VALUE'
        state=self.get();access=state['proposal_access']
        self.assertTrue(access['available']);self.assertEqual(access['status'],'CONFIGURED')
        self.assertFalse(access['judge_key_required']);self.assertEqual(access['funded_by'],'WORKSPACE_OWNER')
        self.assertNotIn('PRIVATE_OWNER_TEST_VALUE',json.dumps(state))
        run=self.create();self.assertEqual(ops.read(self.root/run)['phase'],'DRAFT')
        self.assertEqual(ops.read(self.root/run)['calls'],[])
        self.assertTrue(all(not provider.calls for provider in self.providers.values()))
    def test_browser_rejects_ambiguous_key_fields_outside_nvidia_form(self):
        for field in ('api_key','NEBIUS_API_KEY','key'):
            with self.assertRaises(HTTPError) as caught:
                self.post('/api/action',{'action':'create','proposal_mode':'live',field:'PRIVATE_BROWSER_TEST_VALUE'})
            self.assertEqual(caught.exception.code,400)
        self.assertEqual(self.get()['runs'],[])
        self.assertTrue(all(not provider.calls for provider in self.providers.values()))
    def test_expired_review_cannot_start_an_attempt(self):
        run=self.create();p=self.review(run,'factory');s=ops.read(self.root/run)
        with mock.patch.object(ops.m,'time',SimpleNamespace(time=lambda:s['expires_unix']+1)):
            with self.assertRaises(HTTPError) as caught:self.approve(run,'factory',p)
            self.assertEqual(caught.exception.code,410)
        self.assertEqual(self.providers['factory'].calls,[]);self.assertEqual(ops.read(self.root/run)['calls'],[])
    def test_provider_failure_is_recorded_without_leaking_error_text_or_retry(self):
        def fail():raise RuntimeError('SECRET_PROVIDER_KEY_TEST_DO_NOT_EXPOSE')
        self.providers['factory'].hook=fail;run=self.create();p=self.review(run,'factory');self.approve(run,'factory',p);job=self.settled(run)
        self.assertEqual(job['status'],'FAILED');self.assertNotIn('SECRET_PROVIDER_KEY_TEST_DO_NOT_EXPOSE',json.dumps(self.get()))
        self.server.proposals=jobs.Jobs(self.root,self.providers)
        with self.assertRaises(HTTPError):self.review(run,'factory')
        self.post('/api/action',{'action':'advance','run':run});s=ops.read(self.root/run)
        self.assertEqual(s['calls'][0]['status'],'ERROR');self.assertEqual(len(self.providers['factory'].calls),1);self.assertEqual(s['usage']['missing_usage_calls'],1)
    def test_permission_revocation_is_rechecked_inside_background_job(self):
        entered=threading.Event();original=ops.Mission.request_proposal
        def held(r,*args):entered.set();self.assertTrue(self.release.wait(10));return original(r,*args)
        run=self.create();p=self.review(run,'factory')
        with mock.patch.object(ops.Mission,'request_proposal',held):
            self.approve(run,'factory',p);self.assertTrue(entered.wait(5))
            self.post('/api/account-change',{'subject':self.principal['subject'],'roles':['admin','viewer']})
            self.release.set();self.assertEqual(self.settled(run)['status'],'FAILED')
        self.assertEqual(ops.read(self.root/run)['calls'],[]);self.assertEqual(self.providers['factory'].calls,[])
    def test_changed_provider_settings_fail_before_call(self):
        run=self.create();p=self.review(run,'factory');self.providers['factory'].request_settings={'changed':True}
        self.approve(run,'factory',p);self.assertEqual(self.settled(run)['status'],'FAILED');self.assertEqual(ops.read(self.root/run)['calls'],[])
    def test_started_attempt_after_client_death_is_never_reissued(self):
        run=self.create();p=self.review(run,'factory');auth=named_access.Access(self.root)
        def crash(point,_):
            if point=='role_started':raise SystemExit('MODELLED process interruption')
        try:
            with self.assertRaises(SystemExit),ops.opened(self.root/run,checkpoint=crash,auth=auth) as r:r.request_proposal('factory',p['digest'],self.principal,self.providers['factory'])
        finally:auth.close()
        self.post('/api/action',{'action':'advance','run':run});s=ops.read(self.root/run)
        self.assertEqual(s['calls'][0]['status'],'OUTCOME_UNKNOWN');self.assertEqual(self.providers['factory'].calls,[])
        with self.assertRaises(HTTPError):self.review(run,'factory')
    def test_received_reply_after_client_death_can_finish_without_another_call(self):
        run=self.create();p=self.review(run,'factory');auth=named_access.Access(self.root)
        def crash(point,_):
            if point=='role_received':raise SystemExit('MODELLED process interruption')
        try:
            with self.assertRaises(SystemExit),ops.opened(self.root/run,checkpoint=crash,auth=auth) as r:r.request_proposal('factory',p['digest'],self.principal,self.providers['factory'])
        finally:auth.close()
        self.post('/api/action',{'action':'advance','run':run});s=ops.read(self.root/run)
        self.assertEqual(s['phase'],'QUEUE_READY');self.assertEqual(len(self.providers['factory'].calls),1);self.assertEqual(self.providers['governance'].calls,[])
    def test_lease_expiry_during_response_retains_attempt_without_starting_workers(self):
        run=self.create();p=self.review(run,'factory');s=ops.read(self.root/run);late=[False];auth=named_access.Access(self.root)
        self.providers['factory'].hook=lambda:late.__setitem__(0,True)
        try:
            with mock.patch.object(ops.m,'time',SimpleNamespace(time=lambda:s['expires_unix']+1 if late[0] else s['expires_unix']-300)):
                with self.assertRaises(ops.sim.Denied),ops.opened(self.root/run,auth=auth) as r:r.request_proposal('factory',p['digest'],self.principal,self.providers['factory'])
        finally:auth.close()
        s=ops.read(self.root/run);self.assertIsNone(s['child']);self.assertEqual(s['calls'][0]['status'],'ERROR');self.assertEqual(len(self.providers['factory'].calls),1)
    def test_permission_revoked_during_model_response_prevents_worker_initialization(self):
        def revoke():
            a=named_access.Access(self.root)
            try:a.change(self.principal,self.principal['subject'],roles=['admin','viewer'])
            finally:a.close()
        self.providers['factory'].hook=revoke;run=self.create();p=self.review(run,'factory');self.approve(run,'factory',p)
        self.assertEqual(self.settled(run)['status'],'FAILED');s=ops.read(self.root/run)
        self.assertIsNone(s['child']);self.assertEqual(s['calls'][0]['status'],'RECEIVED');self.assertEqual(len(self.providers['factory'].calls),1)
    def test_worker_start_failure_leaves_no_attempt_and_can_close_cleanly(self):
        run=self.create();p=self.review(run,'factory');auth=named_access.Access(self.root)
        try:
            with mock.patch.object(jobs.threading.Thread,'start',side_effect=RuntimeError('MODELLED thread start failure')):
                with self.assertRaises(ValueError):self.server.proposals.submit(self.root/run,'factory',p['digest'],self.principal,auth)
        finally:auth.close()
        self.assertEqual(ops.read(self.root/run)['calls'],[]);self.assertEqual(self.providers['factory'].calls,[])
        self.server.proposals.close()

if __name__=='__main__':unittest.main()
