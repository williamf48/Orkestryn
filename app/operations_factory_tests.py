"""Recorded-reply compatibility tests; fake transport, zero provider requests."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import named_access
import operations_workspace as ops
import simulator as sim

FIXTURE=Path(__file__).with_name('factory_reply_regression.json')

class Replay(ops.ScriptedRole):
    live=True
    def __init__(self,payload):
        super().__init__('factory');self.payload=payload;self.calls=0
        self.request_settings=ops.factory.inference.inference_settings(2048,False)
    def ask(self,context):
        self.calls+=1
        return json.dumps(self.payload),{'finish_reason':'stop','refused':False,'total_tokens':0,
            'source':'OFFLINE SUPPLIED-REPLY REPLAY; FAKE TRANSPORT; NO PROVIDER REQUEST'}

class FactoryContractTests(unittest.TestCase):
    def setUp(self):
        fixture=json.loads(FIXTURE.read_text());self.workflow=fixture['workflow'];self.reply=fixture['reply']
    def reject(self,value,code):
        with self.assertRaises(ops.FactoryContractError) as caught:ops.factory_requests(value,self.workflow)
        self.assertEqual(caught.exception.code,code)
        return caught.exception
    def test_recorded_nemotron_reply_preserves_registered_metadata_and_bounded_values(self):
        before=copy.deepcopy(self.workflow);compiled=ops.factory_requests(self.reply,self.workflow)
        self.assertEqual(self.workflow,before)
        self.assertEqual([r['parameters'] for r in compiled],[{'units':15},{'limit':70},{'candidate_percent':12},{'dependency_id':'C-mission-job'},{'units':5}])
        for actual,original in zip(compiled,self.workflow['requests']):
            self.assertEqual({k:v for k,v in actual.items() if k!='parameters'}, {k:v for k,v in original.items() if k!='parameters'})
    def test_minimal_partial_and_reordered_matching_echoes_compile_identically(self):
        expected=ops.factory_requests(self.reply,self.workflow)
        for fields in ({'id','parameters'},{'id','parameters','actor'},{'id','parameters','after','priority'}):
            with self.subTest(fields=fields):
                value=copy.deepcopy(self.reply);value['requests']=[{k:v for k,v in item.items() if k in fields} for item in reversed(value['requests'])]
                self.assertEqual(ops.factory_requests(value,self.workflow),expected)
    def test_changed_actor_tool_priority_or_dependencies_are_rejected(self):
        for field,value in [('actor','C'),('tool','set_route'),('priority',4),('priority',3.0),('priority',True),('after',['customer-job']),('after',{})]:
            with self.subTest(field=field,value=value):
                reply=copy.deepcopy(self.reply);reply['requests'][0][field]=value
                self.reject(reply,'FACTORY_METADATA_CHANGED')
    def test_missing_duplicate_invented_and_unknown_fields_are_rejected(self):
        value=copy.deepcopy(self.reply);value['requests'].pop();self.reject(value,'FACTORY_REQUEST_SHAPE_INVALID')
        for value in ('customer-job','invented',{'unhashable':'id'}):
            reply=copy.deepcopy(self.reply);reply['requests'][0]['id']=value;self.reject(reply,'FACTORY_REQUEST_IDS_INVALID')
        for key in ('permission','owner','approval','approved','run_id'):
            reply=copy.deepcopy(self.reply);reply['requests'][0][key]=True;self.reject(reply,'FACTORY_REQUEST_SHAPE_INVALID')
    def test_numeric_parameters_retain_strict_types_and_original_bounds(self):
        for slot,field in [(0,'units'),(2,'candidate_percent'),(4,'units')]:
            for value in (0,21,True,False,1.0,'10',None):
                with self.subTest(slot=slot,value=value):
                    reply=copy.deepcopy(self.reply);reply['requests'][slot]['parameters'][field]=value
                    self.reject(reply,'FACTORY_PARAMETERS_INVALID')
    def test_fixed_parameters_cannot_change_or_use_numeric_type_aliases(self):
        for slot,parameters in [(1,{'limit':100}),(1,{'limit':70.0}),(3,{'dependency_id':'other-job'}),(1,{'limit':70,'approved':True})]:
            reply=copy.deepcopy(self.reply);reply['requests'][slot]['parameters']=parameters
            self.reject(reply,'FACTORY_PARAMETERS_INVALID')
    def test_schema_reason_and_top_level_fields_remain_strict(self):
        for key,value,code in [('schema_version','other','FACTORY_ENVELOPE_INVALID'),('reason','','FACTORY_REASON_INVALID'),('reason','x'*601,'FACTORY_REASON_INVALID'),('extra',True,'FACTORY_ENVELOPE_INVALID')]:
            reply=copy.deepcopy(self.reply);reply[key]=value;self.reject(reply,code)
    def test_recorded_reply_completes_one_reviewed_role_without_chaining_or_batch_effects(self):
        with tempfile.TemporaryDirectory(prefix='factory reply replay ') as directory:
            auth=named_access.Access(Path(directory));path=None
            try:
                principal=auth.create('local-manager','Local test password only!',sorted(named_access.ROLES))
                provider=Replay(self.reply);path=ops.create(Path(directory),self.workflow,live=True,ttl=300,settings=provider.request_settings)
                with ops.opened(path,auth=auth) as run:
                    preview=run.proposal_review('factory');value=run.request_proposal('factory',preview['digest'],principal,provider)
                self.assertEqual(value['phase'],'QUEUE_READY');self.assertEqual(provider.calls,1)
                self.assertEqual(value['model_calls'],1);self.assertEqual(len(value['calls']),1)
                self.assertEqual(value['calls'][0]['status'],'VALIDATED')
                validation=value['calls'][0]['validation'];self.assertEqual(validation['authority_source'],'REGISTERED_WORKFLOW')
                self.assertEqual(sum(len(i['fields']) for i in validation['readonly_echoes_verified']),20)
                self.assertEqual(json.loads(value['calls'][0]['visible_content']),self.reply)
                self.assertTrue(all(i['status']!='COMPLETED' for i in value['service']['intents']))
                self.assertEqual(ops.verify(path)['status'],'CONSISTENT')
                with ops.opened(path,auth=auth) as run:
                    with self.assertRaises(sim.Denied):run.proposal_review('factory')
                self.assertEqual(provider.calls,1)
            finally:
                if path:
                    with ops.opened(path) as run:run.stop()
                auth.close()
    def test_changed_metadata_stops_with_specific_saved_reason_and_no_bootstrap(self):
        with tempfile.TemporaryDirectory(prefix='factory contract denial ') as directory:
            auth=named_access.Access(Path(directory));path=None
            try:
                principal=auth.create('local-manager','Local test password only!',sorted(named_access.ROLES))
                reply=copy.deepcopy(self.reply);reply['requests'][0]['actor']='C';provider=Replay(reply)
                path=ops.create(Path(directory),self.workflow,live=True,ttl=300,settings=provider.request_settings)
                with ops.opened(path,auth=auth) as run:
                    preview=run.proposal_review('factory')
                    with self.assertRaises(ops.FactoryContractError):run.request_proposal('factory',preview['digest'],principal,provider)
                value=ops.read(path);self.assertEqual(value['phase'],'STOPPED_FOR_REVIEW');self.assertIsNone(value['child'])
                self.assertEqual(value['calls'][0]['validation']['code'],'FACTORY_METADATA_CHANGED')
                self.assertEqual(value['calls'][0]['status'],'INVALID');self.assertEqual(provider.calls,1)
                self.assertIsNone(value['operations'].get('compiled_requests'));self.assertEqual(ops.verify(path)['status'],'CONSISTENT')
            finally:
                if path:
                    with ops.opened(path) as run:run.stop()
                auth.close()
    def test_saved_rejection_reason_uses_fixed_text_instead_of_model_content(self):
        reply=copy.deepcopy(self.reply);reply['requests'][0]['actor']='<script>untrusted-model-secret</script>'
        error=self.reject(reply,'FACTORY_METADATA_CHANGED')
        self.assertNotIn('untrusted-model-secret',str(error));self.assertNotIn('<script>',str(error))

if __name__=='__main__':unittest.main()
