"""Original three-role execution continues into owner review and restart."""
from pathlib import Path
from unittest import mock
import contextlib
import http.client
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import agent_team as team
import connected_team as c
import durable_runtime as d
import runtime_sessions as sessions
import service_agent as service
import simulator as sim
import team_workspace as ws


class ConnectedTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory(prefix='connected team tests ');self.root=Path(self.temp.name);self.paths=[];self.hosts=[]
    def tearDown(self):
        for host in self.hosts:
            try:host.close('TEST_COMPLETE')
            except Exception:pass
        for path in self.paths:
            try:
                if not (path/'teardown.json').exists():c.restart_session(path)
                result=c.stop(path);self.assertTrue(result['teardown']['all_workers_stopped'])
            except ValueError:self.assertFalse((path/'connected_registration.json').exists())
        self.temp.cleanup()
    def start(self,family='routing',**kwargs):
        with contextlib.redirect_stdout(io.StringIO()):path,summary=c.run(self.root,scenario=family,ttl=150,**kwargs)
        self.paths.append(path);self.assertTrue(summary['full_three_role_flow_completed'],summary)
        return path,summary
    def kill_owner(self,path):
        view=c.Client(path).status();process=d.CHILDREN[view['supervisor_pid']];process.kill();process.wait(5);return view
    def host(self,path):self.kill_owner(path);host=c.Host(path);self.hosts.append(host);return host
    def request(self,host,decision):
        view=host.publish();digest=view['current_digest']
        return {'session_id':host.directory.name,'decision_key':'C-test','decision':decision,'note':'Exact test owner decision.',
                'expected_digest':digest,'consent':sessions.decision_phrase(host.directory.name,decision,digest),'consent_source':'SCRIPTED LOCAL LAB FIXTURE'}
    def test_both_original_teams_continue_on_same_database_and_real_workers(self):
        for family,decision in [('routing','withdraw_route'),('dependency','release_dependency')]:
            with self.subTest(family=family):
                path,summary=self.start(family);client=c.Client(path);before=client.status();original=(path/'team_summary.json').read_bytes();outcome=(path/'team_outcome.json').read_bytes()
                self.assertEqual(summary['role_requests'],3);self.assertEqual(summary['model_calls'],0)
                self.assertEqual(before['runtime_id'],json.loads(outcome)['actual_at_outcome']['runtime_id'])
                self.assertEqual(before['actual_at_outcome']['workers'],json.loads(outcome)['actual_at_outcome']['workers'])
                self.assertEqual(before['metrics']['committed'],40)
                result=client.request('/review',c.request_for(path,decision));self.assertEqual(result['recovery']['status'],'VERIFIED_RECOVERED')
                after=c.Client(path).status();self.assertEqual(after['metrics']['committed'],30)
                self.assertIn('deploy-A',after['policy_snapshot']['state']['paused_runs']);self.assertTrue(after['actual_at_outcome']['workers']['candidate-v2']['exit_confirmed'])
                self.assertEqual((path/'team_summary.json').read_bytes(),original);self.assertEqual((path/'team_outcome.json').read_bytes(),outcome)
                self.assertTrue(c.stop(path)['teardown']['all_workers_stopped'])
    def test_killing_both_supervisors_restarts_same_state_and_never_replaces_workers(self):
        path,_=self.start();before=self.kill_owner(path);process=d.CHILDREN[before['actual_at_outcome']['supervisor_pid']];process.kill();process.wait(5)
        view,_=c.restart_session(path);self.assertEqual(view['runtime_id'],before['runtime_id']);self.assertEqual(view['actual_at_outcome']['workers'],before['actual_at_outcome']['workers'])
        self.assertEqual(view['policy_snapshot'],before['policy_snapshot']);self.assertEqual(view['metrics']['committed'],40)
        result=c.Client(path).request('/review',c.request_for(path,'withdraw_route'));self.assertEqual(result['recovery']['status'],'VERIFIED_RECOVERED')
    def test_separate_python_client_reconnects_without_recreating_workers(self):
        path,_=self.start('dependency');before=c.Client(path).status()
        code='import json,sys,connected_team as c;from pathlib import Path;s=c.Client(Path(sys.argv[1])).status();print(json.dumps({"runtime":s["runtime_id"],"workers":s["actual_at_outcome"]["workers"]}))'
        result=subprocess.run([sys.executable,'-B','-c',code,str(path)],cwd=c.ROOT,env=d.restart.child_env(),capture_output=True,text=True,timeout=12)
        self.assertEqual(result.returncode,0,result.stderr);value=json.loads(result.stdout)
        self.assertEqual(value['runtime'],before['runtime_id']);self.assertEqual(value['workers'],before['actual_at_outcome']['workers'])
    def test_retain_is_explicit_and_does_not_change_capacity_route_or_workers(self):
        path,_=self.start();before=c.Client(path).status();result=c.Client(path).request('/review',c.request_for(path,'retain'));after=c.Client(path).status()
        self.assertEqual(result['code'],'RETAINED_NO_EFFECTS');self.assertEqual(before['actual_at_outcome'],after['actual_at_outcome'])
        self.assertEqual(before['policy_snapshot']['state'],after['policy_snapshot']['state'])
    def test_owner_crash_at_each_phase_resumes_one_recorded_decision_without_new_consent(self):
        for family in ('routing','dependency'):
            for phase in ('owner_intent','owner_effect','recovery_effect'):
                with self.subTest(family=family,phase=phase):
                    path,_=self.start(family);self.kill_owner(path);view,p=c.launch_review(path,crash_at=phase)
                    req=c.request_for(path,'withdraw_route' if family=='routing' else 'release_dependency')
                    with self.assertRaises((OSError,http.client.HTTPException)):c.Client(path).request('/review',req)
                    p.wait(5);self.assertEqual(p.returncode,74)
                    c.restart_session(path);view=c.Client(path).status();self.assertIn(req['decision_key'],view['pending_reviews'])
                    result=c.Client(path).request('/resume',{'session_id':path.name,'decision_key':req['decision_key']})
                    self.assertEqual(result['recovery']['status'],'VERIFIED_RECOVERED',result)
                    after=c.Client(path).status();self.assertEqual(after['metrics']['committed'],30)
                    self.assertEqual(after['actual_at_outcome']['candidate_stops'],1)
                    replay=c.Client(path).request('/review',req);self.assertTrue(replay['replayed']);self.assertEqual(replay['recovery'],result['recovery'])
                    self.assertTrue(c.stop(path)['teardown']['all_workers_stopped'])
    def test_wrong_consent_and_stale_snapshot_have_no_owner_effect(self):
        path,_=self.start();host=self.host(path);req=self.request(host,'withdraw_route');before=host.runtime.state()
        with self.assertRaises(sim.Denied):host.review({**req,'consent':'generic yes'})
        service.trusted_change(host.engine,'OTHER_STATE_CHANGE',lambda s:s.update(clock=s['clock']+1))
        with self.assertRaises(sim.Denied):host.review(req)
        self.assertEqual(host.runtime.state(),before);self.assertEqual(host.engine.db.execute('SELECT COUNT(*) FROM session_reviews').fetchone()[0],0)
    def test_backend_crash_after_real_stop_keeps_capacity_until_receipt_reconciliation(self):
        path,_=self.start();view=c.Client(path).status();backend=d.CHILDREN[view['actual_at_outcome']['supervisor_pid']]
        backend.kill();backend.wait(5)
        runtime,crashing=d.launch(path/'runtime',restart_existing=True,crash_at='effect',crash_tool='stop_candidate')
        req=c.request_for(path,'withdraw_route')
        try:c.Client(path).request('/review',req)
        except (OSError,http.client.HTTPException):pass
        crashing.wait(5);self.assertEqual(crashing.returncode,73)
        db=sqlite3.connect(path/'coordinator.sqlite3')
        try:state=json.loads(db.execute('SELECT payload FROM service_state WHERE id=1').fetchone()[0])
        finally:db.close()
        self.assertEqual(sim.total_commitments(state),40)
        self.assertEqual(state['deployments']['candidate-v2']['allocated_units'],10)
        c.restart_session(path)
        result=c.Client(path).request('/resume',{'session_id':path.name,'decision_key':req['decision_key']})
        self.assertEqual(result['recovery']['status'],'VERIFIED_RECOVERED',result)
        after=c.Client(path).status();self.assertEqual(after['metrics']['committed'],30)
        self.assertEqual(after['actual_at_outcome']['candidate_stops'],1)
    def test_revoked_current_C_approval_blocks_even_exact_owner_consent(self):
        path,_=self.start('dependency');host=self.host(path)
        service.trusted_change(host.engine,'REVOKE_CURRENT_C',lambda s:s['approvals']['AP-route-C'].update(revoked=True))
        req=self.request(host,'release_dependency');before=host.engine.state()
        with self.assertRaises(sim.Denied):host.review(req)
        self.assertEqual(host.engine.state(),before)
    def test_source_change_blocks_effect_but_historical_evidence_remains_readable(self):
        path,_=self.start();host=self.host(path);req=self.request(host,'withdraw_route')
        with mock.patch.object(c,'implementation_digest',return_value='changed'):
            with self.assertRaises(sim.Denied):host.review(req)
            self.assertTrue(c.saved_view(path)['historical_controller'])
            with self.assertRaises(ValueError):c.validate_registration(path)
    def test_other_legitimate_dependency_is_preserved_after_C_releases_its_own(self):
        path,_=self.start('dependency');host=self.host(path)
        other={'id':'D-legitimate-job','owner_run':'route-D'}
        service.trusted_change(host.engine,'OTHER_DEPENDENCY',lambda s:s['deployments']['candidate-v2']['dependencies'].append(other))
        result=host.review(self.request(host,'release_dependency'));self.assertEqual(result['recovery']['status'],'RECOVERY_REVIEW_REQUIRED')
        state=host.engine.state();self.assertIn(other,state['deployments']['candidate-v2']['dependencies']);self.assertEqual(sim.total_commitments(state),40)
        self.assertTrue(host.runtime.state()['workers']['candidate-v2']['alive'])
    def test_registration_and_journal_tampering_cannot_be_repaired_into_a_pass(self):
        path,_=self.start();raw=(path/'team_summary.json').read_bytes()
        try:
            value=json.loads(raw);value['full_three_role_flow_completed']=False;ws.write_json(path/'team_summary.json',value)
            with self.assertRaises(ValueError):c.saved_view(path)
        finally:(path/'team_summary.json').write_bytes(raw)
    def test_two_slots_are_reserved_before_calling_any_provider(self):
        first=c.reserve_directory(self.root,120);second=c.reserve_directory(self.root,120)
        with mock.patch.object(c.ScriptedRole,'ask') as provider:
            with self.assertRaisesRegex(ValueError,'Two unexpired'):c.run(self.root)
            provider.assert_not_called()
    def test_invalid_live_cli_is_rejected_before_credentials_or_worker_access(self):
        with mock.patch.object(team.terminal_guard,'interactive_input',return_value=False),mock.patch.object(c,'getpass') as password,mock.patch.object(c,'run') as run:
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):c.main(['start','--live'])
            password.assert_not_called();run.assert_not_called()
    def test_private_tokens_do_not_appear_in_public_team_or_session_evidence(self):
        path,_=self.start();client=c.Client(path);view=client.status();text=json.dumps(view)+(path/'team_summary.json').read_text()+(path/'team_outcome.json').read_text()
        self.assertNotIn(client.access['token'],text);self.assertNotIn(d.Runtime(path/'runtime').token,text)
    def test_exact_request_key_cannot_be_reused_for_a_different_owner_action(self):
        path,_=self.start();host=self.host(path);req=self.request(host,'retain');host.review(req)
        with self.assertRaises(sim.Denied):host.review({**req,'decision':'withdraw_route'})

if __name__=='__main__':unittest.main()
