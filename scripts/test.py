"""Run registered offline tests in isolated processes and retain every result."""
from pathlib import Path
import argparse
import concurrent.futures
import faulthandler
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'app'
sys.path.insert(0, str(APP))
import project


class DiagnosticResult(unittest.TextTestResult):
    """Keep earlier failures visible even if a later test hangs."""
    def report_now(self, test, error):
        self.stream.writeln('\nImmediate diagnostic: ' + test.id())
        self.stream.writeln(''.join(traceback.format_exception(*error)))
        self.stream.flush()

    def addError(self, test, error):
        super().addError(test, error)
        self.report_now(test, error)

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.report_now(test, error)

    def addSubTest(self, test, subtest, error):
        super().addSubTest(test, subtest, error)
        if error is not None:
            self.report_now(subtest, error)


def write_result(output, name, value):
    (output / (name + '.json')).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def error_result(name, status, detail):
    return {'module': name, 'passed': False, 'status': status, 'tests_run': 0,
            'failures': 0, 'errors': 1, 'skipped': 0, 'detail': detail}


def run_module(name, output):
    try:
        before = project.source_hashes()
        module = importlib.import_module(name)
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(getattr(module, project.MODULES[name]))
        expected = suite.countTestCases()
        faulthandler.dump_traceback_later(270)
        try:
            result = unittest.TextTestRunner(verbosity=2, buffer=True, resultclass=DiagnosticResult).run(suite)
        finally:
            faulthandler.cancel_dump_traceback_later()
        unchanged = before == project.source_hashes()
        passed = result.wasSuccessful() and result.testsRun == expected and not result.skipped and unchanged
        value = {'module': name, 'passed': bool(passed), 'status': 'PASSED' if passed else 'FAILED',
                 'tests_run': result.testsRun, 'expected_tests': expected,
                 'failures': len(result.failures), 'errors': len(result.errors),
                 'skipped': len(result.skipped), 'source_unchanged': unchanged,
                 'failure_details': [{'test': test.id(), 'traceback': detail} for test, detail in result.failures],
                 'error_details': [{'test': test.id(), 'traceback': detail} for test, detail in result.errors]}
    except Exception:
        detail = traceback.format_exc()
        print(detail, file=sys.stderr, flush=True)
        value = error_result(name, 'MODULE_ERROR', detail)
    write_result(output, name, value)
    return 0 if value['passed'] else 1


def offline_environment():
    environment = os.environ.copy()
    for key in ('NEBIUS_API_KEY', 'OPENAI_API_KEY'):
        environment.pop(key, None)
    environment.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    return environment


def run_one(name, output, timeout=300, runner=None):
    runner = Path(runner or __file__).resolve()
    command = [sys.executable, '-B', '-S', str(runner), '--module', name, '--output', str(output)]
    try:
        with (output / (name + '.log')).open('w', encoding='utf-8') as log:
            try:
                completed = subprocess.run(command, cwd=APP, env=offline_environment(),
                                           stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
            except subprocess.TimeoutExpired:
                detail = 'Module exceeded the %.3g-second process limit.' % timeout
                log.write('\n' + detail + '\n')
                value = error_result(name, 'TIMED_OUT', detail)
                write_result(output, name, value)
                return value
        report = output / (name + '.json')
        if not report.exists():
            value = error_result(name, 'MISSING_REPORT', 'Worker exited without writing its test report.')
        else:
            value = json.loads(report.read_text(encoding='utf-8'))
            if completed.returncode != 0:
                value['passed'] = False
                if not value.get('failures') and not value.get('errors'):
                    value['errors'] = 1
                value['status'] = 'FAILED'
        value['exit_code'] = completed.returncode
    except Exception:
        value = error_result(name, 'RUNNER_ERROR', traceback.format_exc())
    write_result(output, name, value)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--output', type=Path)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument('--module', choices=list(project.MODULES))
    choice.add_argument('--modules', nargs='+', choices=list(project.MODULES))
    args = parser.parse_args(argv)
    if not 1 <= args.jobs <= 8:
        parser.error('--jobs must be between 1 and 8')
    output = (args.output or Path(tempfile.mkdtemp(prefix='orkestryn-tests-'))).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.module:
        return run_module(args.module, output)
    before = project.source_hashes()
    started = time.monotonic()
    results = []
    names = list(dict.fromkeys(args.modules or project.MODULES))
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for future in concurrent.futures.as_completed([pool.submit(run_one, name, output) for name in names]):
            value = future.result()
            results.append(value)
            print(value['module'] + ': ' + value['status'], flush=True)
            if not value['passed']:
                print('--- ' + value['module'] + ' diagnostic log ---', flush=True)
                print((output / (value['module'] + '.log')).read_text(encoding='utf-8', errors='replace'), flush=True)
    value = {'passed': all(r['passed'] for r in results) and before == project.source_hashes(),
             'tests_run': sum(r['tests_run'] for r in results),
             'failures': sum(r['failures'] for r in results), 'errors': sum(r['errors'] for r in results),
             'skipped': sum(r['skipped'] for r in results), 'source_unchanged': before == project.source_hashes(),
             'model_calls': 0, 'elapsed_seconds': round(time.monotonic() - started, 3),
             'module_results': sorted(results, key=lambda r: r['module'])}
    write_result(output, 'summary', value)
    print(json.dumps({k: v for k, v in value.items() if k != 'module_results'}))
    print('Test records: ' + str(output))
    return 0 if value['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
