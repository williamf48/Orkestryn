"""Regression checks for the observed Windows handle, path and probe failures."""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import contextlib,errno,io,json,sqlite3,tempfile,threading,time,unittest
import native_process as native
import durable_runtime as d
import runtime_sessions as sessions
import acceptance
import dashboard
import test_paths
import team_workspace as ws
class CompatibilityTests(unittest.TestCase):
    def test_transient_windows_readiness_denials_retry_the_same_guarded_read(self):
        for code in (None,5,32,33):
            with self.subTest(winerror=code),tempfile.TemporaryDirectory(prefix='ready read ') as temp:
                root=Path(temp);path=root/'ready.json';ws.write_json(path,{'identity':'unchanged'});raw=path.read_bytes()
                original=Path.read_bytes;attempts=[]
                def read(p):
                    attempts.append(p);self.assertEqual(p,path)
                    if len(attempts)<3:
                        error=PermissionError(errno.EACCES,'MODELLED Windows readiness denial',str(p))
                        if code is not None:error.winerror=code
                        raise error
                    return original(p)
                with mock.patch.object(d,'platform',SimpleNamespace(system=lambda:'Windows')),mock.patch.object(Path,'read_bytes',read):
                    self.assertEqual(d.read(root,'ready.json'),{'identity':'unchanged'})
                self.assertEqual(attempts,[path]*3);self.assertEqual(path.read_bytes(),raw)
                self.assertEqual(list(root.iterdir()),[path])
    def test_persistent_windows_readiness_denial_is_bounded_and_propagates(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'ready.json';ws.write_json(path,{'identity':'original'});raw=path.read_bytes()
            error=PermissionError(errno.EACCES,'MODELLED persistent readiness denial',str(path))
            clock=SimpleNamespace(monotonic=mock.Mock(side_effect=[0,.25,2.1]),sleep=mock.Mock())
            with mock.patch.object(d,'platform',SimpleNamespace(system=lambda:'Windows')),mock.patch.object(d,'time',clock),mock.patch.object(Path,'read_bytes',side_effect=error) as read:
                with self.assertRaises(PermissionError) as caught:d.read(root,'ready.json')
            self.assertIs(caught.exception,error);self.assertEqual(read.call_count,2);clock.sleep.assert_called_once()
            self.assertEqual(path.read_bytes(),raw)
    def test_readiness_retry_does_not_hide_other_files_platforms_or_errors(self):
        unrelated=PermissionError(errno.EACCES,'MODELLED unrelated Windows error');unrelated.winerror=1314
        cases=[('Linux','ready.json',PermissionError(errno.EACCES,'Linux access denial')),
               ('Windows','config.json',PermissionError(errno.EACCES,'Config access denial')),
               ('Windows','ready.json',unrelated),('Windows','ready.json',OSError(errno.EIO,'Unrelated I/O error'))]
        for system,name,error in cases:
            with self.subTest(system=system,name=name,error=str(error)),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);ws.write_json(root/name,{'original':True});clock=SimpleNamespace(monotonic=lambda:0,sleep=mock.Mock())
                with mock.patch.object(d,'platform',SimpleNamespace(system=lambda:system)),mock.patch.object(d,'time',clock),mock.patch.object(Path,'read_bytes',side_effect=error) as read:
                    with self.assertRaises(type(error)) as caught:d.read(root,name)
                self.assertIs(caught.exception,error);read.assert_called_once();clock.sleep.assert_not_called()
    def test_invalid_readiness_json_is_not_retried_or_treated_as_healthy(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'ready.json').write_text('not-json');clock=SimpleNamespace(monotonic=lambda:0,sleep=mock.Mock())
            with mock.patch.object(d,'platform',SimpleNamespace(system=lambda:'Windows')),mock.patch.object(d,'time',clock):
                with self.assertRaises(ValueError):d.read(root,'ready.json')
            clock.sleep.assert_not_called();self.assertEqual((root/'ready.json').read_text(),'not-json')
    def test_readiness_retry_rechecks_path_guard_before_opening(self):
        with tempfile.TemporaryDirectory() as temp,contextlib.ExitStack() as stack:
            root=Path(temp);path=root/'ready.json';private=root/'private.json'
            ws.write_json(path,{'identity':'original'});private.write_text('PRIVATE_READY_TARGET');attempts=[]
            def deny_and_relink(p):
                attempts.append(p);self.assertEqual(p,path);self.assertEqual(len(attempts),1)
                path.unlink();stack.enter_context(test_paths.linked_path(path,private))
                raise PermissionError(errno.EACCES,'MODELLED readiness denial before path changed')
            with mock.patch.object(d,'platform',SimpleNamespace(system=lambda:'Windows')),mock.patch.object(Path,'read_bytes',deny_and_relink):
                with self.assertRaisesRegex(ValueError,'missing or linked'):d.read(root,'ready.json')
            self.assertEqual(attempts,[path]);self.assertEqual(private.read_text(),'PRIVATE_READY_TARGET')
    def test_transient_windows_replace_denials_retry_only_the_same_complete_file(self):
        for code in (5,32,33):
            with self.subTest(winerror=code),tempfile.TemporaryDirectory() as temp:
                path=Path(temp)/'summary.json';path.write_text('{"version":"old"}')
                original=Path.replace;attempts=[]
                def replace(pending,target):
                    attempts.append(pending)
                    self.assertEqual(json.loads(pending.read_text()),{'version':'new'})
                    self.assertEqual(json.loads(path.read_text()),{'version':'old'})
                    if len(attempts)<=2:
                        error=PermissionError('MODELLED Windows replacement denial');error.winerror=code;raise error
                    return original(pending,target)
                with mock.patch.object(Path,'replace',replace):ws.write_json(path,{'version':'new'})
                self.assertEqual(len(attempts),3);self.assertEqual(len(set(attempts)),1)
                self.assertEqual(json.loads(path.read_text()),{'version':'new'});self.assertEqual(list(Path(temp).iterdir()),[path])
    def test_persistent_windows_replace_denial_propagates_and_preserves_previous_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'summary.json';old='{"version":"old"}';path.write_text(old)
            error=PermissionError('MODELLED persistent Windows denial');error.winerror=5
            clock=SimpleNamespace(monotonic=mock.Mock(side_effect=[0,.25,2.1]),sleep=mock.Mock())
            with mock.patch.object(ws,'time',clock),mock.patch.object(Path,'replace',side_effect=error) as replace:
                with self.assertRaises(PermissionError) as caught:ws.write_json(path,{'version':'new'})
            self.assertIs(caught.exception,error);self.assertEqual(replace.call_count,2);clock.sleep.assert_called_once()
            self.assertEqual(path.read_text(),old);self.assertEqual(list(Path(temp).iterdir()),[path])
    def test_unrelated_replace_error_is_immediate_and_preserves_previous_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'summary.json';old='{"version":"old"}';path.write_text(old)
            error=PermissionError('Non-Windows permission failure')
            with mock.patch.object(Path,'replace',side_effect=error) as replace,mock.patch.object(ws,'time',SimpleNamespace(monotonic=lambda:0,sleep=mock.Mock())) as clock:
                with self.assertRaises(PermissionError):ws.write_json(path,{'version':'new'})
                replace.assert_called_once();clock.sleep.assert_not_called()
            self.assertEqual(path.read_text(),old);self.assertEqual(list(Path(temp).iterdir()),[path])
    def _check_concurrent_publishers(self,transient_winerror=None):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'summary.json';ws.write_json(path,{'version':'old'})
            barrier=threading.Barrier(2);original=Path.replace;lock=threading.Lock()
            attempts={};completed=[];injected=[];errors=[]
            def replace(pending,target):
                writer=threading.current_thread().name
                with lock:
                    first=writer not in attempts;attempts.setdefault(writer,[]).append(pending)
                self.assertEqual(json.loads(pending.read_text(encoding='utf-8')),{'version':writer,'payload':writer*10000})
                # Coordinate the first attempt per writer. A native retry is
                # still the same publication: its peer may already be done.
                if first:barrier.wait(timeout=5)
                if first and writer=='A' and transient_winerror is not None:
                    with lock:injected.append(transient_winerror)
                    exc=PermissionError('MODELLED transient Windows concurrent replacement denial');exc.winerror=transient_winerror;raise exc
                result=original(pending,target)
                with lock:completed.append(writer)
                return result
            def publish(value):
                try:ws.write_json(path,{'version':value,'payload':value*10000})
                except BaseException as exc:
                    with lock:errors.append(exc)
            with mock.patch.object(Path,'replace',replace):
                threads=[threading.Thread(target=publish,args=(v,),name=v) for v in ('A','B')]
                for thread in threads:thread.start()
                for thread in threads:thread.join(8)
            self.assertTrue(all(not t.is_alive() for t in threads));self.assertEqual(errors,[])
            self.assertEqual(set(attempts),{'A','B'});self.assertCountEqual(completed,['A','B'])
            self.assertEqual(len({paths[0] for paths in attempts.values()}),2)
            for paths in attempts.values():self.assertEqual(len(set(paths)),1,'A retry must reuse its original complete pending file.')
            if transient_winerror is not None:
                self.assertEqual(injected,[transient_winerror]);self.assertGreaterEqual(len(attempts['A']),2)
            final=json.loads(path.read_text());self.assertIn(final['version'],('A','B'));self.assertEqual(final['payload'],final['version']*10000)
            self.assertEqual(list(Path(temp).iterdir()),[path])
    def test_concurrent_publishers_use_independent_pending_files(self):
        self._check_concurrent_publishers()
    def test_concurrent_publisher_retry_does_not_repeat_peer_rendezvous(self):
        for code in (5,32,33):
            with self.subTest(winerror=code):self._check_concurrent_publishers(code)
    def test_native_open_reader_and_publication_keep_both_versions_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'summary.json';ws.write_json(path,{'version':'old'});errors=[]
            def publish():
                try:ws.write_json(path,{'version':'new'})
                except BaseException as exc:errors.append(exc)
            with path.open('rb') as reader:
                thread=threading.Thread(target=publish);thread.start();time.sleep(.1)
                self.assertEqual(json.loads(reader.read()),{'version':'old'})
            thread.join(5);self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
            self.assertEqual(json.loads(path.read_text()),{'version':'new'});self.assertEqual(list(Path(temp).iterdir()),[path])
    def test_invalid_json_leaves_previous_evidence_and_creates_no_pending_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'summary.json';old='{"version":"old"}';path.write_text(old)
            with self.assertRaises(ValueError):ws.write_json(path,{'invalid':float('nan')})
            self.assertEqual(path.read_text(),old);self.assertEqual(list(Path(temp).iterdir()),[path])
    def test_stop_marker_without_native_supervisor_exit_does_not_complete_teardown(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp);ws.write_json(path/'runtime.json',{'runtime_id':'r'});ws.write_json(path/'teardown.json',{'all_workers_stopped':True,'workers':[{'stopped':True}]})
            r=d.Runtime(path)
            with mock.patch.object(d.Runtime,'access',new_callable=mock.PropertyMock,return_value={'pid':123,'os_identity':{'creation':1}}),mock.patch.object(native,'wait_for_exit',return_value=False),mock.patch.object(d.local,'http_json') as network:
                with self.assertRaisesRegex(OSError,'supervisor exit'):r.close()
                network.assert_not_called()
    def test_lost_graceful_teardown_request_reuses_only_the_same_owned_capability(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp);ws.write_json(path/'runtime.json',{'runtime_id':'r'});r=d.Runtime(path)
            access={'pid':123,'os_identity':{'creation':1},'port':1,'token':'PRIVATE'};calls=[]
            def send(port,endpoint,body,token,timeout):
                calls.append((port,endpoint,body,token))
                if len(calls)==1:raise ConnectionResetError('Lost transport')
                ws.write_json(path/'teardown.json',{'all_workers_stopped':True,'workers':[{'stopped':True}]});return {'status':'TEARDOWN_REQUESTED'}
            with mock.patch.object(d.Runtime,'access',new_callable=mock.PropertyMock,return_value=access),mock.patch.object(d,'os_process',return_value={'exists':True,'alive':True,'identity':access['os_identity']}),mock.patch.object(native,'wait_for_exit',return_value=True),mock.patch.object(d.local,'http_json',side_effect=send):
                self.assertEqual(r.close(),[{'stopped':True}]);self.assertEqual(len(calls),2);self.assertEqual(calls[0],calls[1])
    def test_native_shutdown_returns_only_after_owned_supervisor_releases_files(self):
        with tempfile.TemporaryDirectory(prefix='native exit regression ') as temp:
            r,process=d.launch(Path(temp)/'runtime',ttl=60)
            try:
                access=r.access;result=r.close();self.assertTrue(all(x['stopped'] for x in result))
                # Native exit can precede the background Popen reaper publishing
                # returncode. Join the same owned handle; an immediate poll may
                # return None while the reaper holds its nonblocking wait lock.
                process.wait(timeout=5);self.assertIsNotNone(process.returncode)
                facts=d.os_process(access['pid'],access['os_identity']);self.assertTrue(not facts['exists'] or facts['identity']!=access['os_identity'] or facts['alive'] is False)
                for name in ('runtime.sqlite3','supervisor.log'):
                    p=r.directory/name;other=p.with_suffix(p.suffix+'.closed');p.rename(other);other.rename(p)
            finally:r.close()
    def test_permission_failure_is_not_misreported_as_native_exit(self):
        with self.assertRaises(PermissionError):native.wait_for_exit(123,{'creation':1},0,observer=mock.Mock(side_effect=PermissionError('unknown')))
    def test_linux_process_exit_during_stat_read_is_confirmed_absent(self):
        identity={'platform':'Linux','boot_id':'fixture','proc_pid':424242,'start_ticks':1}
        missing=ProcessLookupError(errno.ESRCH,'MODELLED process exited during procfs read')
        with mock.patch.object(native.platform,'system',return_value='Linux'),mock.patch.object(Path,'read_text',side_effect=missing) as read:
            facts=native.os_process(123,identity)
            self.assertEqual(facts,{'exists':False,'alive':False,'identity':None,'exit_code':None,'method':'LINUX_PROCESS_ABSENT'})
            self.assertTrue(native.wait_for_exit(123,identity,0))
            self.assertEqual(read.call_count,2)
    def test_linux_permission_and_unrelated_procfs_errors_are_not_native_exit(self):
        identity={'platform':'Linux','boot_id':'fixture','proc_pid':424242,'start_ticks':1}
        for failure in (PermissionError(errno.EACCES,'MODELLED procfs permission denial'),OSError(errno.EIO,'MODELLED unrelated procfs I/O failure')):
            with self.subTest(error=type(failure).__name__),mock.patch.object(native.platform,'system',return_value='Linux'),mock.patch.object(Path,'read_text',side_effect=failure):
                with self.assertRaises(type(failure)) as caught:native.wait_for_exit(123,identity,0)
                self.assertIs(caught.exception,failure)
    def test_native_wait_confirms_original_lifetime_ended_after_pid_reuse(self):
        observer=mock.Mock(return_value={'exists':True,'alive':True,'identity':{'creation':2}})
        self.assertTrue(native.wait_for_exit(123,{'creation':1},0,observer=observer));observer.assert_called_once_with(123,{'creation':1})
    def test_native_wait_never_calls_a_live_process_stopped_at_timeout(self):
        observer=mock.Mock(return_value={'exists':True,'alive':True,'identity':{'creation':1}})
        self.assertFalse(native.wait_for_exit(123,{'creation':1},0,observer=observer))
    def test_dead_worker_probes_do_not_attempt_slow_refused_tcp_connections(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp);ws.write_json(path/'config.json',{'token':'PRIVATE'});ws.write_json(path/'ready.json',{'pid':123,'os_identity':{'creation':1},'port':1})
            server=SimpleNamespace(worker_dir=lambda _:path)
            with mock.patch.object(d,'os_process',return_value={'exists':False,'identity':None,'alive':False}),mock.patch.object(d.local,'http_json',side_effect=AssertionError('Do not contact dead endpoints')) as network:
                result=d.RuntimeServer.infer(server,'candidate-v2');self.assertFalse(result['ok']);self.assertEqual(result['error'],'ORIGINAL_WORKER_EXITED');network.assert_not_called()
    def test_unknown_process_observation_does_not_attempt_or_claim_healthy_inference(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp);ws.write_json(path/'config.json',{'token':'PRIVATE'});ws.write_json(path/'ready.json',{'pid':123,'os_identity':{'creation':1},'port':1})
            with mock.patch.object(d,'os_process',side_effect=PermissionError('unknown')),mock.patch.object(d.local,'http_json') as network:
                result=d.RuntimeServer.infer(SimpleNamespace(worker_dir=lambda _:path),'candidate-v2');self.assertFalse(result['ok']);network.assert_not_called()
    def test_startup_exception_includes_bounded_worker_cause_but_not_capabilities(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp);(path/'supervisor.log').write_text('x'*10000+'ACTUAL_SUPERVISOR_CAUSE');(path/'workers'/'id').mkdir(parents=True);(path/'workers'/'id'/'worker.log').write_text('ACTUAL_WORKER_CAUSE');(path/'.access.json').write_text('DO_NOT_EXPORT_CAPABILITY')
            message=str(d.startup_error('Startup failed',path,17));self.assertIn('ACTUAL_SUPERVISOR_CAUSE',message);self.assertIn('ACTUAL_WORKER_CAUSE',message);self.assertNotIn('DO_NOT_EXPORT_CAPABILITY',message);self.assertLess(len(message),11000)
    def test_windows_privilege_fallback_still_rejects_linked_output_before_launch(self):
        with tempfile.TemporaryDirectory() as temp,tempfile.TemporaryDirectory() as outside:
            path=Path(temp);failure=OSError('Symlink privilege unavailable');failure.winerror=1314
            with mock.patch.object(Path,'symlink_to',side_effect=failure),test_paths.linked_path(path/sessions.FOLDER,Path(outside),directory=True):
                with self.assertRaises(dashboard.EvidenceError):dashboard.EvidenceStore(path).directory('runtime_session','sample')
            self.assertEqual(list(Path(outside).iterdir()),[])
    def test_windows_file_link_fallback_excludes_private_target_from_export(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp);target=path/'private.txt';target.write_text('SECRET');failure=OSError('No symlink privilege');failure.winerror=1314
            with mock.patch.object(Path,'symlink_to',side_effect=failure),test_paths.linked_path(path/'summary.json',target):
                self.assertEqual(acceptance.evidence_export(path/'public.zip',[path])['records'],0)
    def test_unrelated_link_errors_are_not_hidden_by_privilege_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp);failure=OSError('Filesystem unavailable');failure.winerror=5
            with mock.patch.object(Path,'symlink_to',side_effect=failure):
                with self.assertRaises(OSError):
                    with test_paths.linked_path(path/'link',path/'outside'):pass
if __name__=='__main__':unittest.main()
