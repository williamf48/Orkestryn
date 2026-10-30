"""Legacy evidence publication and complete campaign regressions; zero model calls."""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import contextlib,errno,io,json,tempfile,threading,unittest
import challenge_factory as factory
import team_evaluation as evaluation

def denial(code):
    error=PermissionError(errno.EACCES,'Modelled Windows publication denial');error.winerror=code;return error

class PublicationTests(unittest.TestCase):
    def test_transient_windows_denials_replace_only_the_same_closed_complete_file(self):
        for code in [5,32,33]:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                target=Path(directory)/'summary.json';target.write_text('old');attempts=[];original=Path.replace
                def replace(pending,destination):
                    attempts.append(pending)
                    self.assertEqual(json.loads(pending.read_text()),{'complete':[1,2,3]})
                    with pending.open('a'):pass
                    if len(attempts)<3:raise denial(code)
                    return original(pending,destination)
                with mock.patch.object(Path,'replace',replace):factory.write_json(target,{'complete':[1,2,3]})
                self.assertEqual(len(set(attempts)),1)
                self.assertEqual(json.loads(target.read_text()),{'complete':[1,2,3]})
                self.assertEqual(list(Path(directory).glob('*.tmp')),[])

    def test_persistent_denial_is_bounded_and_keeps_previous_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'summary.json';target.write_bytes(b'previous complete');attempts=[];clock=iter([0,.1,2.1])
            def replace(pending,destination):attempts.append(pending);raise denial(5)
            with mock.patch.object(Path,'replace',replace),mock.patch.object(factory,'time',SimpleNamespace(monotonic=lambda:next(clock),sleep=lambda _:None)):
                with self.assertRaises(PermissionError):factory.write_json(target,{'new':True})
            self.assertEqual(target.read_bytes(),b'previous complete');self.assertEqual(len(attempts),2)
            self.assertEqual(len(set(attempts)),1);self.assertEqual(list(Path(directory).glob('*.tmp')),[])

    def test_unrelated_errors_never_retry_or_truncate_the_destination(self):
        for error in [PermissionError(errno.EACCES,'not a Windows sharing fact'),denial(111),OSError(errno.EIO,'disk failure')]:
            with self.subTest(error=type(error).__name__),tempfile.TemporaryDirectory() as directory:
                target=Path(directory)/'summary.json';target.write_bytes(b'previous complete')
                with mock.patch.object(Path,'replace',side_effect=error) as replace:
                    with self.assertRaises(OSError):factory.write_json(target,{'new':True})
                self.assertEqual(replace.call_count,1);self.assertEqual(target.read_bytes(),b'previous complete')
                self.assertEqual(list(Path(directory).glob('*.tmp')),[])

    def test_invalid_json_creates_no_pending_file_and_preserves_previous_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'summary.json';target.write_bytes(b'previous complete')
            with self.assertRaises(ValueError):factory.write_json(target,{'invalid':float('nan')})
            self.assertEqual(target.read_bytes(),b'previous complete');self.assertEqual(list(Path(directory).glob('*.tmp')),[])

    def test_concurrent_publishers_keep_independent_pending_files_across_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'summary.json';factory.write_json(target,{'version':'old'})
            barrier=threading.Barrier(2);lock=threading.Lock();pending_names=set();first=set();errors=[];original=Path.replace
            def replace(pending,destination):
                identity=threading.get_ident()
                with lock:
                    new=identity not in first;first.add(identity);pending_names.add(pending)
                if new:barrier.wait(5);raise denial(32)
                return original(pending,destination)
            def write(value):
                try:factory.write_json(target,{'version':value,'complete':list(range(25))})
                except BaseException as error:errors.append(error)
            with mock.patch.object(Path,'replace',replace):
                threads=[threading.Thread(target=write,args=(value,)) for value in ['a','b']]
                for thread in threads:thread.start()
                for thread in threads:thread.join(8)
            self.assertFalse(errors,errors);self.assertTrue(all(not t.is_alive() for t in threads))
            self.assertEqual(len(pending_names),2);self.assertIn(json.loads(target.read_text())['version'],['a','b'])
            self.assertEqual(json.loads(target.read_text())['complete'],list(range(25)))
            self.assertEqual(list(Path(directory).glob('*.tmp')),[])

    def test_second_case_publication_denial_does_not_repeat_roles_or_stop_campaign(self):
        with tempfile.TemporaryDirectory(prefix='factory campaign ') as directory:
            original=Path.replace;injected=[]
            def replace(pending,destination):
                target=Path(destination)
                if not injected and target.name=='summary.json' and target.parent.name=='dev-dependency-low':
                    value=json.loads(pending.read_text())
                    if value.get('status')=='FACTORY_REPLY_RECEIVED':injected.append(pending);raise denial(5)
                return original(pending,destination)
            with mock.patch.object(Path,'replace',replace),contextlib.redirect_stdout(io.StringIO()):path,summary=evaluation.run_campaign(Path(directory))
            self.assertEqual(len(injected),1)
            self.assertEqual((summary['task_completed'],summary['not_run'],summary['unknown_evidence'],summary['model_calls'],summary['role_requests']),(6,0,0,0,18))
            self.assertEqual(summary['status'],'COMPLETED')
            for case in summary['case_ids']:
                saved=json.loads((path/'cases'/case/'summary.json').read_text())
                self.assertEqual([c['role'] for c in saved['calls']],['factory','governance','recovery'])

    def test_persistent_case_denial_keeps_failure_visible_without_retrying_the_role(self):
        with tempfile.TemporaryDirectory(prefix='factory stopped ') as directory:
            original=Path.replace;pending_to_deny=[];clock=[0]
            def monotonic():clock[0]+=1;return clock[0]
            def replace(pending,destination):
                target=Path(destination)
                if not pending_to_deny and target.name=='summary.json' and target.parent.name=='dev-dependency-low':
                    value=json.loads(pending.read_text())
                    if value.get('status')=='FACTORY_REPLY_RECEIVED':pending_to_deny.append(pending)
                if pending_to_deny and pending==pending_to_deny[0]:raise denial(5)
                return original(pending,destination)
            with mock.patch.object(Path,'replace',replace),mock.patch.object(factory,'time',SimpleNamespace(monotonic=monotonic,sleep=lambda _:None)),contextlib.redirect_stdout(io.StringIO()) as output:
                path,summary=evaluation.run_campaign(Path(directory))
            self.assertEqual((summary['task_completed'],summary['unknown_evidence'],summary['not_run'],summary['model_calls']),(1,1,4,0))
            saved=json.loads((path/'cases/dev-dependency-low/summary.json').read_text())
            self.assertEqual(len(saved['calls']),1)
            self.assertIn('Offline case cause [dev-dependency-low]: PermissionError:',output.getvalue())
            self.assertEqual(summary['status'],'REVIEW_REQUIRED')


if __name__=='__main__':unittest.main()
