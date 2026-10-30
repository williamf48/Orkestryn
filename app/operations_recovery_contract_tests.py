"""Recovery boundary regressions: policy copies and fake transports, zero inference."""
import copy,json,tempfile,time,unittest
from pathlib import Path
import named_access
import operations_workspace as ops
import simulator as sim
from operations_factory_tests import Replay
from operations_contract_tests import GovernanceReplay

DATA=Path(__file__).with_name('recovery_deferral_regression.json')

class RecoveryReplay(ops.ScriptedRole):
    live=True
    def __init__(self,decision='recover'):
        super().__init__('recovery');self.decision=decision;self.calls=[]
        self.request_settings=ops.factory.inference.inference_settings(2048,False)
    def ask(self,context):
        self.calls.append(copy.deepcopy(context))
        value={'schema_version':ops.m.RECOVERY_SCHEMA,'decision':self.decision,
            'based_on_incident':context['based_on_incident'],
            'reason':'Offline synthetic reply: recommend only bounded A cleanup and preserve customer work pending separate reviews.'}
        return sim.canonical(value),{'finish_reason':'stop','refused':False,'source':'OFFLINE FAKE RECOVERY REPLY; NO PROVIDER REQUEST','total_tokens':0}

class RecoveryContractTests(unittest.TestCase):
    def setUp(self):self.data=json.loads(DATA.read_text())
    def observe_settled(self,run):
        # A confirmed native exit can change from a signalled handle to a
        # missing process. Discard that window exactly as the observer does.
        deadline=time.monotonic()+5
        while True:
            try:return run.observe()
            except ValueError as exc:
                if str(exc)!='Service changed during the observation; discard this window.' or time.monotonic()>=deadline:raise
                time.sleep(.05)
    def test_original_deferral_is_still_valid_and_stays_unchanged(self):
        value=copy.deepcopy(self.data['recovery_reply']);raw=sim.canonical(value)
        self.assertEqual(ops.m.parse_recovery(raw,value['based_on_incident'])['decision'],'defer')
        self.assertEqual(self.data['recovery_reply'],value)
        with self.assertRaises(ValueError):ops.m.parse_recovery(raw,'other-incident')
    def test_saved_budget_state_allows_pause_and_protected_review_outcome(self):
        before=self.data['state'];snapshot=copy.deepcopy(before);engine=sim.Engine(':memory:')
        try:
            engine.db.execute('UPDATE service_state SET payload=? WHERE id=1',(sim.canonical(before),))
            result=engine.recover();after=engine.state()
            expected=ops.m.reference.reference_recovery(before);assessment=ops.m.reference.assess(before,after,expected)
            self.assertEqual(result['status'],'RECOVERY_REVIEW_REQUIRED')
            self.assertTrue(all(assessment['checks'].values()));self.assertEqual(assessment['violations'],[])
            self.assertEqual(after['budget_version'],2);self.assertEqual(after['capacity_limit'],70)
            self.assertEqual(after['routing'],before['routing']);self.assertEqual(after['route_owner'],'route-C')
            self.assertEqual(after['deployments']['candidate-v2']['dependencies'],before['deployments']['candidate-v2']['dependencies'])
            self.assertEqual(after['deployments']['candidate-v2']['allocated_units'],20)
            self.assertIn('deploy-A',after['paused_runs']);self.assertEqual(sim.total_commitments(after),60)
            self.assertEqual(before,snapshot)
        finally:engine.close()
    def test_separate_recovery_scope_still_rejects_revoked_expired_or_wrong_actor_approval(self):
        engine=sim.Engine(':memory:');state=self.data['state']
        try:
            # No effect: inspect the pinned approval guard on copied states only.
            engine._approval(state,'RECOVERY','deploy-A','AP-recovery-A','release_capacity')
            with self.assertRaises(sim.Denied):engine._approval(state,'A','deploy-A','AP-deploy-A','reserve_capacity')
            for field,value in [('revoked',True),('expires_at',0),('actor','A'),('run_id','route-C')]:
                changed=copy.deepcopy(state);changed['approvals']['AP-recovery-A'][field]=value
                with self.assertRaises(sim.Denied):engine._approval(changed,'RECOVERY','deploy-A','AP-recovery-A','release_capacity')
        finally:engine.close()
    def test_reviewed_recovery_recommends_before_consent_and_preserves_every_gate(self):
        with tempfile.TemporaryDirectory(prefix='recovery recommendation boundary ') as directory:
            root=Path(directory);auth=named_access.Access(root);path=None
            try:
                principal=auth.create('local-manager','Local test password only!',sorted(named_access.ROLES))
                factory=Replay(self.data['factory_reply']);governance=GovernanceReplay(self.data['governance_reply']);recovery=RecoveryReplay()
                path=ops.create(root,self.data['workflow'],live=True,ttl=300,settings=factory.request_settings)
                with ops.opened(path,auth=auth) as run:
                    for role,provider in [('factory',factory),('governance',governance)]:
                        preview=run.proposal_review(role);run.request_proposal(role,preview['digest'],principal,provider)
                    _,phrase=run.batch_proposal();run.approve_batch(phrase,principal)
                    run.simulate_failure('candidate');self.observe_settled(run);self.observe_settled(run)
                    before=run.state();self.assertEqual(before['phase'],'INCIDENT_RECORDED')
                    review=run.proposal_review('recovery');ctx=review['context']
                    self.assertEqual(ctx['current_state']['budget_version'],2)
                    self.assertEqual(ctx['approved_recovery_scope']['budget_version'],None)
                    self.assertIn('RECOMMENDATION_ONLY',ctx['decision_stage'])
                    self.assertIn('RECOVERY_REVIEW_REQUIRED',ctx['partial_recovery_contract'])
                    result=run.request_proposal('recovery',review['digest'],principal,recovery)
                    self.assertEqual(result['phase'],'AWAITING_RECOVERY_APPROVAL');self.assertIsNone(result['recovery'])
                    self.assertEqual(result['service']['policy_snapshot']['state']['paused_runs'],[])
                    self.assertEqual(result['model_calls'],3);self.assertEqual(len(recovery.calls),1)
                    with self.assertRaises(sim.Denied):run.proposal_review('recovery')
                    with self.assertRaises(sim.Denied):run.approve_recovery('wrong-incident-consent',principal)
                    _,phrase=run.recovery_proposal();result=run.approve_recovery(phrase,principal)
                    self.assertEqual(result['phase'],'AWAITING_OWNER_REVIEW')
                    self.assertEqual(result['recovery']['status'],'RECOVERY_REVIEW_REQUIRED')
                    self.assertEqual(result['service']['metrics']['committed'],60)
                    self.assertEqual(result['service']['actual_at_outcome']['candidate_percent'],15)
                    self.assertEqual(len(result['service']['policy_snapshot']['state']['deployments']['candidate-v2']['dependencies']),1)
                    self.assertEqual(result['operations']['emergency'],None)
                    self.assertEqual(len(recovery.calls),1);self.assertEqual(ops.verify(path)['status'],'CONSISTENT')
            finally:
                if path:
                    with ops.opened(path) as run:run.stop()
                auth.close()
    def test_scripted_context_is_the_same_recommendation_only_contract(self):
        with tempfile.TemporaryDirectory(prefix='scripted recovery contract ') as directory:
            path=ops.create(Path(directory),self.data['workflow'],ttl=300)
            try:
                with ops.opened(path) as run:
                    run.advance();_,phrase=run.batch_proposal();run.approve_batch(phrase,ops.fixture('manager'))
                    run.simulate_failure('candidate');self.observe_settled(run);self.observe_settled(run);value=run.advance()
                    self.assertIn('RECOMMENDATION_ONLY',value['calls'][-1]['context']['decision_stage'])
                    self.assertEqual(value['phase'],'AWAITING_RECOVERY_APPROVAL');self.assertIsNone(value['recovery'])
                    self.assertEqual(value['model_calls'],0)
            finally:
                with ops.opened(path) as run:run.stop()

if __name__=='__main__':unittest.main()
