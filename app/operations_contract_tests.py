"""Owner-supplied reply regressions; offline transport, no provider requests."""
import copy,json,tempfile,unittest
from pathlib import Path
import named_access
import operations_workspace as ops
import simulator as sim
from operations_factory_tests import Replay

FIXTURE=Path(__file__).with_name('proposal_reply_regressions.json')

class GovernanceReplay(ops.ScriptedRole):
    live=True
    def __init__(self,reply):
        super().__init__('governance');self.reply=reply;self.calls=[]
        self.request_settings=ops.factory.inference.inference_settings(2048,False)
    def ask(self,context):
        self.calls.append(copy.deepcopy(context));value=copy.deepcopy(self.reply)
        # A fresh synthetic test binding; never rewrite the saved user evidence.
        value['queue_digest']=context['queue_digest']
        if value['schema_version']=='COPY_CONTEXT':value['schema_version']=context['schema_version']
        return json.dumps(value),{'finish_reason':'stop','refused':False,'total_tokens':0,
            'source':'OFFLINE SYNTHETIC GOVERNANCE REPLAY; ZERO PROVIDER REQUESTS'}

class ContractTests(unittest.TestCase):
    def setUp(self):
        data=json.loads(FIXTURE.read_text());self.workflow=data['workflow'];self.desktop=data['desktop_planning'];self.laptop=data['laptop_governance']
        self.basis={'queue_digest':self.laptop['queue_digest'],'intents':self.workflow['requests']}
    def reject_factory(self,value,code):
        with self.assertRaises(ops.FactoryContractError) as error:ops.factory_requests(value,self.workflow)
        self.assertEqual(error.exception.code,code)
    def reject_governance(self,value,code):
        with self.assertRaises(ops.GovernanceContractError) as error:ops.parse_governance(json.dumps(value),self.basis)
        self.assertEqual(error.exception.code,code)
        self.assertNotIn('UNTRUSTED',str(error.exception))
    def valid_governance(self):
        value=copy.deepcopy(self.laptop);value['schema_version']=ops.m.GOVERNANCE_SCHEMA;return value
    def test_desktop_reply_compiles_without_modifying_raw_reply_or_registered_authority(self):
        before=copy.deepcopy(self.desktop);workflow=copy.deepcopy(self.workflow)
        actual=ops.factory_requests(self.desktop,self.workflow)
        self.assertEqual([i['parameters'] for i in actual],[{'units':15},{'limit':70},{'candidate_percent':15},{'dependency_id':'C-mission-job'},{'units':8}])
        self.assertEqual(self.desktop,before);self.assertEqual(self.workflow,workflow)
        for a,b in zip(actual,workflow['requests']):self.assertEqual({k:v for k,v in a.items() if k!='parameters'},{k:v for k,v in b.items() if k!='parameters'})
    def test_map_and_array_order_never_changes_registered_execution_metadata(self):
        value=copy.deepcopy(self.desktop);value['requests']=list(reversed(list(value['requests'].values())))
        self.assertEqual(ops.factory_requests(value,self.workflow),ops.factory_requests(self.desktop,self.workflow))
    def test_map_keys_cannot_replace_or_infer_explicit_identity(self):
        for mutation in ('mismatch','missing','not_object','non_string'):
            value=copy.deepcopy(self.desktop)
            if mutation=='mismatch':value['requests']['deploy-large']['id']='deploy-small'
            elif mutation=='missing':del value['requests']['deploy-large']['id']
            elif mutation=='not_object':value['requests']['deploy-large']=[]
            else:value['requests']['deploy-large']['id']=[]
            self.reject_factory(value,'FACTORY_REQUEST_KEY_MISMATCH')
    def test_map_cannot_omit_invent_or_duplicate_registered_ids(self):
        value=copy.deepcopy(self.desktop);del value['requests']['deploy-large'];self.reject_factory(value,'FACTORY_REQUEST_SHAPE_INVALID')
        value=copy.deepcopy(self.desktop);item=value['requests'].pop('deploy-large');item['id']='invented';value['requests']['invented']=item
        self.reject_factory(value,'FACTORY_REQUEST_IDS_INVALID')
        raw=json.dumps(self.desktop).replace('"deploy-large": {','"deploy-large": {}, "deploy-large": {',1)
        with self.assertRaises(ValueError):ops.factory.decode(raw)
    def test_map_retains_metadata_permissions_and_parameter_bounds(self):
        for field,value,code in [('actor','C','FACTORY_METADATA_CHANGED'),('approved',True,'FACTORY_REQUEST_SHAPE_INVALID'),('parameters',{'units':21},'FACTORY_PARAMETERS_INVALID'),('parameters',{'units':True},'FACTORY_PARAMETERS_INVALID')]:
            reply=copy.deepcopy(self.desktop);reply['requests']['deploy-large'][field]=value;self.reject_factory(reply,code)
    def test_laptop_original_wrong_schema_remains_rejected(self):
        self.reject_governance(self.laptop,'GOVERNANCE_SCHEMA_INVALID')
        with self.assertRaises(ValueError):ops.m.parse_governance(json.dumps(self.laptop),self.basis)
    def test_laptop_counterfactual_only_schema_change_passes_pinned_parser(self):
        value=self.valid_governance();self.assertEqual(ops.parse_governance(json.dumps(value),self.basis),ops.m.parse_governance(json.dumps(value),self.basis))
        self.assertEqual(self.laptop['schema_version'],ops.c.PLANNER_SCHEMA)
    def test_governance_rejects_wrong_envelope_decision_digest_and_reason(self):
        for field,value,code in [('extra','UNTRUSTED','GOVERNANCE_ENVELOPE_INVALID'),('schema_version','UNTRUSTED','GOVERNANCE_SCHEMA_INVALID'),('decision',[],'GOVERNANCE_DECISION_INVALID'),('decision','UNTRUSTED','GOVERNANCE_DECISION_INVALID'),('queue_digest','UNTRUSTED','GOVERNANCE_QUEUE_MISMATCH'),('reason','x'*601,'GOVERNANCE_REASON_INVALID'),('reason','','GOVERNANCE_REASON_INVALID')]:
            reply=self.valid_governance();reply[field]=value;self.reject_governance(reply,code)
    def test_governance_requires_all_ids_once_as_array(self):
        ids=self.laptop['order']
        for order in (ids[:-1],ids+[ids[0]],ids[:-1]+[ids[0]],['UNTRUSTED']+ids[1:],{'id':ids[0]},[{}]+ids[1:]):
            reply=self.valid_governance();reply['order']=order;self.reject_governance(reply,'GOVERNANCE_ORDER_INVALID')
    def test_governance_defer_is_valid_only_with_empty_array(self):
        value=self.valid_governance();value.update(decision='defer',order=[])
        self.assertEqual(ops.parse_governance(json.dumps(value),self.basis),value)
        for order in ({},None,['deploy-large']):
            value['order']=order;self.reject_governance(value,'GOVERNANCE_ORDER_INVALID')
    def test_governance_duplicate_json_keys_and_non_json_have_fixed_diagnostics(self):
        for raw in ('UNTRUSTED',json.dumps(self.valid_governance()).replace('"decision":','"decision":"defer", "decision":',1)):
            with self.assertRaises(ops.GovernanceContractError) as error:ops.parse_governance(raw,self.basis)
            self.assertEqual(error.exception.code,'GOVERNANCE_ENVELOPE_INVALID')
    def run_reviewed(self,wrong_schema=False):
        with tempfile.TemporaryDirectory(prefix='proposal contract replay ') as directory:
            auth=named_access.Access(Path(directory));path=None
            try:
                principal=auth.create('local-manager','Local test password only!',sorted(named_access.ROLES))
                planning=Replay(self.desktop);gov=self.valid_governance();gov['schema_version']=ops.c.PLANNER_SCHEMA if wrong_schema else 'COPY_CONTEXT';governance=GovernanceReplay(gov)
                path=ops.create(Path(directory),self.workflow,live=True,ttl=300,settings=planning.request_settings)
                with ops.opened(path,auth=auth) as run:
                    p=run.proposal_review('factory');result=run.request_proposal('factory',p['digest'],principal,planning)
                    self.assertEqual(result['phase'],'QUEUE_READY');self.assertEqual(result['calls'][0]['validation']['request_representation'],'ID_KEYED_OBJECT')
                    self.assertEqual(json.loads(result['calls'][0]['visible_content']),self.desktop)
                    preview=run.proposal_review('governance');ctx=preview['context']
                    self.assertEqual(ctx['schema_version'],ops.m.GOVERNANCE_SCHEMA)
                    self.assertIn(ctx['schema_version'],ops.PROMPTS['governance']);self.assertNotIn(ops.c.PLANNER_SCHEMA,json.dumps(ctx))
                    if wrong_schema:
                        with self.assertRaises(ops.GovernanceContractError):run.request_proposal('governance',preview['digest'],principal,governance)
                        result=run.publish();self.assertEqual(result['phase'],'STOPPED_FOR_REVIEW')
                        self.assertEqual(result['calls'][-1]['validation']['code'],'GOVERNANCE_SCHEMA_INVALID')
                        self.assertIsNone(result['plan_id'])
                    else:
                        result=run.request_proposal('governance',preview['digest'],principal,governance)
                        self.assertEqual(result['phase'],'AWAITING_BATCH_APPROVAL')
                        self.assertTrue(result['plan_id'])
                    self.assertEqual(result['model_calls'],2);self.assertEqual(planning.calls,1);self.assertEqual(len(governance.calls),1)
                    self.assertTrue(all(i['status']!='COMPLETED' for i in result['service']['intents']))
                    with self.assertRaises(sim.Denied):run.proposal_review('governance')
                    with self.assertRaises(sim.Denied):run.approve_batch('wrong approval',principal)
                    self.assertEqual(ops.verify(path)['status'],'CONSISTENT')
            finally:
                if path:
                    with ops.opened(path) as run:run.stop()
                auth.close()
    def test_desktop_then_context_copying_governance_reaches_exact_batch_review(self):self.run_reviewed()
    def test_invalid_governance_persists_fixed_reason_and_never_retries_or_executes(self):self.run_reviewed(True)
    def test_scripted_advance_uses_the_same_governance_response_schema(self):
        with tempfile.TemporaryDirectory(prefix='scripted contract replay ') as directory:
            path=ops.create(Path(directory),self.workflow,ttl=300)
            try:
                with ops.opened(path) as run:
                    result=run.advance();call=result['calls'][1]
                    self.assertEqual(call['context']['schema_version'],ops.m.GOVERNANCE_SCHEMA)
                    self.assertEqual(result['phase'],'AWAITING_BATCH_APPROVAL');self.assertEqual(result['model_calls'],0)
            finally:
                with ops.opened(path) as run:run.stop()

    def test_recorded_cycle_claim_has_a_policy_checked_partial_batch_counterexample(self):
        data=json.loads(Path(__file__).with_name('governance_deferral_regression.json').read_text())
        result=ops.planner.plan(data['state'],data['requests'],data['requests'][0]['submitted_unix']+1)
        chosen=result['selected']
        self.assertEqual(result['orders_compared'],120)
        self.assertEqual(set(chosen['accepted']),{'customer-job','customer-route','deploy-small','budget-70'})
        self.assertEqual([v['id'] for v in chosen['waiting']],['deploy-large'])
        self.assertEqual(chosen['committed_units'],65);self.assertEqual(chosen['capacity_limit'],70)
        self.assertTrue(chosen['reference_agrees'])
        self.assertEqual(data['state']['routing']['candidate-v2'],0)
    def test_new_instructions_distinguish_conflict_pairs_from_after_dependencies(self):
        prompt=ops.PROMPTS['governance']
        for phrase in ["Only each request's after field defines prerequisites",'safe partial batch','Defer remains allowed','exact manager consent']:
            self.assertIn(phrase,prompt)
    def test_valid_deferral_remains_recorded_without_a_plan_fallback_or_paid_retry(self):
        with tempfile.TemporaryDirectory(prefix='valid governance deferral ') as directory:
            auth=named_access.Access(Path(directory));path=None
            try:
                principal=auth.create('local-manager','Local test password only!',sorted(named_access.ROLES))
                planning=Replay(self.desktop);value=self.valid_governance();value.update(decision='defer',order=[],reason='Uncertainty remains; request manager review.')
                governance=GovernanceReplay(value)
                path=ops.create(Path(directory),self.workflow,live=True,ttl=300,settings=planning.request_settings)
                with ops.opened(path,auth=auth) as run:
                    review=run.proposal_review('factory');run.request_proposal('factory',review['digest'],principal,planning)
                    review=run.proposal_review('governance')
                    self.assertIn('blocked requests remain waiting',review['context']['conflict_semantics'])
                    result=run.request_proposal('governance',review['digest'],principal,governance)
                    self.assertEqual(result['phase'],'GOVERNANCE_DEFERRED');self.assertIsNone(result['plan_id'])
                    self.assertEqual(result['decisions']['governance']['decision'],'defer')
                    self.assertEqual(result['calls'][-1]['status'],'VALIDATED');self.assertEqual(result['model_calls'],2)
                    with self.assertRaises(sim.Denied):run.proposal_review('governance')
                    self.assertEqual(len(governance.calls),1);self.assertEqual(result['service']['plans'],[])
                    self.assertTrue(all(i['status']!='COMPLETED' for i in result['service']['intents']))
                    self.assertEqual(ops.verify(path)['status'],'CONSISTENT')
            finally:
                if path:
                    with ops.opened(path) as run:run.stop()
                auth.close()

if __name__=='__main__':unittest.main()
