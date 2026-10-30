from pathlib import Path
from unittest import mock
import contextlib
import io
import tempfile
import unittest

import change_coordinator as c
import coordination_benchmark as b
import simulator as sim

class BenchmarkTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_all_twelve_baseline_cases_are_explicit_policy_only_and_optimal(self):
        p,s=b.run(self.root)
        self.assertEqual((s['selected'],s['valid_proposals'],s['optimal'],s['not_run'],s['model_calls'],s['worker_processes_started']),(12,12,12,0,0,0))
        self.assertTrue(s['benchmark_valid']);self.assertEqual(b.verify(p)['status'],'CONSISTENT')
    def provider(self,fail=None,optimal=False):
        class Fake:
            live=True;request_settings={'max_tokens':8192}
            def ask(self,context):
                if fail:raise fail('MOCK endpoint failure')
                order=[i['id'] for i in context['requests']]
                if optimal:order=c.plan_orders(context['current_state'],context['requests'],b.AT)['selected']['order']
                return sim.canonical({'schema_version':c.PLANNER_SCHEMA,'queue_digest':context['queue_digest'],'order':order,'reason':'MOCK provider ordering.'}),{'finish_reason':'stop','refused':False,'total_tokens':123}
        return Fake()
    def test_live_fixture_budget_keeps_unrun_cases_in_selected_denominator(self):
        p,s=b.run(self.root,self.provider(),call_limit=1)
        self.assertEqual((s['selected'],s['attempted'],s['not_run'],s['model_calls'],s['reported_tokens']),(12,1,11,1,123))
        self.assertEqual(s['status'],'CALL_BUDGET');self.assertEqual(s['optimal'],0);self.assertGreater(s['total_priority_regret'],0)
        self.assertEqual(b.verify(p)['status'],'CONSISTENT')
    def test_failed_attempt_counts_unknown_usage_without_retry_or_substitution(self):
        p,s=b.run(self.root,self.provider(c.factory.GenerationError),call_limit=12)
        self.assertEqual((s['model_calls'],s['missing_usage_calls'],s['error_requests'],s['not_run']),(1,1,1,11));self.assertEqual(s['valid_proposals'],0)
    def test_unexpected_provider_bug_is_recorded_and_stops_later_cases(self):
        p,s=b.run(self.root,self.provider(RuntimeError));self.assertEqual((s['model_calls'],s['not_run']),(1,11));self.assertEqual(s['status'],'RUNNER_ERROR')
    def test_provider_context_never_receives_the_optimal_reference_or_scores(self):
        seen=[];provider=self.provider();original=provider.ask
        def ask(context):seen.append(context);return original(context)
        provider.ask=ask;b.run(self.root,provider,case_ids=[b.load_set()[1][0]['id']])
        self.assertEqual(len(seen),1)
        for key in ('reference','selected','reference_objective','optimal','expected_task'):self.assertNotIn(key,seen[0])
    def test_pinned_set_tampering_blocks_inference(self):
        path=self.root/'changed.json';path.write_bytes(b.SET_PATH.read_bytes()+b' ')
        with self.assertRaises(ValueError):b.load_set(path=path)
    def test_audit_changes_no_files_and_calls_no_provider_or_runtime(self):
        p,s=b.run(self.root,case_ids=[b.load_set()[1][0]['id']]);before={str(x.relative_to(p)):c.sha(x) for x in p.rglob('*') if x.is_file()}
        with mock.patch.object(c.durable,'Runtime',side_effect=AssertionError('worker capability read')),mock.patch.object(c.Planner,'ask',side_effect=AssertionError('model call')):
            self.assertEqual(b.verify(p)['status'],'CONSISTENT')
        self.assertEqual(before,{str(x.relative_to(p)):c.sha(x) for x in p.rglob('*') if x.is_file()})
    def test_live_cli_checks_console_before_accessing_key_or_creating_run(self):
        with mock.patch.object(c.terminal_guard,'interactive_input',return_value=False),mock.patch.object(b,'getpass') as key,mock.patch.object(b,'run') as run:
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):b.main(['--live'])
            key.assert_not_called();run.assert_not_called()

if __name__=='__main__':unittest.main()
