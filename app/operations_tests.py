import copy,json,tempfile,time,unittest
from pathlib import Path
from unittest import mock
import operations_workspace as o
import operations_supervisor as supervisor
import named_access as access
import workflow_planner as planner
import change_coordinator as c
import durable_runtime as d
import simulator as sim
class Crash(BaseException):pass
class OperationsTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory(prefix='operations checks ');self.root=Path(self.tmp.name);self.paths=[]
    def tearDown(self):
        for p in self.paths:
            try:supervisor.stop(p)
            except (ValueError,OSError):pass
            try:
                with o.opened(p) as r:r.stop()
            except (ValueError,OSError):pass
        self.tmp.cleanup()
    def start(self,profile='balanced',auth=None):
        path=o.create(self.root,planner.profile(profile),ttl=300);self.paths.append(path)
        with o.opened(path,auth=auth) as r:value=r.advance()
        self.assertEqual(value['phase'],'AWAITING_BATCH_APPROVAL');return path
    def execute(self,path,principal=None,auth=None):
        with o.opened(path,auth=auth) as r:p,phrase=r.batch_proposal();return r.approve_batch(phrase,principal or o.fixture('manager'))
    def recover(self,path,fault='candidate'):
        self.execute(path)
        with o.opened(path) as r:
            r.observe();r.simulate_failure(fault);r.observe();r.observe();r.advance();_,phrase=r.recovery_proposal();return r.approve_recovery(phrase,o.fixture('manager'))
    def handoff(self,path):
        with o.opened(path) as r:
            p=r.failover_proposal();r.approve_failover(p['digest'],o.fixture('manager'),'manager');return r.approve_failover(p['digest'],o.fixture('owner_C'),'owner_C')
    def test_dead_candidate_two_party_handoff_preserves_jobs_then_releases_only_eligible_capacity(self):
        path=self.start();before=self.recover(path);self.assertEqual(before['service']['metrics']['committed'],60)
        value=self.handoff(path);e=value['operations']['emergency'];self.assertEqual(e['availability'],{'requests':20,'successful':20});self.assertEqual(value['service']['actual_at_outcome']['candidate_percent'],0)
        self.assertEqual(len(value['service']['policy_snapshot']['state']['deployments']['candidate-v2']['dependencies']),1)
        with o.opened(path) as r:
            p=r.job_proposal('C-mission-job');value=r.approve_job(p['digest'],'C-mission-job',o.fixture('owner_C'))
        self.assertEqual(value['phase'],'RECOVERED_SERVICE_ACTIVE');self.assertEqual(value['service']['metrics']['committed'],40);self.assertIn('deploy-A',value['service']['policy_snapshot']['state']['paused_runs']);self.assertEqual(o.verify(path)['status'],'CONSISTENT')
    def test_one_handoff_consent_cannot_change_the_route(self):
        path=self.start();self.recover(path)
        with o.opened(path) as r:
            p=r.failover_proposal();value=r.approve_failover(p['digest'],o.fixture('manager'),'manager')
        self.assertEqual(value['service']['actual_at_outcome']['candidate_percent'],20);self.assertEqual(value['phase'],'FAILOVER_AWAITING_CONSENT')
    def test_named_manager_cannot_approve_as_c_owner(self):
        auth=access.Access(self.root)
        try:
            admin=auth.create('admin','Long local password!',['admin','manager']);path=self.start(auth=auth);self.recover(path)
            with o.opened(path,auth=auth) as r:
                p=r.failover_proposal();r.approve_failover(p['digest'],admin,'manager')
                with self.assertRaises(access.AccessDenied):r.approve_failover(p['digest'],admin,'owner_C')
        finally:auth.close()
    def test_revoked_named_handoff_approval_blocks_execution(self):
        auth=access.Access(self.root)
        try:
            admin=auth.create('admin','Long local password!',['admin','manager','owner_C']);manager=auth.create('manager','Long local password!',['manager'],admin);path=self.start(auth=auth);self.recover(path)
            with o.opened(path,auth=auth) as r:
                p=r.failover_proposal();r.approve_failover(p['digest'],manager,'manager');auth.change(admin,manager['subject'],roles=['viewer'])
                with self.assertRaises(access.AccessDenied):r.approve_failover(p['digest'],admin,'owner_C')
            self.assertEqual(o.read(path)['service']['actual_at_outcome']['candidate_percent'],20)
        finally:auth.close()
    def test_unsafe_fallback_prevents_a_handoff_even_with_both_consents(self):
        path=self.start();self.recover(path)
        with o.opened(path) as r,r.broker() as b:
            b.engine.client.fail_worker('stable-v1');c.publish(r.child(),b)
        with o.opened(path) as r:
            p=r.failover_proposal();r.approve_failover(p['digest'],o.fixture('manager'),'manager')
            with self.assertRaises(sim.Denied):r.approve_failover(p['digest'],o.fixture('owner_C'),'owner_C')
        self.assertEqual(o.read(path)['service']['actual_at_outcome']['candidate_percent'],20)
    def test_handoff_client_death_after_effect_uses_exact_receipt_without_duplicate_route(self):
        path=self.start();self.recover(path)
        def crash(phase,key):
            if phase=='failover_effect':raise Crash()
        with o.opened(path,crash) as r:
            p=r.failover_proposal();r.approve_failover(p['digest'],o.fixture('manager'),'manager')
            with self.assertRaises(Crash):r.approve_failover(p['digest'],o.fixture('owner_C'),'owner_C')
        with o.opened(path) as r:value=r.advance()
        self.assertEqual(value['service']['actual_at_outcome']['candidate_percent'],0)
        receipts=[e for e in value['service']['policy_snapshot']['events'] if e.get('action_key','').startswith('ops-failover') and e.get('kind')=='ACTION'];self.assertEqual(len(receipts),1)
    def test_lost_handoff_reply_keeps_route_intent_until_exact_reconciliation(self):
        path=self.start();self.recover(path);original=d.Runtime.operate;lost=[]
        def operation(runtime,request):
            result=original(runtime,request)
            if request['tool']=='set_route' and request['percent']==0 and not lost:lost.append(True);raise OSError('MOCK lost after real route effect')
            return result
        with o.opened(path) as r:
            p=r.failover_proposal();r.approve_failover(p['digest'],o.fixture('manager'),'manager')
            with mock.patch.object(d.Runtime,'operate',operation):value=r.approve_failover(p['digest'],o.fixture('owner_C'),'owner_C')
            self.assertEqual(value['phase'],'FAILOVER_EXECUTING');self.assertTrue(value['service']['pending_external']);value=r.advance()
        self.assertEqual(value['phase'],'AWAITING_OWNER_REVIEW');self.assertEqual(value['service']['actual_at_outcome']['candidate_percent'],0)
    def test_release_one_job_cannot_delete_other_customers_dependencies(self):
        path=self.start('dependency-chain');self.recover(path);self.handoff(path)
        with o.opened(path) as r:
            p=r.job_proposal('pipeline-stage-0');value=r.approve_job(p['digest'],'pipeline-stage-0',o.fixture('owner_C'))
        jobs=value['service']['policy_snapshot']['state']['deployments']['candidate-v2']['dependencies'];self.assertEqual(len(jobs),9);self.assertEqual(value['service']['metrics']['committed'],60)
    def test_28_request_batch_uses_actual_policy_receipts_and_bounded_search(self):
        path=self.start('bulk');value=self.execute(path);plan=value['service']['plans'][-1]
        self.assertEqual(len(value['service']['intents']),28);self.assertEqual(plan['cursor'],len(plan['choice']['accepted']));self.assertFalse(plan['comparison']['global_optimum_proven']);self.assertLessEqual(plan['comparison']['orders_compared'],96)
        self.assertEqual(value['phase'],'MONITORING');self.assertLessEqual(value['service']['metrics']['committed'],70);self.assertEqual(o.verify(path)['status'],'CONSISTENT')
    def test_configurable_factory_cannot_edit_registered_graph_identity_or_fixed_budget(self):
        workflow=planner.profile();provider=o.ScriptedRole('factory');v=json.loads(provider.ask({'workflow':workflow})[0]);v['requests'][1]['parameters']={'limit':100}
        with self.assertRaises(ValueError):o.factory_requests(v,workflow)
        v=json.loads(provider.ask({'workflow':workflow})[0]);v['requests'][0]['actor']='C'
        with self.assertRaises(ValueError):o.factory_requests(v,workflow)
    def test_named_request_submission_invalidates_old_plan_and_requires_new_exact_review(self):
        auth=access.Access(self.root)
        try:
            actor=auth.create('admin','Long local password!',['admin','manager','traffic']);path=self.start(auth=auth)
            with o.opened(path,auth=auth) as r:
                old,phrase=r.batch_proposal();r.submit_request('C',{'id':'new-job','tool':'add_dependency','parameters':{'dependency_id':'extra-job'},'priority':3,'after':['customer-job']},actor)
                with self.assertRaises(sim.Denied):r.approve_batch(phrase,actor)
                self.assertNotEqual(r.state()['plan_id'],old['id'])
                with self.assertRaises(access.AccessDenied):r.submit_request('A',{'id':'other','tool':'reserve_capacity','parameters':{'units':1},'priority':1,'after':[]},actor)
        finally:auth.close()
    def test_background_supervisor_measures_incident_prepares_review_and_never_approves(self):
        path=self.start();self.execute(path);before=o.read(path)['service']['runtime_id'];first=supervisor.launch(path,1);second=supervisor.launch(path,1);self.assertTrue(second['reused'])
        with o.opened(path) as r:r.simulate_failure('candidate')
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            value=o.read(path)
            if value['phase']=='AWAITING_RECOVERY_APPROVAL':break
            time.sleep(.2)
        self.assertEqual(value['phase'],'AWAITING_RECOVERY_APPROVAL');self.assertNotIn('recovery_grant',value);self.assertEqual(value['service']['metrics']['committed'],65);self.assertEqual(value['model_calls'],0);self.assertEqual(value['service']['runtime_id'],before);self.assertEqual(supervisor.stop(path)['status'],'STOPPED')
        a=o.sessions.read_file(path,'.supervisor-access.json');facts=d.os_process(a['pid'],a['os_identity']);self.assertTrue(not facts['exists'] or facts['identity']!=a['os_identity'] or not facts['alive'])
        log=path/'operations-supervisor.log';closed=path/'operations-supervisor.closed';log.rename(closed);closed.rename(log)
    def test_source_change_blocks_effects_but_owned_teardown_still_works(self):
        path=self.start()
        with o.opened(path) as r:
            with mock.patch.object(o,'source_digest',return_value='changed'):
                with self.assertRaises(sim.Denied):r.advance()
            self.assertEqual(r.stop()['lifecycle'],'STOPPED')
if __name__=='__main__':unittest.main()
