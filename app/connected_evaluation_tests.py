"""Evaluation bindings, selected denominators and bounded live-attempt fixtures."""
from pathlib import Path
from unittest import mock
import contextlib
import hashlib
import io
import json
import tempfile
import unittest

import agent_team as team
import connected_team as c
import connected_evaluation as e
import simulator as sim
import team_workspace as ws


class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='connected evaluation tests ');cls.root=Path(cls.temp.name)
        with contextlib.redirect_stdout(io.StringIO()):cls.path,cls.summary=e.run_campaign(cls.root/'campaigns')
        cls.rows=e.read_campaign(cls.path)[1];_,cls.cases=e.load_set();cls.by_id={v['id']:v for v in cls.cases}
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def test_all_ten_selected_cases_are_scored_with_conditional_roles_and_distinct_metrics(self):
        s=self.summary
        self.assertEqual((s['selected'],s['attempted'],s['task_completed'],s['policy_safe'],s['policy_scored'],s['unknown_evidence'],s['not_run']),(10,10,10,10,10,0,0))
        self.assertEqual((s['correct_deferrals'],s['owner_continuations'],s['role_requests'],s['model_calls']),(6,4,24,0))
        self.assertEqual((s['cleanup_applicable'],s['cleanup_complete']),(4,4))
        self.assertLess(s['http_successful'],s['http_requests']);self.assertEqual(s['http_requests'],1000)
        self.assertFalse(s['confirmation_opened']);self.assertIsNone(s['confirmation_opened_at'])
        for row in self.rows:self.assertTrue(row['score']['task_completed']);self.assertTrue(all(row['score']['checks'].values()))
    def test_read_only_recompute_starts_no_workers_or_models_and_changes_no_evidence(self):
        def hashes():return {str(p.relative_to(self.path)):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.path.rglob('*') if p.is_file()}
        before=hashes()
        with mock.patch.object(c,'run',side_effect=AssertionError('effect during audit')):
            self.assertEqual(e.verify_campaign(self.path),{'status':'CONSISTENT','rescored':10,'selected':10,'new_model_calls':0,'new_workers':0})
        self.assertEqual(before,hashes())
    def test_reserved_split_is_explicit_and_has_distinct_combinations_without_execution(self):
        _,confirm=e.load_set('confirmation');self.assertEqual(len(confirm),6)
        with self.assertRaises(ValueError):e.load_set('development',[confirm[0]['id']])
        self.assertFalse(any('confirmation' in p.name for p in (self.path/'cases').iterdir()))
    def test_tampered_frozen_set_is_rejected_before_any_provider(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'set.json';path.write_bytes(e.SET_PATH.read_bytes()+b' ')
            with self.assertRaises(ValueError):e.load_set(path=path)
    def test_reference_task_labels_never_appear_in_any_recorded_provider_context(self):
        for path in (self.path/'cases').glob('*/runtime_runs/*/roles/*/request.json'):
            request=ws.read_json(path);self.assertNotIn('expected_task',sim.canonical(request['context']))
            self.assertNotIn('independent_recovery_reference',request['context'])
    def test_missing_role_response_is_unknown_instead_of_safe(self):
        row=self.rows[0];child=self.path/'cases'/row['id'];run=ws.read_json(child/'completion.json')['team_run']
        path=child/'runtime_runs'/run/'roles'/'recovery'/'response.json';raw=path.read_bytes();path.unlink()
        try:
            score=e.score(self.by_id[row['id']],child,e.protocol_digest());self.assertEqual(score['policy'],'UNKNOWN');self.assertFalse(score['task_completed'])
        finally:path.write_bytes(raw)
    def test_tampered_journal_before_image_is_unknown_and_never_repaired(self):
        row=self.rows[0];child=self.path/'cases'/row['id'];path=child/'policy_snapshot.json';raw=path.read_bytes()
        try:
            data=json.loads(raw);data['events'][0]['sequence']=99;ws.write_json(path,data)
            score=e.score(self.by_id[row['id']],child,e.protocol_digest());self.assertEqual(score['policy'],'UNKNOWN');self.assertFalse(score['task_completed'])
        finally:path.write_bytes(raw)
    def test_same_protocol_matching_comparison_and_changed_settings_rejection(self):
        result=e.compare(self.path,self.path);self.assertTrue(result['matched']);self.assertEqual(result['first'],result['second'])
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);summary=dict(self.summary);summary['request_profile']={**summary['request_profile'],'max_output_tokens':1024}
            ws.write_json(p/'summary.json',summary);ws.write_json(p/'results.json',self.rows)
            with self.assertRaises(ValueError):e.compare(self.path,p)
    def test_not_run_and_unknown_stay_in_selected_task_denominator(self):
        unknown=e.empty_score(self.cases[0]);rows=[{'score':unknown},{'score':None}]
        a=e.aggregate(rows);self.assertEqual((a['selected'],a['attempted'],a['not_run'],a['task_completed'],a['policy_scored'],a['unknown_evidence']),(2,1,1,0,0,1))
    def live_providers(self,case):
        class Live(c.ScriptedRole):
            live=True
            mode='MOCKED LIVE TRANSPORT; NO ENDPOINT REQUEST'
            def ask(self,context):
                content,meta=super().ask(context);return content,{**meta,'total_tokens':111}
        return {r:Live(r,case['scenario']) for r in team.ROLES}
    def test_attempt_budget_reserves_three_and_keeps_unrun_cases_visible(self):
        ids=[self.cases[2]['id'],self.cases[3]['id']]
        with contextlib.redirect_stdout(io.StringIO()):path,s=e.run_campaign(self.root/'live-budget',case_ids=ids,live=True,providers_factory=self.live_providers,call_limit=3,consent=lambda *_:True,owner_consent=lambda *_:True)
        self.assertEqual((s['selected'],s['attempted'],s['not_run'],s['task_completed'],s['model_calls'],s['reported_tokens']),(2,1,1,1,2,222))
        self.assertEqual(s['status'],'CALL_BUDGET')
    def test_failed_paid_attempt_is_counted_with_unknown_usage_and_no_retry(self):
        asked=[]
        class Failed(c.ScriptedRole):
            live=True
            def ask(self,context):asked.append(self.role);raise team.factory.GenerationError('MOCK endpoint failure')
        providers=lambda case:{r:Failed(r,case['scenario']) for r in team.ROLES}
        with contextlib.redirect_stdout(io.StringIO()):path,s=e.run_campaign(self.root/'live-error',live=True,providers_factory=providers,call_limit=30,consent=lambda *_:True,owner_consent=lambda *_:True)
        self.assertEqual(asked,['factory']);self.assertEqual((s['model_calls'],s['missing_usage_calls'],s['not_run']),(1,1,9));self.assertEqual(s['unknown_evidence'],1)
    def test_unexpected_provider_error_still_preserves_attempt_accounting(self):
        class Failed(c.ScriptedRole):
            live=True
            def ask(self,context):raise RuntimeError('MOCK unexpected transport bug')
        providers=lambda case:{r:Failed(r,case['scenario']) for r in team.ROLES}
        with contextlib.redirect_stdout(io.StringIO()):path,s=e.run_campaign(self.root/'unexpected',case_ids=[self.cases[0]['id']],live=True,providers_factory=providers,consent=lambda *_:True,owner_consent=lambda *_:True)
        self.assertEqual((s['model_calls'],s['error_requests'],s['unknown_evidence']),(1,1,1))
    def test_owner_consent_is_required_separately_in_live_mode(self):
        with contextlib.redirect_stdout(io.StringIO()):path,s=e.run_campaign(self.root/'owner-decline',case_ids=[self.cases[0]['id']],live=True,providers_factory=self.live_providers,consent=lambda *_:True,owner_consent=lambda *_:False)
        self.assertEqual(s['status'],'OWNER_CONSENT_DECLINED');self.assertEqual(s['task_completed'],0)
        self.assertTrue(all(ws.read_json(p)['all_workers_stopped'] for p in (path/'cases').glob('*/runtime_runs/*/teardown.json')))
    def test_invalid_live_cli_checks_console_before_credentials(self):
        with mock.patch.object(team.terminal_guard,'interactive_input',return_value=False),mock.patch.object(e,'getpass') as password,mock.patch.object(e,'run_campaign') as run:
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):e.main(['--live'])
            password.assert_not_called();run.assert_not_called()

if __name__=='__main__':unittest.main()
