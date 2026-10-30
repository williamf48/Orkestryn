"""Native observations plus explicitly modelled Windows API boundary checks."""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import ctypes,json,os,platform,subprocess,sys,tempfile,time,unittest
import native_process as native
import restart_checker as restart


class _Kernel:
    """A model, not a native Windows result. No process can be terminated."""
    def __init__(self, processes=None, open_error=5):
        self.error=open_error;self.open_error=open_error;self.processes=list(processes or [])
        self.index=0;self.snapshot_error=None;self.first_error=None;self.next_error=18
        self.OpenProcess=mock.Mock(side_effect=self.open)
        self.GetProcessTimes=mock.Mock(side_effect=self.times)
        self.WaitForSingleObject=mock.Mock(return_value=258)
        self.GetExitCodeProcess=mock.Mock(side_effect=self.exit_code)
        self.CloseHandle=mock.Mock(return_value=True)
        self.CreateToolhelp32Snapshot=mock.Mock(side_effect=self.snapshot)
        self.Process32FirstW=mock.Mock(side_effect=self.first)
        self.Process32NextW=mock.Mock(side_effect=self.next)
    def open(self,access,inherit,pid):self.error=self.open_error;return 0 if self.open_error else 101
    def snapshot(self,flags,pid):
        if self.snapshot_error is not None:self.error=self.snapshot_error;return ctypes.c_void_p(-1).value
        self.index=0;return 202
    def first(self,handle,entry):
        if self.first_error is not None:self.error=self.first_error;return False
        if not self.processes:self.error=18;return False
        entry._obj.th32ProcessID=self.processes[0];return True
    def next(self,handle,entry):
        self.index+=1
        if self.index>=len(self.processes):self.error=self.next_error;return False
        entry._obj.th32ProcessID=self.processes[self.index];return True
    def times(self,handle,created,*other):created._obj.dwLowDateTime=17;created._obj.dwHighDateTime=1;return True
    def exit_code(self,handle,code):code._obj.value=259;return True


class ProcessTests(unittest.TestCase):
    def model(self,kernel):
        stack=__import__('contextlib').ExitStack()
        stack.enter_context(mock.patch.object(native,'platform',SimpleNamespace(system=lambda:'Windows')))
        stack.enter_context(mock.patch.object(native.ctypes,'WinDLL',return_value=kernel,create=True))
        stack.enter_context(mock.patch.object(native.ctypes,'get_last_error',side_effect=lambda:kernel.error,create=True))
        return stack
    def test_windows_structure_uses_pointer_sized_heap_and_fixed_width_fields(self):
        entry=native._ProcessEntry32W
        self.assertEqual(entry.th32ProcessID.offset,8)
        self.assertEqual(entry.szExeFile.offset,44 if ctypes.sizeof(ctypes.c_void_p)==8 else 36)
        self.assertEqual(ctypes.sizeof(entry),568 if ctypes.sizeof(ctypes.c_void_p)==8 else 556)
    def test_access_denied_needs_complete_snapshot_absence_before_exit(self):
        kernel=_Kernel([0,os.getpid(),424241])
        with self.model(kernel):facts=native.os_process(424242,{'creation':1})
        self.assertFalse(facts['exists']);self.assertFalse(facts['alive']);self.assertIsNone(facts['exit_code'])
        self.assertEqual(facts['method'],'WINDOWS_PROCESS_SNAPSHOT_ABSENT')
        kernel.OpenProcess.assert_called_once_with(0x101000,False,424242)
        kernel.CreateToolhelp32Snapshot.assert_called_once_with(2,0)
        kernel.CloseHandle.assert_called_once_with(202)
        kernel.GetProcessTimes.assert_not_called()
    def test_listed_pid_remains_unknown_and_preserves_open_error(self):
        kernel=_Kernel([os.getpid(),424242])
        with self.model(kernel),self.assertRaises(OSError) as caught:native.os_process(424242)
        self.assertEqual(caught.exception.winerror,5);self.assertEqual(caught.exception.observation_stage,'OpenProcess')
        self.assertIn('WinError 5',str(caught.exception));self.assertIn('unconfirmed',str(caught.exception))
        kernel.CloseHandle.assert_called_once_with(202)
    def test_snapshot_creation_failure_never_proves_exit(self):
        kernel=_Kernel();kernel.snapshot_error=5
        with self.model(kernel),self.assertRaises(OSError) as caught:native.os_process(424242)
        self.assertEqual(caught.exception.observation_stage,'CreateToolhelp32Snapshot')
        kernel.CloseHandle.assert_not_called()
    def test_empty_or_incomplete_process_snapshot_never_proves_exit(self):
        for entries in ([],[0,424241]):
            with self.subTest(entries=entries):
                kernel=_Kernel(entries)
                with self.model(kernel),self.assertRaises(OSError) as caught:native.os_process(424242)
                self.assertIn(caught.exception.observation_stage,{'Process32FirstW','ProcessSnapshotIncomplete'})
                kernel.CloseHandle.assert_called_once_with(202)
    def test_first_and_next_enumeration_errors_are_not_normal_end(self):
        for stage in ('first','next'):
            with self.subTest(stage=stage):
                kernel=_Kernel([os.getpid()])
                if stage=='first':kernel.first_error=6
                else:kernel.next_error=299
                with self.model(kernel),self.assertRaises(OSError) as caught:native.os_process(424242)
                self.assertEqual(caught.exception.winerror,6 if stage=='first' else 299)
                kernel.CloseHandle.assert_called_once_with(202)
    def test_process_snapshot_walk_is_bounded_and_closes_its_handle(self):
        kernel=_Kernel([os.getpid()]*4)
        with self.model(kernel),mock.patch.object(native,'_MAX_SNAPSHOT_PROCESSES',3),self.assertRaises(OSError) as caught:native.os_process(424242)
        self.assertEqual(caught.exception.observation_stage,'ProcessSnapshotLimit')
        self.assertEqual(kernel.Process32NextW.call_count,3);kernel.CloseHandle.assert_called_once_with(202)
    def test_invalid_parameter_means_absent_without_snapshot_or_invented_code(self):
        kernel=_Kernel(open_error=87)
        with self.model(kernel):facts=native.os_process(424242)
        self.assertFalse(facts['exists']);self.assertIsNone(facts['exit_code'])
        kernel.CreateToolhelp32Snapshot.assert_not_called();kernel.CloseHandle.assert_not_called()
    def test_unrelated_open_errors_are_reported_without_snapshot(self):
        for code in (0,6,8,299):
            with self.subTest(code=code):
                kernel=_Kernel(open_error=code)
                if code==0:kernel.OpenProcess.side_effect=lambda *args:0
                with self.model(kernel),self.assertRaises(OSError) as caught:native.os_process(424242)
                self.assertEqual(caught.exception.winerror,code);self.assertEqual(caught.exception.observation_stage,'OpenProcess')
                kernel.CreateToolhelp32Snapshot.assert_not_called();kernel.CloseHandle.assert_not_called()
    def test_open_handle_retains_identity_and_wait_signal_decides_liveness(self):
        for signal,alive in ((258,True),(0,False)):
            with self.subTest(signal=signal):
                kernel=_Kernel(open_error=0);kernel.WaitForSingleObject.return_value=signal
                with self.model(kernel):facts=native.os_process(424242)
                self.assertEqual(facts['alive'],alive)
                self.assertEqual(facts['identity'],{'platform':'Windows','created_filetime':(1<<32)|17})
                self.assertEqual(facts['exit_code'],None if alive else 259)
                kernel.CloseHandle.assert_called_once_with(101);kernel.CreateToolhelp32Snapshot.assert_not_called()
    def test_open_handle_query_and_wait_errors_close_handle_and_keep_diagnostics(self):
        for stage in ('GetProcessTimes','WaitForSingleObject','GetExitCodeProcess'):
            with self.subTest(stage=stage):
                kernel=_Kernel(open_error=0)
                def fail(*args):kernel.error=6;return 0xffffffff if stage=='WaitForSingleObject' else False
                getattr(kernel,stage).side_effect=fail
                with self.model(kernel),self.assertRaises(OSError) as caught:native.os_process(424242)
                self.assertEqual(caught.exception.winerror,6);self.assertEqual(caught.exception.observation_stage,stage)
                kernel.CloseHandle.assert_called_once_with(101)
    def test_wait_reobserves_same_denied_lifetime_until_snapshot_confirms_absence(self):
        kernel=_Kernel([os.getpid(),424242]);identity={'platform':'Windows','created_filetime':17}
        original=kernel.snapshot
        def snapshot(*args):
            if kernel.OpenProcess.call_count==2:kernel.processes=[os.getpid()]
            return original(*args)
        kernel.CreateToolhelp32Snapshot.side_effect=snapshot
        clock=SimpleNamespace(monotonic=mock.Mock(side_effect=[0,.01]),sleep=mock.Mock())
        with self.model(kernel),mock.patch.object(native,'time',clock):
            self.assertTrue(native.wait_for_exit(424242,identity,1))
        self.assertEqual(kernel.OpenProcess.call_args_list,[mock.call(0x101000,False,424242)]*2)
        self.assertEqual(kernel.CloseHandle.call_args_list,[mock.call(202)]*2);clock.sleep.assert_called_once()
    def test_persistent_denial_at_deadline_raises_and_never_claims_exit(self):
        kernel=_Kernel([os.getpid(),424242]);clock=SimpleNamespace(monotonic=mock.Mock(side_effect=[0,.01,1.0]),sleep=mock.Mock())
        with self.model(kernel),mock.patch.object(native,'time',clock),self.assertRaises(OSError) as caught:
            native.wait_for_exit(424242,{'creation':1},1)
        self.assertEqual(caught.exception.winerror,5);self.assertEqual(kernel.OpenProcess.call_count,2)
        clock.sleep.assert_called_once_with(.025)
    def test_wait_does_not_retry_snapshot_failures_or_unqualified_denials(self):
        for stage,code in (('CreateToolhelp32Snapshot',5),('GetProcessTimes',5),('OpenProcess',6),(None,5)):
            with self.subTest(stage=stage,code=code):
                error=OSError('MODELLED unknown observation');error.winerror=code
                if stage:error.observation_stage=stage
                observer=mock.Mock(side_effect=error);clock=SimpleNamespace(monotonic=lambda:0,sleep=mock.Mock())
                with mock.patch.object(native,'platform',SimpleNamespace(system=lambda:'Windows')),mock.patch.object(native,'time',clock),self.assertRaises(OSError) as caught:
                    native.wait_for_exit(424242,{'creation':1},1,observer=observer)
                self.assertIs(caught.exception,error);observer.assert_called_once_with(424242,{'creation':1});clock.sleep.assert_not_called()
    def test_windows_pid_range_is_checked_before_calling_api(self):
        kernel=_Kernel()
        with self.model(kernel):
            for pid in (True,0,-1,1<<32):
                with self.subTest(pid=pid),self.assertRaises(ValueError):native.os_process(pid)
        kernel.OpenProcess.assert_not_called()
    def test_native_current_process_is_observable_on_target_platform(self):
        facts=native.os_process(os.getpid());self.assertTrue(facts['exists']);self.assertTrue(facts['alive'])
        self.assertEqual(facts['identity']['platform'],platform.system())
        if platform.system()=='Windows':
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            self.assertTrue(native._windows_process_present(kernel,os.getpid()))
    def _owned_child_lifetime(self,root):
        # Runs on the actual host; only a Windows invocation tests native Win32.
        output=root/'owned.log';ready=root/'ready.json'
        code=('import json,os,sys;from pathlib import Path;import native_process as n;'
              'value={**n.os_process(os.getpid()),"pid":os.getpid(),'
              '"clean_environment":not any(k in os.environ for k in ("__PYVENV_LAUNCHER__","NEBIUS_API_KEY","OPENAI_API_KEY"))};'
              'Path(sys.argv[1]).write_text(json.dumps(value));sys.stdin.buffer.read(1)')
        # Use the same stdlib-only worker setup as the application. On Windows,
        # sys.executable can name a venv redirector with a different process PID.
        with output.open('wb') as log:
            process=subprocess.Popen([restart.worker_interpreter(),'-B','-S','-c',code,str(ready)],
                cwd=Path(native.__file__).parent,env=restart.child_env(),stdin=subprocess.PIPE,stdout=log,stderr=log)
        try:
            deadline=time.monotonic()+5;registration=None
            while time.monotonic()<deadline:
                try:registration=json.loads(ready.read_text());break
                except (FileNotFoundError,json.JSONDecodeError):
                    if process.poll() is not None:self.fail(output.read_text())
                    time.sleep(.025)
            self.assertIsNotNone(registration,'Owned child did not publish its native lifetime.')
            self.assertEqual(registration['pid'],process.pid,'Diagnostic launcher PID differs from the actual worker PID.')
            self.assertTrue(registration['clean_environment'],'Worker inherited a launcher marker or provider credential.')
            identity=registration['identity'];facts=native.os_process(process.pid,identity)
            self.assertEqual(facts['identity'],identity,'Observed creation identity differs from the registered worker lifetime.')
            self.assertTrue(facts['alive'],'Registered worker exited before its explicit stdin release.')
            self.assertFalse(native.wait_for_exit(process.pid,identity,0))
            process.communicate(b'x',timeout=5);self.assertEqual(process.returncode,0,output.read_text())
            self.assertTrue(native.wait_for_exit(process.pid,identity,5))
            self.assertTrue(native.wait_for_exit(process.pid,identity,0))
            other=output.with_suffix('.released');output.rename(other);other.rename(output)
        finally:
            if process.stdin and not process.stdin.closed:process.stdin.close()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    def test_native_owned_child_exit_and_file_release_with_same_identity(self):
        with tempfile.TemporaryDirectory(prefix='native process lifetime ') as temp:self._owned_child_lifetime(Path(temp))
    def test_child_probe_bypasses_windows_venv_redirector_without_losing_identity(self):
        # Model Windows interpreter selection but observe a real child on this
        # host. This is not native Windows proof when run on Linux.
        actual=getattr(sys,'_base_executable',None) or sys.executable
        with tempfile.TemporaryDirectory(prefix='native launcher with spaces ') as temp:
            root=Path(temp);launcher=root/'virtual environment'/'Scripts'/'python.exe';launcher.parent.mkdir(parents=True)
            launcher.write_text('MODELLED redirector; deliberately not an executable interpreter.')
            with mock.patch.object(restart,'platform',SimpleNamespace(system=lambda:'Windows')), \
                    mock.patch.object(sys,'executable',str(launcher)), \
                    mock.patch.object(sys,'_base_executable',actual,create=True), \
                    mock.patch.dict(os.environ,{'__PYVENV_LAUNCHER__':'fixture'}):
                self._owned_child_lifetime(root)


if __name__=='__main__':unittest.main()
