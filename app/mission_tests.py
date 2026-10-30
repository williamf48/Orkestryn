from test_paths import linked_path
"""Contract, persistence, measured health and native worker mission checks."""
import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from types import SimpleNamespace

import mission_control as m
import change_coordinator as c
import durable_runtime as d
import service_agent as service
import simulator as sim

class Crash(BaseException):pass

class LiveRole(m.ScriptedRole):
    live=True;request_settings={'max_output_tokens':8192,'thinking':True}
    def __init__(self,role):super().__init__(role);self.calls=[]
    def ask(self,context):
        self.calls.append(copy.deepcopy(context));content,meta=super().ask(context)
        return content,{**meta,'total_tokens':100,'source':'MOCK LIVE, NO API REQUEST'}

class MissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='mission checks ');self.root=Path(self.temp.name);self.paths=[]
    def tearDown(self):
        for path in self.paths:
            try:
                with m.opened(path) as mission:mission.stop()
            except (OSError,ValueError,sim.Denied):
                try:
                    with m.opened(path) as mission:mission.restart();mission.stop()
                except (OSError,ValueError,sim.Denied):pass
        self.temp.cleanup()
    def start(self,live=False):
        providers={r:LiveRole(r) if live else m.ScriptedRole(r) for r in m.ROLES}
        path=m.create(self.root,live=live,ttl=240,settings=providers['factory'].request_settings);self.paths.append(path)
        with m.opened(path) as mission:value=mission.advance(providers)
        self.assertEqual(value['phase'],'AWAITING_BATCH_APPROVAL');return path,providers
    def execute(self,path):
        with m.opened(path) as mission:
            p,phrase=mission.batch_proposal();return mission.approve_batch(phrase,'EXPLICIT TERMINAL MANAGER FIXTURE' if mission.state()['live'] else 'SCRIPTED MANAGER FIXTURE')
    def incident(self,path,providers=None,fault='workflow'):
        self.execute(path)
        with m.opened(path) as mission:
            mission.observe();mission.simulate_failure(fault);mission.observe();value=mission.observe();self.assertEqual(value['phase'],'INCIDENT_RECORDED')
            return mission.advance(providers)
    def recover(self,path):
        with m.opened(path) as mission:
            p,phrase=mission.recovery_proposal();return mission.approve_recovery(phrase,'EXPLICIT TERMINAL MANAGER FIXTURE' if mission.state()['live'] else 'SCRIPTED MANAGER FIXTURE')
    def core(self,path):
        s=m.read(path);return path/'service'/s['child']
    def test_all_three_agents_handoff_into_the_same_real_service_and_owner_completion(self):
        path,providers=self.start(live=True);initial=m.read(path);self.assertEqual(initial['role_requests'],2)
        self.incident(path,providers);value=self.recover(path);before=value['service'];self.assertEqual((before['metrics']['committed'],before['metrics']['limit']),(60,70))
        self.assertEqual(before['actual_at_outcome']['candidate_percent'],20);self.assertTrue(before['actual_at_outcome']['workers']['candidate-v2']['alive'])
        self.assertEqual([c['role'] for c in value['calls']],list(m.ROLES));self.assertEqual(value['model_calls'],3);self.assertEqual(value['usage']['reported_tokens'],300)
        self.assertEqual(len({c['system'] for c in value['calls']}),3);self.assertNotIn('comparison',providers['governance'].calls[0]);self.assertNotIn('expected',sim.canonical(providers['factory'].calls[0]))
        with m.opened(path) as mission:
            p,phrase=mission.owner_proposal('retain');kept=mission.approve_owner(p,phrase,'EXPLICIT TERMINAL C OWNER FIXTURE')
            self.assertEqual(kept['service']['policy_snapshot']['state'],before['policy_snapshot']['state'])
            for decision in ('withdraw_route','release_dependency'):
                p,phrase=mission.owner_proposal(decision);value=mission.approve_owner(p,phrase,'EXPLICIT TERMINAL C OWNER FIXTURE')
        self.assertEqual(value['phase'],'RECOVERED_SERVICE_ACTIVE');self.assertEqual(value['service']['metrics']['committed'],40)
        self.assertTrue(value['service']['actual_at_outcome']['workers']['candidate-v2']['exit_confirmed']);self.assertIn('deploy-A',value['service']['policy_snapshot']['state']['paused_runs'])
        self.assertEqual(initial['service']['runtime_id'],value['service']['runtime_id']);self.assertEqual(m.verify(path)['status'],'CONSISTENT')
    def test_factory_rejects_bool_ranges_extra_authority_and_no_workers_start(self):
        v=json.loads(m.ScriptedRole('factory').ask({})[0])
        for change in ({'large_units':True},{'large_units':21},{'small_units':0},{'candidate_percent':21},{'approved':True}):
            with self.subTest(change=change),self.assertRaises(ValueError):m.compile_factory({**v,**change})
        path=m.create(self.root,ttl=180);self.paths.append(path)
        provider=m.ScriptedRole('factory');provider.ask=lambda _: (sim.canonical({**v,'tool':'shell'}),{'finish_reason':'stop','refused':False})
        with m.opened(path) as mission:
            with self.assertRaises(ValueError):mission.advance({'factory':provider})
        self.assertFalse((path/'service').exists());self.assertEqual(m.read(path)['calls'][0]['status'],'INVALID')
    def test_factory_rejected_response_never_gets_a_scripted_replacement(self):
        path=m.create(self.root,ttl=180);self.paths.append(path)
        p=m.ScriptedRole('factory');p.ask=mock.Mock(return_value=('not JSON',{'finish_reason':'stop','refused':False}))
        with m.opened(path) as mission:
            with self.assertRaises(ValueError):mission.advance({'factory':p})
            self.assertEqual(mission.advance()['phase'],'STOPPED_FOR_REVIEW')
        self.assertEqual(p.ask.call_count,1);self.assertFalse((path/'service').exists())
    def test_batch_needs_exact_separate_consent_before_any_requested_effect(self):
        path,_=self.start();before=m.read(path)['service']['policy_snapshot']['state']
        with m.opened(path) as mission:
            self.assertEqual(mission.advance()['phase'],'AWAITING_BATCH_APPROVAL')
            with self.assertRaises(sim.Denied):mission.approve_batch('yes','SCRIPTED MANAGER FIXTURE')
        self.assertEqual(m.read(path)['service']['policy_snapshot']['state'],before);self.assertEqual(sim.total_commitments(before),60)
    def test_live_manager_and_owner_cannot_use_scripted_consent_source(self):
        path,providers=self.start(live=True)
        with m.opened(path) as mission:
            p,phrase=mission.batch_proposal()
            with self.assertRaises(ValueError):mission.approve_batch(phrase,'SCRIPTED MANAGER FIXTURE')
        self.incident(path,providers);self.recover(path)
        with m.opened(path) as mission:
            p,phrase=mission.owner_proposal('retain')
            with self.assertRaises(ValueError):mission.approve_owner(p,phrase,'SCRIPTED C OWNER FIXTURE')
    def test_healthy_monitoring_does_not_call_recovery_or_pause_a(self):
        path,_=self.start();self.execute(path)
        with m.opened(path) as mission:
            for _ in range(3):value=mission.observe()
        self.assertEqual(value['phase'],'MONITORING');self.assertEqual(len(value['calls']),2);self.assertEqual(value['consecutive_bad'],0)
        self.assertNotIn('deploy-A',value['service']['policy_snapshot']['state']['paused_runs']);self.assertTrue(all(not r['bad'] for r in value['health']))
    def test_one_bad_window_is_insufficient_and_a_healthy_window_resets_it(self):
        path,_=self.start();self.execute(path)
        with m.opened(path) as mission:
            original=m.measure
            def bad(b,seq):
                r=original(b,seq);r['samples']['router'][0]['ok']=False;r['bad']=True;r['digest']=sim.digest({k:v for k,v in r.items() if k!='digest'});return r
            with mock.patch.object(m,'measure',side_effect=bad):value=mission.observe()
            self.assertEqual(value['phase'],'MONITORING');self.assertEqual(value['consecutive_bad'],1)
            value=mission.observe();self.assertEqual(value['consecutive_bad'],0);self.assertIsNone(value['incident'])
    def test_bad_windows_with_a_large_gap_do_not_form_an_incident(self):
        path,_=self.start();self.execute(path)
        with m.opened(path) as mission:
            mission.simulate_failure();mission.observe();s=mission.state();s['health'][-1]['finished_unix']-=61;mission.save(s,'MOCK_OLD_WINDOW')
            value=mission.observe();self.assertEqual(value['phase'],'MONITORING');self.assertEqual(value['consecutive_bad'],1)
    def test_two_actual_candidate_failure_windows_preserve_c_and_no_capacity_is_lost(self):
        path,_=self.start();self.incident(path,fault='candidate');value=self.recover(path)
        health=value['health'][-1];self.assertEqual(sum(r['ok'] for r in health['samples']['candidate']),0);self.assertEqual(sum(r['ok'] for r in health['samples']['stable']),10)
        self.assertEqual(value['service']['metrics']['committed'],60);self.assertEqual(value['service']['actual_at_outcome']['candidate_percent'],20)
        self.assertEqual(value['recovery']['status'],'RECOVERY_REVIEW_REQUIRED')
        with m.opened(path) as mission:
            proposal,phrase=mission.owner_proposal('withdraw_route')
            with self.assertRaises(ValueError):mission.approve_owner(proposal,phrase,'SCRIPTED C OWNER FIXTURE')
        self.assertEqual(m.read(path)['service']['actual_at_outcome']['candidate_percent'],20)
    def test_recovery_needs_its_own_exact_incident_consent(self):
        path,_=self.start();value=self.incident(path)
        with m.opened(path) as mission:
            before=mission.publish()['service']['policy_snapshot']['state']
            with self.assertRaises(sim.Denied):mission.approve_recovery('recover','SCRIPTED MANAGER FIXTURE')
            self.assertEqual(mission.publish()['service']['policy_snapshot']['state'],before)
        self.assertEqual(value['service']['metrics']['committed'],65)
    def test_recovery_stops_when_the_reviewed_incident_has_cleared(self):
        path,_=self.start();self.incident(path)
        with c.open_broker(self.core(path)) as broker:
            service.trusted_change(broker.engine,'MOCK_INCIDENT_CLEARED',lambda s:s['paused_runs'].remove('deploy-A'));c.publish(self.core(path),broker)
        with m.opened(path) as mission:
            p,phrase=mission.recovery_proposal()
            with self.assertRaises(sim.Denied):mission.approve_recovery(phrase,'SCRIPTED MANAGER FIXTURE')
        self.assertEqual(m.read(path)['service']['metrics']['committed'],65)
    def test_generation_error_records_the_attempt_without_retry_or_usage_fabrication(self):
        path=m.create(self.root,live=True,ttl=180,settings=LiveRole.request_settings);self.paths.append(path);provider=LiveRole('factory')
        provider.ask=mock.Mock(side_effect=m.factory.GenerationError('MOCK transport error'))
        with m.opened(path) as mission:
            with self.assertRaises(m.factory.GenerationError):mission.advance({'factory':provider})
            value=mission.advance()
        self.assertEqual(value['model_calls'],1);self.assertEqual(value['usage']['missing_usage_calls'],1);self.assertEqual(provider.ask.call_count,1)
    def test_an_uncertain_model_attempt_is_never_repeated_after_interruption(self):
        path=m.create(self.root,live=True,ttl=180,settings=LiveRole.request_settings);self.paths.append(path);p=LiveRole('factory')
        def crash(phase,key):
            if phase=='role_started':raise Crash()
        with m.opened(path,crash) as mission:
            with self.assertRaises(Crash):mission.advance({'factory':p})
        with m.opened(path) as mission:value=mission.advance({'factory':p})
        self.assertEqual(p.calls,[]);self.assertEqual(value['model_calls'],1);self.assertEqual(value['calls'][0]['status'],'OUTCOME_UNKNOWN');self.assertFalse((path/'service').exists())
    def test_a_saved_factory_response_can_resume_without_another_model_request(self):
        path=m.create(self.root,ttl=180);self.paths.append(path);p=m.ScriptedRole('factory');p.ask=mock.Mock(wraps=p.ask)
        def crash(phase,key):
            if phase=='role_received':raise Crash()
        with m.opened(path,crash) as mission:
            with self.assertRaises(Crash):mission.advance({'factory':p})
        with m.opened(path) as mission:value=mission.advance({'factory':p})
        self.assertEqual(p.ask.call_count,1);self.assertEqual(value['phase'],'QUEUE_READY');self.assertEqual(len(value['calls']),1)
    def test_interrupted_bootstrap_adopts_the_same_runtime_and_does_not_reseed(self):
        path=m.create(self.root,ttl=180);self.paths.append(path)
        def crash(phase,key):
            if phase=='bootstrap_effect':raise Crash()
        with m.opened(path,crash) as mission:
            with self.assertRaises(Crash):mission.advance()
        child=next(p for p in (path/'service').iterdir() if p.is_dir());runtime=d.Runtime(child/'runtime').identity
        with m.opened(path) as mission:value=mission.advance()
        self.assertEqual(value['service']['runtime_id'],runtime);self.assertEqual(len(value['service']['intents']),5);self.assertEqual(len(list((path/'service').glob('*/session.json'))),1)
    def test_interrupted_governance_plan_links_the_original_plan_without_a_new_call(self):
        path=m.create(self.root,ttl=180);self.paths.append(path)
        def crash(phase,key):
            if phase=='plan_effect':raise Crash()
        with m.opened(path,crash) as mission:
            with self.assertRaises(Crash):mission.advance()
        with m.opened(path) as mission:value=mission.advance()
        self.assertEqual(value['phase'],'AWAITING_BATCH_APPROVAL');self.assertEqual(len(value['service']['plans']),1);self.assertEqual(len(value['calls']),2)
    def test_client_death_after_batch_effect_reconciles_the_completed_cursor(self):
        path,_=self.start()
        def crash(phase,key):
            if phase=='batch_effect':raise Crash()
        with m.opened(path,crash) as mission:
            p,phrase=mission.batch_proposal()
            with self.assertRaises(Crash):mission.approve_batch(phrase,'SCRIPTED MANAGER FIXTURE')
        before=c.saved_view(self.core(path))['policy_snapshot']
        with m.opened(path) as mission:value=mission.advance()
        self.assertEqual(value['phase'],'MONITORING');self.assertEqual(before,value['service']['policy_snapshot']);self.assertTrue(value['batch_result']['replayed'])
    def test_client_death_after_recovery_report_reuses_it_without_another_recovery(self):
        path,_=self.start();self.incident(path)
        def crash(phase,key):
            if phase=='recovery_effect':raise Crash()
        with m.opened(path,crash) as mission:
            p,phrase=mission.recovery_proposal()
            with self.assertRaises(Crash):mission.approve_recovery(phrase,'SCRIPTED MANAGER FIXTURE')
        child=self.core(path);before=c.saved_view(child)['policy_snapshot']
        with m.opened(path) as mission:value=mission.advance()
        self.assertEqual(value['phase'],'AWAITING_OWNER_REVIEW');self.assertEqual(before,value['service']['policy_snapshot'])
    def test_client_death_after_c_route_effect_reconciles_exact_receipt(self):
        path,_=self.start();self.incident(path);self.recover(path)
        def crash(phase,key):
            if phase=='owner_effect':raise Crash()
        with m.opened(path,crash) as mission:
            p,phrase=mission.owner_proposal('withdraw_route')
            with self.assertRaises(Crash):mission.approve_owner(p,phrase,'SCRIPTED C OWNER FIXTURE')
        before=service.runtime_view(d.Runtime(self.core(path)/'runtime').state())
        with m.opened(path) as mission:value=mission.advance()
        self.assertEqual(service.runtime_view(value['service']['actual_at_outcome']),before);self.assertEqual(value['owner_requests'][-1]['phase'],'FINISHED')
        routes=[e for e in value['service']['policy_snapshot']['events'] if e.get('action_key')=='mission-owner-1' and e.get('kind')=='ACTION'];self.assertEqual(len(routes),1)
    def test_client_death_after_c_dependency_effect_is_exactly_reconciled(self):
        path,_=self.start();self.incident(path);self.recover(path)
        def crash(phase,key):
            if phase=='owner_effect':raise Crash()
        with m.opened(path,crash) as mission:
            p,phrase=mission.owner_proposal('release_dependency')
            with self.assertRaises(Crash):mission.approve_owner(p,phrase,'SCRIPTED C OWNER FIXTURE')
        with m.opened(path) as mission:value=mission.advance()
        events=[e for e in value['service']['policy_snapshot']['events'] if e.get('kind')=='MISSION_C_DEPENDENCY_RELEASE'];self.assertEqual(len(events),1)
        self.assertEqual(value['service']['actual_at_outcome']['candidate_percent'],20)
    def test_supervisor_loss_is_not_counted_as_a_worker_incident_and_restart_keeps_workers(self):
        path,_=self.start();self.execute(path);child=self.core(path);before=d.Runtime(child/'runtime').state();process=d.CHILDREN[before['supervisor_pid']]
        process.kill();process.wait(5)
        with m.opened(path) as mission:
            value=mission.observe();self.assertEqual(value['last_connection'],'UNREACHABLE');self.assertIsNone(value['incident']);self.assertEqual(value['health'],[])
            value=mission.restart();mission.observe()
        self.assertEqual(value['service']['actual_at_outcome']['workers'],before['workers']);self.assertEqual(value['service']['runtime_id'],before['runtime_id'])
    def test_stale_owner_consent_cannot_remove_another_dependency(self):
        path,_=self.start();self.incident(path);self.recover(path)
        with m.opened(path) as mission:p,phrase=mission.owner_proposal('release_dependency')
        with c.open_broker(self.core(path)) as b:
            service.trusted_change(b.engine,'MOCK_OTHER_DEPENDENCY',lambda s:s['deployments']['candidate-v2']['dependencies'].append({'id':'other','owner_run':'cost-B'}));c.publish(self.core(path),b)
        with m.opened(path) as mission:
            with self.assertRaises(sim.Denied):mission.approve_owner(p,phrase,'SCRIPTED C OWNER FIXTURE')
        self.assertEqual(len(c.saved_view(self.core(path))['policy_snapshot']['state']['deployments']['candidate-v2']['dependencies']),2)
    def test_source_or_expiry_blocks_new_commands_but_separate_teardown_remains_available(self):
        path,_=self.start();state=m.read(path)['service']['policy_snapshot']['state']
        with m.opened(path) as mission:
            with mock.patch.object(m,'source_digest',return_value='changed'):
                with self.assertRaises(sim.Denied):mission.advance()
            with mock.patch.object(m.time,'time',return_value=time.time()+1000):
                with self.assertRaises(sim.Denied):mission.observe()
            value=mission.stop()
        self.assertEqual(value['lifecycle'],'STOPPED');self.assertEqual(value['service']['policy_snapshot']['state'],state)
    def test_store_tampering_is_detected_before_an_effect(self):
        path,_=self.start()
        with m.opened(path) as mission:
            s=mission.state();s['phase']='BATCH_APPROVED';mission.store.db.execute('UPDATE mission SET payload=?',(sim.canonical(s),))
            with self.assertRaisesRegex(ValueError,'outside'):mission.advance()
            # Repair the intentionally damaged test fixture for bounded teardown.
            s['phase']='AWAITING_BATCH_APPROVAL';mission.store.db.execute('UPDATE mission SET payload=?',(sim.canonical(s),))
    def test_public_audit_is_read_only_and_detects_saved_mutation(self):
        path,_=self.start();self.incident(path);self.recover(path)
        before={str(p.relative_to(path)):c.sha(p) for p in path.rglob('*') if p.is_file() and p.suffix!='.log'}
        with mock.patch.object(c,'open_broker',side_effect=AssertionError('private runtime accessed')):
            self.assertEqual(m.verify(path)['status'],'CONSISTENT')
        self.assertEqual(before,{str(p.relative_to(path)):c.sha(p) for p in path.rglob('*') if p.is_file() and p.suffix!='.log'})
        raw=m.sessions.read_file(path,'summary.json');raw['model_calls']=999;m.workspace.write_json(path/'summary.json',raw)
        with self.assertRaisesRegex(ValueError,'changed'):m.read(path)
    def test_governance_deferral_does_not_approve_or_execute_a_batch(self):
        path=m.create(self.root,ttl=180);self.paths.append(path);p=m.ScriptedRole('governance')
        p.ask=lambda ctx:(sim.canonical({'schema_version':m.GOVERNANCE_SCHEMA,'decision':'defer','queue_digest':ctx['queue_digest'],'order':[],'reason':'SCRIPTED insufficient scope.'}),{'finish_reason':'stop','refused':False})
        with m.opened(path) as mission:value=mission.advance({'factory':m.ScriptedRole('factory'),'governance':p})
        self.assertEqual(value['phase'],'GOVERNANCE_DEFERRED');self.assertEqual(value['service']['plans'],[]);self.assertEqual(value['service']['metrics']['committed'],60)
    def test_recovery_deferral_keeps_original_commitments_and_no_grant(self):
        path,_=self.start();self.execute(path)
        p=m.ScriptedRole('recovery');p.ask=lambda ctx:(sim.canonical({'schema_version':m.RECOVERY_SCHEMA,'decision':'defer','based_on_incident':ctx['based_on_incident'],'reason':'SCRIPTED review first.'}),{'finish_reason':'stop','refused':False})
        with m.opened(path) as mission:
            mission.simulate_failure();mission.observe();mission.observe();value=mission.advance({'recovery':p})
        self.assertEqual(value['phase'],'RECOVERY_DEFERRED');self.assertNotIn('recovery_grant',value);self.assertEqual(value['service']['metrics']['committed'],65)
    def test_mixed_roles_or_settings_cannot_spend_or_start_a_worker(self):
        path=m.create(self.root,ttl=180);self.paths.append(path)
        with m.opened(path) as mission:
            with self.assertRaises(ValueError):mission.advance({'factory':LiveRole('factory')})
        self.assertEqual(m.read(path)['calls'],[]);self.assertFalse((path/'service').exists())
    def test_fourth_or_repeated_role_attempt_is_blocked_before_provider_access(self):
        path,providers=self.start(live=True);self.incident(path,providers);self.recover(path)
        with m.opened(path) as mission:
            with self.assertRaises(ValueError):mission.call('recovery',{},providers['recovery'])
        self.assertEqual(sum(len(p.calls) for p in providers.values()),3)
    def test_lost_stop_reply_retains_capacity_until_exact_owner_recovery_reconciliation(self):
        path,_=self.start();self.incident(path);self.recover(path)
        with m.opened(path) as mission:
            p,phrase=mission.owner_proposal('withdraw_route');mission.approve_owner(p,phrase,'SCRIPTED C OWNER FIXTURE')
            original=d.Runtime.operate;lost=[]
            def operation(runtime,request):
                result=original(runtime,request)
                if request['tool']=='stop_candidate' and not lost:lost.append(True);raise OSError('MOCK reply lost after native stop')
                return result
            p,phrase=mission.owner_proposal('release_dependency')
            with mock.patch.object(d.Runtime,'operate',operation):value=mission.approve_owner(p,phrase,'SCRIPTED C OWNER FIXTURE')
            self.assertEqual(value['phase'],'OWNER_APPLYING');self.assertEqual(value['service']['metrics']['committed'],60)
            self.assertTrue(value['service']['actual_at_outcome']['workers']['candidate-v2']['exit_confirmed'])
            self.assertTrue(value['service']['pending_external']);before=value['service']['actual_at_outcome']
            value=mission.advance()
        self.assertEqual(value['phase'],'RECOVERED_SERVICE_ACTIVE');self.assertEqual(value['service']['metrics']['committed'],40)
        self.assertEqual(value['service']['actual_at_outcome']['candidate_starts'],1);self.assertEqual(value['service']['actual_at_outcome']['candidate_stops'],1)
        self.assertEqual(value['service']['runtime_id'],before['runtime_id']);self.assertEqual(m.verify(path)['status'],'CONSISTENT')
    def test_a_recovery_prefixed_foreign_intent_is_blocked_before_reconciliation(self):
        path,_=self.start();self.incident(path);self.recover(path)
        with m.opened(path) as mission,mission.broker() as broker:
            grant=mission.state()['recovery_grant']
            with mock.patch.object(broker.engine,'pending',return_value=[('recovery-foreign',)]),mock.patch.object(broker.engine,'reconcile') as reconcile:
                with self.assertRaises(sim.Denied) as caught:mission.recover_operation(broker,grant)
                self.assertEqual(caught.exception.code,'FOREIGN_PENDING_INTENT');reconcile.assert_not_called()
    def test_an_unbound_existing_service_cannot_be_adopted_as_this_mission(self):
        path=m.create(self.root,ttl=180);self.paths.append(path)
        with m.opened(path) as mission:
            content=mission.call('factory',{},m.ScriptedRole('factory'));mission.accept('factory',json.loads(content),'FACTORY_READY')
            other=path/'service'/'unbound';other.mkdir(parents=True);(other/'summary.json').write_text('{}')
            with self.assertRaises(ValueError):mission.bootstrap()
            self.assertIsNone(mission.state()['child'])
    def test_linked_service_root_is_rejected_before_workers_start(self):
        path=m.create(self.root,ttl=180);self.paths.append(path);outside=self.root/'outside';outside.mkdir()
        with linked_path(path/'service',outside,directory=True):
            with m.opened(path) as mission:
                content=mission.call('factory',{},m.ScriptedRole('factory'));mission.accept('factory',json.loads(content),'FACTORY_READY')
                with self.assertRaisesRegex(ValueError,'root cannot be a link'):mission.bootstrap()
            self.assertEqual(list(outside.iterdir()),[])
    def test_three_sdk_clients_send_distinct_role_prompts_and_use_the_existing_nebius_model(self):
        requests=[];inits=[]
        def create(**kwargs):
            requests.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='{}',refusal=None))],usage=SimpleNamespace(prompt_tokens=3,completion_tokens=5,total_tokens=8),model=m.factory.inference.MODEL,id='mock-no-api-request')
        def client(**kwargs):inits.append(kwargs);return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch.dict(sys.modules,{'openai':SimpleNamespace(OpenAI=client)}):
            for role in m.ROLES:
                provider=m.RoleClient(role,'LOCAL_TEST_KEY',8192,True);content,metadata=provider.ask({'role_context':role})
                self.assertEqual(metadata['total_tokens'],8);self.assertNotIn('LOCAL_TEST_KEY',content)
        self.assertEqual([r['messages'][0]['content'] for r in requests],[m.PROMPTS[r] for r in m.ROLES])
        self.assertEqual(len(inits),3);self.assertTrue(all(i['max_retries']==0 and i['base_url']==m.factory.inference.BASE_URL for i in inits))
        self.assertTrue(all(r['model']==m.factory.inference.MODEL for r in requests))
    def test_health_deadline_stops_new_probes_before_a_full_slow_window(self):
        clock=[0];calls=[];state=sim.initial_state();runtime={'runtime_id':'r','revision':1,'candidate_percent':0,'route_owner':None,'workers':{}}
        def probe(target):calls.append(target);clock[0]+=11;return {'ok':True,'body':{'result':49}}
        b=SimpleNamespace(guard=lambda:None,engine=SimpleNamespace(state=lambda:copy.deepcopy(state),client=SimpleNamespace(state=lambda:runtime,infer=probe)))
        with mock.patch.object(m.time,'time',side_effect=lambda:clock[0]):
            with self.assertRaisesRegex(ValueError,'deadline'):m.measure(b,1)
        self.assertEqual(len(calls),3)
    def test_noninteractive_live_cli_and_effect_commands_stop_before_credentials_or_workers(self):
        for action in ('start','demo','approve','recover','owner','stop','simulate-failure'):
            cmd=[sys.executable,'-B','-S',str(m.ROOT/'mission_control.py'),action,'--output',str(self.root)]
            if action in {'start','demo'}:cmd.append('--live')
            result=subprocess.run(cmd,input='',capture_output=True,text=True,env=d.restart.child_env(),timeout=10)
            self.assertNotEqual(result.returncode,0);self.assertIn('interactive terminal',result.stderr)
        self.assertEqual(list(self.root.iterdir()),[])

if __name__=='__main__':unittest.main()
