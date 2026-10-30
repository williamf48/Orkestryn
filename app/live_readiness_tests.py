import copy
import json
from pathlib import Path
import tempfile
import unittest

import live_readiness as live
import operations_workspace as ops


class LiveEvidenceTests(unittest.TestCase):
    def fixture(self):
        return {'live':True,'historical_operations':False,'mission_id':'fixture-only',
            'model_calls':3,'phase':'RECOVERED_SERVICE_ACTIVE','lifecycle':'STOPPED',
            'teardown':{'all_workers_stopped':True},'calls':[
                {'role':r,'status':'VALIDATED','live':True,'metadata':{
                    'source':'LIVE NEBIUS MISSION '+r.upper(),'response_id':'fixture-'+r,
                    'model':'fixture-model-not-a-real-response','finish_reason':'stop',
                    'refused':False,'total_tokens':12}} for r in ops.ROLES]}

    def test_complete_saved_record_is_scoped_and_excludes_response_text(self):
        summary=self.fixture();summary['calls'][0]['visible_content']='private fixture response'
        report=live.assess(summary,{'status':'CONSISTENT'})
        self.assertEqual(report['status'],'READY_FROM_SAVED_LIVE_RECORDS')
        self.assertNotIn('private fixture response',json.dumps(report))
        self.assertIn('not provider-signed attestation',report['scope'])
        self.assertEqual(report['model_calls_to_build_report'],0)

    def test_scripted_or_falsely_flagged_fixture_cannot_look_like_validated_live_clients(self):
        for scripted in (True,False):
            summary=self.fixture()
            if scripted:summary['live']=False;summary['model_calls']=0
            for call in summary['calls']:
                call['metadata']={'source':'SCRIPTED OR MOCK FIXTURE','finish_reason':'stop','refused':False}
            report=live.assess(summary,{'status':'CONSISTENT'})
            self.assertEqual(report['status'],'NEEDS_LIVE_WALKTHROUGH')
            self.assertFalse(report['checks']['live_client_metadata_present'])

    def test_partial_failed_historical_active_and_unconfirmed_runs_remain_incomplete(self):
        base=self.fixture()
        versions=[{**base,'calls':base['calls'][:2]}, {**base,'historical_operations':True},
                  {**base,'phase':'STOPPED_FOR_REVIEW'}, {**base,'lifecycle':'LEASED'},
                  {**base,'teardown':{'all_workers_stopped':False}}]
        failed=copy.deepcopy(base);failed['calls'][-1]['status']='ERROR';versions.append(failed)
        for summary in versions:
            self.assertEqual(live.assess(summary,{'status':'CONSISTENT'})['status'],'NEEDS_LIVE_WALKTHROUGH')
        self.assertEqual(live.assess(base,{'status':'INCONSISTENT'})['status'],'NEEDS_LIVE_WALKTHROUGH')

    def test_unknown_or_invalid_usage_stays_unknown(self):
        summary=self.fixture();summary['calls'][1]['metadata']['total_tokens']=None
        summary['calls'][2]['metadata']['total_tokens']=-1
        report=live.assess(summary,{'status':'CONSISTENT'})
        self.assertEqual(report['reported_total_tokens'],12)
        self.assertEqual(report['attempts_with_unknown_usage'],2)

    def test_scripted_capture_is_read_only_and_explicitly_needs_live_validation(self):
        with tempfile.TemporaryDirectory(prefix='live capture ') as tmp:
            root=Path(tmp);path,_=ops.demo(root/'runs')
            before={p.name:p.read_bytes() for p in path.iterdir() if p.is_file()}
            directory,report=live.capture(path,root/'public')
            self.assertEqual(report['status'],'NEEDS_LIVE_WALKTHROUGH')
            self.assertEqual(report['attempts_with_unknown_usage'],0)
            self.assertEqual({p.name:p.read_bytes() for p in path.iterdir() if p.is_file()},before)
            self.assertEqual({p.name for p in directory.iterdir()}, {'live_walkthrough.json','outcome_report.json','outcome_report.html'})
            self.assertEqual(json.loads((directory/'live_walkthrough.json').read_text())['model_calls_to_build_report'],0)

    def test_session_key_records_use_supported_exact_source_without_mutating_evidence(self):
        summary=self.fixture()
        for call in summary['calls']:call['metadata']['source']='LIVE NVIDIA VIA NEBIUS TOKEN FACTORY; SESSION KEY'
        before=copy.deepcopy(summary);report=live.assess(summary,{'status':'CONSISTENT'})
        self.assertEqual(report['status'],'READY_FROM_SAVED_LIVE_RECORDS')
        self.assertEqual(summary,before);self.assertEqual(report['model_calls_to_build_report'],0)
    def test_unknown_source_variants_and_incomplete_session_metadata_remain_rejected(self):
        for source in ['LIVE NVIDIA VIA NEBIUS TOKEN FACTORY','LIVE NVIDIA VIA NEBIUS TOKEN FACTORY; SESSION KEY; MOCK','SCRIPTED OR MOCK FIXTURE',None,True,[],{}]:
            summary=self.fixture();summary['calls'][0]['metadata']['source']=source
            self.assertFalse(live.assess(summary,{'status':'CONSISTENT'})['checks']['live_client_metadata_present'])
        for key,value in [('response_id',''),('model',None),('finish_reason','length'),('refused',True)]:
            summary=self.fixture();meta=summary['calls'][0]['metadata'];meta['source']='LIVE NVIDIA VIA NEBIUS TOKEN FACTORY; SESSION KEY';meta[key]=value
            self.assertFalse(live.assess(summary,{'status':'CONSISTENT'})['checks']['live_client_metadata_present'])
    def test_session_label_never_overrides_failed_roles_inconsistent_ledger_or_missing_shutdown(self):
        summary=self.fixture()
        for call in summary['calls']:call['metadata']['source']='LIVE NVIDIA VIA NEBIUS TOKEN FACTORY; SESSION KEY'
        for field,value in [('phase','RECOVERY_DEFERRED'),('lifecycle','LEASED'),('teardown',{'all_workers_stopped':False})]:
            changed=copy.deepcopy(summary);changed[field]=value
            self.assertEqual(live.assess(changed,{'status':'CONSISTENT'})['status'],'NEEDS_LIVE_WALKTHROUGH')
        self.assertEqual(live.assess(summary,{'status':'INCONSISTENT'})['status'],'NEEDS_LIVE_WALKTHROUGH')
