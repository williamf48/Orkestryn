"""Regression checks for crashes, timeouts, and contradictory worker reports."""
import importlib.util
from collections import Counter
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('offline_test_runner', Path(__file__).with_name('test.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='runner diagnostics ')
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)

    def worker(self, text):
        script = self.output / 'worker.py'
        script.write_text(text, encoding='utf-8')
        return script

    def test_timed_out_process_retains_output_and_failed_report(self):
        script = self.worker("import time\nprint('worker started', flush=True)\ntime.sleep(30)\n")
        result = runner.run_one('fixture', self.output, timeout=1, runner=script)
        self.assertFalse(result['passed'])
        self.assertEqual(result['status'], 'TIMED_OUT')
        self.assertIn('worker started', (self.output / 'fixture.log').read_text())
        self.assertEqual(json.loads((self.output / 'fixture.json').read_text()), result)

    def test_crashed_worker_cannot_pass_without_report(self):
        result = runner.run_one('fixture', self.output, runner=self.worker("raise RuntimeError('fixture crash')\n"))
        self.assertFalse(result['passed'])
        self.assertEqual(result['status'], 'MISSING_REPORT')
        self.assertEqual(result['exit_code'], 1)
        self.assertIn('fixture crash', (self.output / 'fixture.log').read_text())

    def test_nonzero_exit_overrules_a_claimed_pass(self):
        script = self.worker("import json, pathlib, sys\noutput=pathlib.Path(sys.argv[sys.argv.index('--output')+1])\n(output/'fixture.json').write_text(json.dumps({'module':'fixture','passed':True,'status':'PASSED','tests_run':1,'failures':0,'errors':0,'skipped':0}))\nraise SystemExit(7)\n")
        result = runner.run_one('fixture', self.output, runner=script)
        self.assertFalse(result['passed'])
        self.assertEqual(result['errors'], 1)
        self.assertEqual(result['exit_code'], 7)

    def test_import_error_is_recorded_with_traceback(self):
        with mock.patch.object(runner.importlib, 'import_module', side_effect=ImportError('fixture missing dependency')):
            self.assertEqual(runner.run_module('desktop_tests', self.output), 1)
        result = json.loads((self.output / 'desktop_tests.json').read_text())
        self.assertFalse(result['passed'])
        self.assertEqual(result['status'], 'MODULE_ERROR')
        self.assertIn('fixture missing dependency', result['detail'])

    def test_subtest_error_is_visible_before_the_next_test_runs(self):
        log = io.StringIO()
        class Example(unittest.TestCase):
            def test_a_error(self):
                with self.subTest(family='dependency'):
                    raise RuntimeError('fixture child failed')
            def test_b_inspect(self):
                self.assertIn('Immediate diagnostic:', log.getvalue())
                self.assertIn('RuntimeError: fixture child failed', log.getvalue())
        result = unittest.TextTestRunner(stream=log, buffer=True, resultclass=runner.DiagnosticResult).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(Example))
        self.assertEqual(result.testsRun, 2)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.failures, [])

    def test_large_registered_classes_cover_every_method_exactly_once(self):
        for name in runner.PARTITIONS:
            module = runner.importlib.import_module(name)
            case = getattr(module, runner.project.MODULES[name])
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(case)
            expected = Counter(test.id() for test in suite)
            selected = []
            reports = []
            for task_name, partition in runner.worker_tasks([name]):
                part = runner.partition_suite(suite, partition)
                selected.extend(test.id() for test in part)
                reports.append(runner.result_name(task_name, partition))
                self.assertGreater(part.countTestCases(), 0)
            self.assertEqual(Counter(selected), expected)
            self.assertEqual(len(reports), len(set(reports)))


if __name__ == '__main__':
    unittest.main()
