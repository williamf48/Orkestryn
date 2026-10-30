"""Independent scheduling, exact batch consent and actual receipt/restart tests."""
from pathlib import Path
from unittest import mock
import contextlib
import copy
import http.client
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

import change_coordinator as c
import coordination_benchmark as bench
import coordination_reference as reference
import durable_runtime as d
import simulator as sim
import team_workspace as ws


def base():return bench.fixture({'profile':'balanced'})


class PlannerTests(unittest.TestCase):
    def test_optimizer_completes_small_request_and_budget_while_fifo_cannot(self):
        state,intents=base();report=c.plan_orders(state,intents,bench.AT)
        self.assertEqual(report['orders_compared'],120);self.assertEqual(report['selected']['objective'],[15,4,-5]);self.assertEqual(report['fifo']['objective'],[11,3,-20])
        self.assertEqual(report['selected']['committed_units'],65);self.assertEqual(report['selected']['capacity_limit'],70)
        self.assertEqual(report['selected']['waiting'][0]['blocking_facts']['extra_headroom_needed'],15)
        self.assertEqual(report['selected']['waiting'][0]['blocking_facts']['approved_budget_version'],1)
        self.assertEqual(report['selected']['waiting'][0]['blocking_facts']['current_budget_version'],2)
    def test_priority_is_explicit_and_can_prefer_deployment_over_cost_reduction(self):
        state,intents=bench.fixture({'profile':'deployment-first'});r=c.plan_orders(state,intents,bench.AT)
        self.assertIn('deploy-large',r['selected']['accepted']);self.assertNotIn('budget-70',r['selected']['accepted'])
    def test_only_one_competing_route_wins_and_no_existing_owner_is_overwritten(self):
        for profile in ('competing-routes','existing-C-owner'):
            state,intents=bench.fixture({'profile':profile});r=c.plan_orders(state,intents,bench.AT)
            routes=[i for i in intents if i['tool']=='set_route' and i['id'] in r['selected']['accepted']];self.assertEqual(len(routes),1)
            if profile=='existing-C-owner':self.assertEqual(routes[0]['actor'],'C');self.assertEqual(r['selected']['final_state']['route_owner'],'route-C')
    def test_revoked_approval_paused_A_and_stale_metrics_are_respected(self):
        for profile,blocked in [('approval-revoked',{'deploy-large','deploy-small'}),('A-paused',{'deploy-large','deploy-small'}),('stale-measurements',{'customer-route'}),('unsafe-fallback',{'customer-route'})]:
            with self.subTest(profile=profile):
                state,intents=bench.fixture({'profile':profile});r=c.plan_orders(state,intents,bench.AT)
                self.assertTrue(blocked.isdisjoint(r['selected']['accepted']));self.assertTrue(r['selected']['reference_agrees'])
    def test_age_bonus_is_capped_and_ties_are_deterministic(self):
        state,intents=bench.fixture({'profile':'aged-small-request'});a=c.plan_orders(state,intents,bench.AT);b=c.plan_orders(state,intents,bench.AT)
        self.assertEqual(a,b);self.assertEqual(a['weights']['deploy-small'],4)
    def test_independent_reference_catches_an_engine_that_miscounts_reserved_units(self):
        state,intents=base();original=sim.Engine._execute
        def incorrect(engine,s,actor,run,tool,args,plan):
            result=original(engine,s,actor,run,tool,args,plan)
            if tool=='reserve_capacity':s['reservations'][args['reservation_id']]['units']+=1
            return result
        with mock.patch.object(sim.Engine,'_execute',incorrect),self.assertRaisesRegex(ValueError,'reference disagrees'):c.trial(state,intents[0])
    def test_planning_never_mutates_input_states_or_grants_authority(self):
        state,intents=base();before=copy.deepcopy((state,intents));c.plan_orders(state,intents,bench.AT)
        self.assertEqual((state,intents),before)
    def test_only_registered_integer_payloads_and_caller_owned_tools_are_admitted(self):
        good={'id':'request','tool':'reserve_capacity','parameters':{'units':5},'priority':3}
        bad=[{**good,'actor':'B'},{**good,'parameters':{'units':True}},{**good,'parameters':{'units':5,'approval':'yes'}},{**good,'tool':'stop_candidate'},{**good,'priority':float('nan')},{**good,'id':'../escape'}]
        for value in bad:
            with self.subTest(value=value),self.assertRaises(ValueError):c.validate_intent('A',value)
        with self.assertRaises(ValueError):c.validate_intent('C',good)
    def test_unknown_duplicate_and_omitted_model_order_ids_are_rejected(self):
        state,intents=base();basis={'intents':intents,'queue_digest':sim.digest(intents)}
        good={'schema_version':c.PLANNER_SCHEMA,'queue_digest':basis['queue_digest'],'order':[i['id'] for i in intents],'reason':'Ordering only.'}
        for change in ({'order':['shell']},{'order':[good['order'][0]]*5},{'queue_digest':'old'},{'authority':'approve'},{'reason':''}):
            with self.subTest(change=change),self.assertRaises((ValueError,c.factory.ProposalError)):c.decode_proposal(sim.canonical({**good,**change}),basis)
    def test_conflict_edges_explain_shared_capacity_and_exclusive_route_writes(self):
        state,intents=bench.fixture({'profile':'competing-routes'});edges=c.conflicts(intents)
        self.assertTrue(any(set((e['from'],e['to']))=={'deploy-large','budget-70'} for e in edges))
        self.assertTrue(any(set((e['from'],e['to']))=={'A-traffic','customer-route'} for e in edges))
    def test_maximum_six_requests_is_enforced_before_search(self):
        state,intents=base()
        with self.assertRaises(ValueError):c.plan_orders(state,intents+[intents[0],intents[0]])
    def test_ordering_cannot_resume_a_workflow_or_remove_a_dependency(self):
        for tool in ('remove_dependency','resume','release_capacity','approve'):
            with self.assertRaises(ValueError):c.validate_intent('A',{'id':'request','tool':tool,'parameters':{},'priority':3})


class BrokerTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory(prefix='coordination broker tests ');self.root=Path(self.temp.name);self.paths=[]
    def tearDown(self):
        for path in self.paths:
            try:self.assertTrue(c.stop(path)['all_workers_stopped'])
            except (OSError,ValueError):
                d.launch(path/'runtime',restart_existing=True);self.assertTrue(c.stop(path)['all_workers_stopped'])
        self.temp.cleanup()
    def start(self):path=c.create(self.root,180);self.paths.append(path);return path
    def seed(self,b):
        for actor,intent in c.default_intents():b.submit(actor,intent)
    def approve(self,b,p):return b.approve(p['id'],c.approval_phrase(b.spec['session_id'],p['id'],p['plan_digest']),'SCRIPTED MANAGER FIXTURE')
    def test_exact_approved_batch_executes_four_requests_and_recovers_only_A_work(self):
        path=self.start()
        with c.open_broker(path) as b:
            self.seed(b);p=b.propose();before=b.engine.state();self.approve(b,p);r=b.execute(p['id']);self.assertEqual(r['status'],'COMPLETED')
            self.assertEqual(sim.total_commitments(b.engine.state()),65);self.assertEqual(b.engine.state()['capacity_limit'],70)
            self.assertEqual(b.engine.client.state()['candidate_percent'],20)
            c.service.trusted_change(b.engine,'TEST_WORKFLOW_FAILURE',lambda s:s['paused_runs'].append('deploy-A'))
            rec=b.engine.recover();self.assertEqual(rec['status'],'RECOVERY_REVIEW_REQUIRED');self.assertEqual(sim.total_commitments(b.engine.state()),60)
            self.assertIn({'id':'C-retained-job','owner_run':'route-C'},b.engine.state()['deployments']['candidate-v2']['dependencies'])
            self.assertTrue(b.engine.client.state()['workers']['candidate-v2']['alive']);self.assertIn('deploy-A',b.engine.state()['paused_runs']);c.publish(path,b)
        c.stop(path);self.assertEqual(c.verify_saved(path)['status'],'CONSISTENT')
    def test_generic_yes_wrong_digest_and_unapproved_execute_make_no_effect(self):
        path=self.start()
        with c.open_broker(path) as b:
            self.seed(b);p=b.propose();before=b.engine.state()
            for phrase in ('yes','APPROVE','APPROVE BATCH wrong'):
                with self.assertRaises(sim.Denied):b.approve(p['id'],phrase,'SCRIPTED MANAGER FIXTURE')
            with self.assertRaises(sim.Denied):b.execute(p['id'])
            self.assertEqual(b.engine.state(),before)
    def test_new_request_cancel_or_state_change_invalidates_reviewed_plan(self):
        path=self.start()
        with c.open_broker(path) as b:
            self.seed(b);p=b.propose();b.cancel('A','deploy-large')
            with self.assertRaises(sim.Denied):self.approve(b,p)
            p=b.propose();self.approve(b,p)
            c.service.trusted_change(b.engine,'EXTERNAL_CHANGE',lambda s:s.update(clock=s['clock']+1))
            with self.assertRaises(sim.Denied):b.execute(p['id'])
    def test_revoked_approval_after_plan_is_never_replaced_by_batch_consent(self):
        path=self.start()
        with c.open_broker(path) as b:
            self.seed(b);p=b.propose();self.approve(b,p)
            c.service.trusted_change(b.engine,'REVOKE_C',lambda s:s['approvals']['AP-route-C'].update(revoked=True))
            with self.assertRaises(sim.Denied):b.execute(p['id'])
    def test_submit_identity_is_separate_and_duplicate_content_is_idempotent(self):
        path=self.start()
        with c.open_broker(path) as b:
            actor,item=c.default_intents()[0];b.submit(actor,item);r=b.submit(actor,item);self.assertTrue(r['replayed']);self.assertEqual(len(b.ready()),1)
            with self.assertRaises(sim.Denied):b.submit(actor,{**item,'priority':5})
            with self.assertRaises(sim.Denied):b.cancel('C',item['id'])
    def test_stopped_service_cannot_admit_more_requests_even_before_lease_expiry(self):
        path=self.start();c.stop(path)
        with c.open_broker(path) as b:
            actor,item=c.default_intents()[0]
            with self.assertRaises(sim.Denied) as caught:b.submit(actor,item)
            self.assertEqual(caught.exception.code,'COORDINATOR_STOPPED');self.assertEqual(b.ready(),[])
    def test_cancel_cannot_undo_an_executed_request(self):
        path=self.start()
        with c.open_broker(path) as b:
            self.seed(b);p=b.propose();self.approve(b,p);b.execute(p['id'])
            with self.assertRaises(sim.Denied):b.cancel('A','deploy-small')
            self.assertEqual(sim.total_commitments(b.engine.state()),65)
    def test_three_separate_actor_clients_submit_concurrently_without_lost_requests(self):
        path=self.start();processes=[]
        for actor,item in c.default_intents()[:3]:
            processes.append(subprocess.Popen([sys.executable,'-B','-S',str(c.ROOT/'change_coordinator.py'),'submit','--output',str(self.root),'--session',path.name,
                '--actor',actor,'--id',item['id'],'--tool',item['tool'],'--parameters',sim.canonical(item['parameters']),'--priority',str(item['priority'])],env=d.restart.child_env(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
        for process in processes:
            out,err=process.communicate(timeout=15);self.assertEqual(process.returncode,0,out+err)
        with c.open_broker(path) as b:
            self.assertEqual(len(b.ready()),3);self.assertEqual(b.revision(),3);self.assertEqual({i['actor'] for i in b.ready()},{'A','B','C'})
            self.assertEqual(sim.total_commitments(b.engine.state()),60)
    def test_cancel_and_queue_revision_roll_back_together_if_journal_write_fails(self):
        path=self.start()
        with c.open_broker(path) as b:
            self.seed(b);p=b.propose();revision=b.revision()
            with mock.patch.object(b,'event',side_effect=RuntimeError('MOCK journal failure')):
                with self.assertRaises(RuntimeError):b.cancel('A','deploy-large')
            self.assertEqual(b.revision(),revision);self.assertIn('deploy-large',{i['id'] for i in b.ready()});self.approve(b,p)
    def test_fourth_live_planning_attempt_is_blocked_before_provider_access(self):
        class Proposal:
            live=True;request_settings={}
            def ask(self,context):return sim.canonical({'schema_version':c.PLANNER_SCHEMA,'queue_digest':context['queue_digest'],'order':[i['id'] for i in context['requests']],'reason':'MOCK ordering only.'}),{'finish_reason':'stop','refused':False}
        path=self.start();provider=Proposal()
        with c.open_broker(path) as b:self.seed(b)
        for _ in range(3):c.propose_live(path,provider)
        with mock.patch.object(provider,'ask',side_effect=AssertionError('fourth request sent')):
            with self.assertRaisesRegex(ValueError,'Three live'):c.propose_live(path,provider)
    def test_queue_limit_and_expiry_do_not_erase_committed_capacity(self):
        path=self.start()
        with c.open_broker(path) as b:
            for i in range(6):b.submit('A',{'id':'request-'+str(i),'tool':'reserve_capacity','parameters':{'units':1},'priority':2},ttl=1)
            with self.assertRaises(sim.Denied):b.submit('A',{'id':'overflow','tool':'reserve_capacity','parameters':{'units':1},'priority':2})
            before=b.engine.state()
            with mock.patch.object(c.time,'time',return_value=time.time()+2):self.assertEqual(len(b.expire()),6)
            self.assertEqual(b.engine.state(),before);self.assertEqual(b.ready(),[])
    def test_read_only_probe_keeps_reviewed_state_and_exact_arguments_after_long_review(self):
        path=self.start()
        with c.open_broker(path) as b:
            c.service.trusted_change(b.engine,'OLD_WALL_OBSERVATIONS',lambda s:s['deployments']['candidate-v2']['metrics'].update(observed_unix=time.time()-120))
            self.seed(b);p=b.propose();self.approve(b,p);before=b.engine.state();r=b.execute(p['id'])
            self.assertEqual(r['status'],'COMPLETED');self.assertEqual(b.engine.state()['deployments']['candidate-v2']['metrics'],before['deployments']['candidate-v2']['metrics'])
            self.assertTrue(any(e['kind']=='EXACT_STEP_PROBE' for e in b.status()['coordination_events']))
    def test_route_probe_for_other_arguments_cannot_be_reused(self):
        path=self.start()
        with c.open_broker(path) as b:
            self.seed(b);p=b.propose();step=next(s for s in p['choice']['actions'] if s['tool']=='set_route');b.engine.reviewed_plan_digest=p['plan_digest'];c.probe_step(b.engine,step,p['plan_digest'])
            r=sim.action(b.engine,'set_route',{**step['args'],'candidate_percent':10},'different-key','C','route-C')
            self.assertEqual(r['status'],'BLOCKED');self.assertEqual(r['code'],'POST_REVIEW_PROBE_MISMATCH');self.assertEqual(b.engine.client.state()['candidate_percent'],0)
    def test_actual_client_crashes_at_intent_effect_and_commit_resume_without_duplicate_actions(self):
        for phase in ('intent','effect','committed'):
            with self.subTest(phase=phase):
                path=self.start()
                with c.open_broker(path) as b:
                    self.seed(b);p=b.propose();self.approve(b,p)
                result=subprocess.run([sys.executable,'-B','-S',str(c.ROOT/'change_coordinator.py'),'execute','--output',str(self.root),'--session',path.name,'--plan',p['id'],'--crash-at',phase,'--crash-intent','customer-route'],env=d.restart.child_env(),capture_output=True,text=True,timeout=20)
                self.assertEqual(result.returncode,76,result.stdout+result.stderr)
                with c.open_broker(path) as b:
                    r=b.execute(p['id']);self.assertEqual(r['status'],'COMPLETED');actual=b.engine.client.state();self.assertEqual(actual['candidate_percent'],20)
                    receipts=b.db.execute("SELECT COUNT(*) FROM action_receipts WHERE action_key='coord-step-customer-route'").fetchone()[0];self.assertEqual(receipts,1)
                    b.execute(p['id']);self.assertEqual(b.engine.client.state()['revision'],actual['revision']);c.publish(path,b)
                self.assertEqual(c.verify_saved(path)['status'],'CONSISTENT');c.stop(path)
    def test_actual_runtime_death_after_route_effect_reconciles_exact_plan(self):
        path=self.start()
        with c.open_broker(path) as b:
            self.seed(b);p=b.propose();self.approve(b,p);pid=b.engine.client.state()['supervisor_pid']
        process=d.CHILDREN[pid];process.kill();process.wait(5)
        runtime,crashing=d.launch(path/'runtime',restart_existing=True,crash_at='effect',crash_tool='set_route')
        with c.open_broker(path) as b:
            r=b.execute(p['id']);self.assertEqual(r['status'],'WAITING_RECONCILIATION')
        crashing.wait(5);self.assertEqual(crashing.returncode,73);d.launch(path/'runtime',restart_existing=True)
        with c.open_broker(path) as b:
            r=b.execute(p['id']);self.assertEqual(r['status'],'COMPLETED');self.assertEqual(b.engine.client.state()['candidate_percent'],20);self.assertEqual(sim.total_commitments(b.engine.state()),65)
    def test_foreign_pending_runtime_intent_cannot_be_reconciled_as_this_batch(self):
        path=self.start()
        with c.open_broker(path) as b:
            self.seed(b);p=b.propose();self.approve(b,p)
            with mock.patch.object(b.engine,'pending',return_value=[('some-other-job',)]):
                with self.assertRaises(sim.Denied):b.execute(p['id'])
    def test_source_or_worker_drift_blocks_new_batch_effects(self):
        path=self.start()
        with c.open_broker(path) as b:
            self.seed(b);p=b.propose();self.approve(b,p)
            with mock.patch.object(c,'implementation_digest',return_value='changed'):
                with self.assertRaises(sim.Denied):b.execute(p['id'])
            before=b.engine.state();b.engine.client.fail_worker('candidate-v2')
            with self.assertRaises(sim.Denied):b.execute(p['id'])
            self.assertEqual(b.engine.state(),before)
    def test_counterfactual_units_preview_can_fit_all_five_without_modifying_original(self):
        path=self.start()
        with c.open_broker(path) as b:self.seed(b);p=b.propose();c.publish(path,b)
        def hashes():return {str(p.relative_to(path)):c.sha(p) for p in path.rglob('*') if p.is_file()}
        before=hashes();result=c.preview(path,p['plan_digest'],'deploy-large',5)
        self.assertEqual(len(result['result']['selected']['accepted']),5);self.assertFalse(result['live_effects']);self.assertTrue(result['requires_new_intent_and_exact_approval']);self.assertEqual(before,hashes())
    def test_saved_audit_recomputes_plans_without_contacts_or_writes(self):
        path=self.start()
        with c.open_broker(path) as b:self.seed(b);p=b.propose();self.approve(b,p);b.execute(p['id']);c.publish(path,b)
        c.stop(path);before={str(p.relative_to(path)):c.sha(p) for p in path.rglob('*') if p.is_file()}
        with mock.patch.object(d,'Runtime',side_effect=AssertionError('audit accessed runtime')):self.assertEqual(c.verify_saved(path)['status'],'CONSISTENT')
        self.assertEqual(before,{str(p.relative_to(path)):c.sha(p) for p in path.rglob('*') if p.is_file()})
    def test_live_terminal_guard_runs_before_key_or_service_access(self):
        with mock.patch.object(c.terminal_guard,'interactive_input',return_value=False),mock.patch.object(c,'getpass') as key,mock.patch.object(c,'create') as create:
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):c.main(['demo','--live'])
            key.assert_not_called();create.assert_not_called()
    def test_live_proposal_records_failure_attempt_with_no_retry_or_approval(self):
        class Failed:
            live=True;request_settings={}
            def ask(self,context):raise c.factory.GenerationError('MOCK transport failure')
        path=self.start()
        with c.open_broker(path) as b:self.seed(b)
        with self.assertRaises(c.factory.GenerationError):c.propose_live(path,Failed())
        with c.open_broker(path) as b:
            value=b.status();self.assertEqual(value['model_calls'],1);self.assertEqual(value['usage']['missing_usage_calls'],1);self.assertEqual(value['plans'],[])
    def test_live_proposal_never_grants_itself_execution_authority(self):
        class Proposal:
            live=True;request_settings={}
            def ask(self,context):return sim.canonical({'schema_version':c.PLANNER_SCHEMA,'queue_digest':context['queue_digest'],'order':[i['id'] for i in context['requests']],'reason':'MOCK order only.'}),{'finish_reason':'stop','refused':False,'total_tokens':123}
        path=self.start()
        with c.open_broker(path) as b:self.seed(b)
        p=c.propose_live(path,Proposal())
        with c.open_broker(path) as b:
            with self.assertRaises(sim.Denied):b.execute(p['id'])
            self.assertEqual(b.status()['usage']['reported_tokens'],123);self.assertEqual(sim.total_commitments(b.engine.state()),60)

if __name__=='__main__':unittest.main()
