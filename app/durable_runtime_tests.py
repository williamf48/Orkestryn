"""Real supervisor interruption and native process lifetime regression tests."""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import ctypes
import errno
import uuid
import http.client
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest

import durable_runtime as d
import local_service as local
import simulator as sim
import team_workspace as ws


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='durable runtime tests ')
        self.root=Path(self.temp.name);self.runtimes=[]
    def tearDown(self):
        for runtime in reversed(self.runtimes):
            self.close_runtime(runtime)
        self.temp.cleanup()
    def close_runtime(self,runtime):
        try:
            d.launch(runtime.directory,restart_existing=True)
            cleanup=runtime.close()
            self.assertTrue(bool(cleanup) and all(v['stopped'] for v in cleanup),cleanup)
        except ValueError as exc:
            self.assertTrue((runtime.directory/'teardown.json').exists(),str(exc))
            self.assertTrue(d.read(runtime.directory,'teardown.json')['all_workers_stopped'])
            access=runtime.access
            self.assertTrue(d.native_process.wait_for_exit(access['pid'],access['os_identity'],observer=d.os_process))
    def launch(self,**kwargs):
        runtime,p=d.launch(self.root/uuid.uuid4().hex,ttl=90,**kwargs)
        self.runtimes.append(runtime);return runtime,p
    def op(self,r,tool,key='test-op',**kwargs):
        state=r.state()
        return {'runtime_id':r.identity,'operation_id':key,'tool':tool,'expected_revision':state['revision'],
                'percent':kwargs.get('percent',0),'owner':kwargs.get('owner','deploy-A')}
    def receipt_context(self,r,request,state,stage):
        return json.dumps({'stage':stage,'tool':request['tool'],'operation_id':request['operation_id'],
            'expected_revision':request['expected_revision'],'actual_revision':state['revision'],
            'receipt':state['receipts'].get(request['operation_id']),
            'pending_receipts':{key:value for key,value in state['receipts'].items() if value.get('status')=='PENDING'},
            'lease_seconds_remaining':round(d.read(r.directory,'runtime.json')['expires_unix']-time.time(),3)},sort_keys=True)
    def confirm_setup(self,r,request):
        # A pending setup is not permission to construct the next effect from
        # its old revision. Reconcile only this exact local operation/key.
        deadline=time.monotonic()+15
        while True:
            result=r.operate(request)
            if result.get('status')!='PENDING' or time.monotonic()>=deadline:break
            time.sleep(.025)
        state=r.state();detail=self.receipt_context(r,request,state,'candidate setup')
        self.assertEqual(result.get('status'),'SUCCEEDED',detail)
        self.assertEqual(state['receipts'].get(request['operation_id'],{}).get('status'),'SUCCEEDED',detail)
        self.assertEqual(state['revision'],request['expected_revision']+1,detail)
        self.assertTrue(state['workers']['candidate-v2']['healthy'],detail)
        return result
    def test_actual_supervisor_death_preserves_workers_and_resumes_same_runtime(self):
        r,p=self.launch();self.assertEqual(r.operate(self.op(r,'start_candidate'))['status'],'SUCCEEDED')
        before=r.state();p.kill();p.wait(5)
        for name in before['workers']:
            db=sqlite3.connect(r.directory/'runtime.sqlite3')
            try:row=db.execute('SELECT identity FROM workers WHERE target=?',(name,)).fetchone()
            finally:db.close()
            facts=d.observe(r.directory/'workers'/row[0]);self.assertTrue(facts['healthy'])
            self.assertEqual(facts,before['workers'][name])
        with self.assertRaises((OSError,http.client.HTTPException)):r.state()
        new,process=d.launch(r.directory,restart_existing=True)
        self.assertNotEqual(process.pid,p.pid);self.assertEqual(new.state()['workers'],before['workers'])
        self.assertEqual(new.identity,r.identity);self.assertEqual(new.state()['revision'],before['revision'])
    def test_real_crashes_reconcile_intent_effect_and_committed_receipts_for_each_effect(self):
        for tool in ('start_candidate','set_route','stop_candidate'):
            for phase in ('intent','effect','committed'):
                with self.subTest(tool=tool,phase=phase):
                    r,p=self.launch(crash_at=phase,crash_tool=tool)
                    owned=r
                    try:
                        if tool!='start_candidate':self.confirm_setup(r,self.op(r,'start_candidate','setup'))
                        before=r.state();req=self.op(r,tool,percent=20 if tool=='set_route' else 0)
                        with self.assertRaises((OSError,http.client.HTTPException)):r.operate(req)
                        p.wait(5);self.assertEqual(p.returncode,73)
                        r,_=d.launch(r.directory,restart_existing=True)
                        after=r.state();detail=self.receipt_context(r,req,after,'after '+phase+' crash')
                        self.assertEqual(after['receipts']['test-op']['status'],'SUCCEEDED',detail)
                        self.assertEqual(after['revision'],before['revision']+1,detail)
                        self.assertEqual(after['workers']['stable-v1'],before['workers']['stable-v1'])
                        self.assertEqual(len(list((r.directory/'workers').iterdir())),2)
                        replay=r.operate(req);self.assertTrue(replay['replayed'])
                        self.assertEqual(r.state()['revision'],after['revision'])
                        changed={**req,'owner':'different-owner'};self.assertEqual(r.operate(changed)['code'],'RUNTIME_KEY_CONFLICT')
                        if tool=='stop_candidate':
                            self.assertTrue(after['workers']['candidate-v2']['exit_confirmed'])
                            self.assertIsNone(after['workers']['candidate-v2']['exit_code']) if os.name!='nt' else None
                        if tool=='set_route':self.assertEqual(after['candidate_percent'],20)
                    finally:
                        # Each crash is an independent fixture. Confirm native
                        # shutdown before creating the next leased runtime.
                        self.close_runtime(owned)
                        self.runtimes.remove(owned)
    def test_pending_setup_reconciles_exact_receipt_before_next_operation(self):
        r,p=self.launch();p.kill();p.wait(5)
        interrupted=[]
        def checkpoint(phase,request):
            if phase=='effect' and request['operation_id']=='setup' and not interrupted:
                interrupted.append(True)
                raise OSError('Modelled interruption after worker start and before receipt commit')
        host=d.RuntimeServer(r.directory,checkpoint)
        try:
            client=SimpleNamespace(identity=r.identity,directory=r.directory,state=host.snapshot,operate=host.operate)
            setup=self.op(client,'start_candidate','setup')
            pending=host.operate(setup);self.assertEqual(pending['status'],'PENDING')
            before=host.snapshot();self.assertEqual(before['revision'],0)
            self.assertTrue(before['workers']['candidate-v2']['healthy'])
            self.confirm_setup(client,setup)
            confirmed=host.snapshot();self.assertEqual(confirmed['workers'],before['workers'])
            stop=self.op(client,'stop_candidate');self.assertEqual(stop['expected_revision'],1)
            self.assertEqual(host.operate(stop)['status'],'SUCCEEDED')
            after=host.snapshot();self.assertEqual(after['revision'],2)
            self.assertTrue(after['workers']['candidate-v2']['exit_confirmed'])
            self.assertEqual(len(list((r.directory/'workers').iterdir())),2)
            self.assertTrue(host.operate(setup)['replayed'])
            self.assertEqual(host.snapshot()['revision'],2)
        finally:host.close()
    def test_blocked_setup_stops_the_fixture_with_reason_and_revision(self):
        r,p=self.launch();request=self.op(r,'start_candidate','setup');request['expected_revision']=99
        with self.assertRaisesRegex(AssertionError,'RUNTIME_VERSION_CONFLICT'):
            self.confirm_setup(r,request)
        after=r.state();self.assertEqual(after['revision'],0)
        self.assertNotIn('candidate-v2',after['workers'])
        self.assertEqual(set(after['receipts']),{'setup'})
        self.assertEqual(after['receipts']['setup']['status'],'BLOCKED')
    def test_competing_supervisor_never_replaces_active_workers(self):
        r,p=self.launch();before=r.state()
        again,child=d.launch(r.directory,restart_existing=True)
        self.assertIsNone(child);self.assertEqual(before['workers'],again.state()['workers'])
        with self.assertRaises(sqlite3.OperationalError):d.RuntimeServer(r.directory)
    def test_failed_initial_startup_waits_for_its_owned_worker_and_releases_log(self):
        root=self.root/'failed startup';root.mkdir()
        ws.write_json(root/'runtime.json',{'version':d.VERSION,'runtime_id':uuid.uuid4().hex,
            'source_digest':d.source_digest(),'expires_unix':time.time()+60,'token':'LOCAL_STARTUP_TEST_ONLY'})
        original=d.read;spawn=d.spawn_child;children=[]
        error=PermissionError(errno.EACCES,'MODELLED denial after readiness-read retry budget')
        def read(directory,name):
            value=original(directory,name)
            if name=='ready.json':raise error
            return value
        def capture(command,log):
            child=spawn(command,log);children.append(child);return child
        try:
            with mock.patch.object(d,'read',side_effect=read),mock.patch.object(d,'spawn_child',side_effect=capture):
                with self.assertRaises(PermissionError) as caught:d.RuntimeServer(root)
            self.assertIs(caught.exception,error);self.assertEqual(len(children),1)
            self.assertIsNotNone(children[0].poll())
            record=d.read(root,'teardown.json');self.assertTrue(record['all_workers_stopped'])
            self.assertEqual(record['reason'],'INITIAL_STARTUP_FAILED')
            self.assertEqual(record['workers'][0]['exit_observation'],'OWNED_CHILD_NATIVE_WAIT')
            self.assertEqual(record['workers'][0]['pid'],children[0].pid)
            log=next((root/'workers').glob('*/worker.log'));log.rename(log.with_suffix('.closed'))
            with self.assertRaisesRegex(ValueError,'torn-down'):d.launch(root,restart_existing=True)
        finally:
            for child in children:
                if child.poll() is None:child.terminate()
                child.wait(5)
    def test_failed_reattachment_does_not_stop_existing_workers(self):
        r,p=self.launch();before=r.state();p.kill();p.wait(5);original=d.read
        def read(directory,name):
            value=original(directory,name)
            if name=='ready.json':raise PermissionError(errno.EACCES,'MODELLED persistent readiness denial on restart')
            return value
        with mock.patch.object(d,'read',side_effect=read),mock.patch.object(d,'spawn_child') as spawn:
            with self.assertRaises(PermissionError):d.RuntimeServer(r.directory)
            spawn.assert_not_called()
        self.assertFalse((r.directory/'teardown.json').exists())
        restarted,_=d.launch(r.directory,restart_existing=True)
        self.assertEqual(restarted.state()['workers'],before['workers'])
        self.assertTrue(restarted.state()['workers']['stable-v1']['healthy'])
    def test_initial_startup_rollback_timeout_remains_unconfirmed(self):
        host=object.__new__(d.RuntimeServer);host.directory=self.root/'unknown rollback';host.directory.mkdir()
        child=SimpleNamespace(pid=123,poll=mock.Mock(return_value=None),terminate=mock.Mock(),
            wait=mock.Mock(side_effect=subprocess.TimeoutExpired('MODELLED owned child wait',5)))
        host.initial_workers=[('stable-v1',child)]
        with self.assertRaisesRegex(OSError,'no confirmed exit'):host.abort_initial_startup()
        record=d.read(host.directory,'teardown.json');self.assertFalse(record['all_workers_stopped'])
        self.assertFalse(record['workers'][0]['stopped']);self.assertEqual(record['workers'][0]['error'],'TimeoutExpired')
        child.terminate.assert_called_once_with();child.wait.assert_called_once_with(timeout=5)
    def test_live_but_unreachable_supervisor_blocks_competing_spawn(self):
        r,p=self.launch()
        with mock.patch.object(d.Runtime,'state',side_effect=ConnectionRefusedError),mock.patch.object(d,'spawn_child') as spawn:
            with self.assertRaisesRegex(OSError,'alive but unreachable'):d.launch(r.directory,restart_existing=True)
            spawn.assert_not_called()
    def test_source_change_and_expiry_block_restart_before_spawning(self):
        r,p=self.launch()
        with mock.patch.object(d,'source_digest',return_value='changed'),mock.patch.object(d,'spawn_child') as spawn:
            with self.assertRaises(ValueError):d.launch(r.directory,restart_existing=True)
            spawn.assert_not_called()
        with mock.patch.object(d.time,'time',return_value=time.time()+4000),mock.patch.object(d,'spawn_child') as spawn:
            with self.assertRaises(ValueError):d.launch(r.directory,restart_existing=True)
            spawn.assert_not_called()
    def test_pid_reuse_means_original_exit_but_never_sends_shutdown_to_replacement(self):
        r,p=self.launch();folder=next((r.directory/'workers').iterdir())
        old=d.read(folder,'ready.json')
        facts={'exists':True,'alive':True,'identity':{'replacement':True},'exit_code':None,'method':'TEST_NATIVE_LIFETIME'}
        with mock.patch.object(d,'os_process',return_value=facts),mock.patch.object(local,'http_json') as http:
            state=d.observe(folder);self.assertFalse(state['alive']);self.assertTrue(state['exit_confirmed'])
            self.assertEqual(state['os_identity'],old['os_identity']);http.assert_not_called()
    def test_missing_native_identity_and_wrong_nonce_fail_closed(self):
        r,p=self.launch();folder=next((r.directory/'workers').iterdir());path=folder/'ready.json';raw=path.read_bytes()
        try:
            ready=json.loads(raw);ready['os_identity']=None;ws.write_json(path,ready)
            with self.assertRaisesRegex(ValueError,'OS creation identity'):d.observe(folder)
            ready=json.loads(raw);ready['identity']='forged';ws.write_json(path,ready)
            with self.assertRaisesRegex(ValueError,'identity changed'):d.observe(folder)
        finally:path.write_bytes(raw)
    def test_unobservable_process_never_becomes_a_confirmed_exit(self):
        r,p=self.launch();folder=next((r.directory/'workers').iterdir())
        with mock.patch.object(d,'os_process',side_effect=PermissionError('denied')):
            with self.assertRaises(OSError):d.observe(folder)
    def test_exit_notice_alone_does_not_confirm_exit_or_release_capacity(self):
        r,p=self.launch();folder=next((r.directory/'workers').iterdir())
        ws.write_json(folder/'exit_notice.json',{'reason':'forged notice'})
        self.assertTrue(d.observe(folder)['alive']);self.assertFalse(d.observe(folder)['exit_confirmed'])
    def test_http_has_no_arbitrary_pid_or_harness_fault_tool_and_rejects_browser_origins(self):
        r,p=self.launch();a=r.access
        for path,body,origin in [('/kill',{'pid':os.getpid()},None),('/operate',{'tool':'kill'},None),('/teardown',{'runtime_id':r.identity},'http://127.0.0.1')]:
            conn=http.client.HTTPConnection('127.0.0.1',a['port'],timeout=3)
            headers={'X-Rehearsal-Token':a['token'],'Content-Type':'application/json'}
            if origin:headers['Origin']=origin
            conn.request('POST',path,json.dumps(body),headers);response=conn.getresponse();response.read();conn.close()
            self.assertIn(response.status,(400,403));self.assertTrue(r.state()['workers']['stable-v1']['alive'])
    def test_leases_stop_independent_worker_and_record_separate_teardown(self):
        # The lease includes process startup. Two seconds can expire before a
        # worker is ready on a shared runner, testing startup failure instead.
        started=time.time()
        r,p=d.launch(self.root/'short',ttl=30);self.runtimes.append(r)
        expires=d.read(r.directory,'runtime.json')['expires_unix']
        self.assertGreaterEqual(expires,started+30)
        self.assertTrue(r.state()['workers']['stable-v1']['alive'])
        deadline=time.monotonic()+max(0,expires-time.time())+9
        while not (r.directory/'teardown.json').exists() and time.monotonic()<deadline:time.sleep(.1)
        record=d.read(r.directory,'teardown.json');self.assertTrue(record['all_workers_stopped']);p.wait(5)
        self.assertGreaterEqual(time.time(),expires)
        self.assertTrue(all(worker['stopped'] for worker in record['workers']))
        with self.assertRaisesRegex(ValueError,'lease changed|torn-down'):d.launch(r.directory,restart_existing=True)
    def test_release_requires_os_exit_and_does_not_require_invented_exit_code(self):
        state={'routing':{'candidate-v2':0},'route_owner':'deploy-A'}
        actual={'candidate_percent':0,'route_owner':'deploy-A','workers':{'candidate-v2':{'alive':False,'exit_confirmed':False,'exit_code':0}}}
        with self.assertRaises(sim.Denied):d.Engine._live_guard(None,state,actual,'release_capacity')
        actual['workers']['candidate-v2'].update(exit_confirmed=True,exit_code=None)
        d.Engine._live_guard(None,state,actual,'release_capacity')
    def test_windows_handle_identity_exit_observation_and_rights_are_explicit(self):
        from ctypes import wintypes as w
        def times(handle,created,*_):
            pointer=ctypes.cast(created,ctypes.POINTER(w.FILETIME));pointer.contents.dwLowDateTime=123;pointer.contents.dwHighDateTime=4;return 1
        def exitcode(handle,pointer):ctypes.cast(pointer,ctypes.POINTER(w.DWORD)).contents.value=259;return 1
        kernel=SimpleNamespace(OpenProcess=mock.Mock(return_value=7),GetProcessTimes=mock.Mock(side_effect=times),
            WaitForSingleObject=mock.Mock(return_value=258),GetExitCodeProcess=mock.Mock(side_effect=exitcode),CloseHandle=mock.Mock(return_value=1))
        with mock.patch.object(d.platform,'system',return_value='Windows'),mock.patch.object(ctypes,'WinDLL',create=True,return_value=kernel):
            alive=d.os_process(100);self.assertTrue(alive['alive']);self.assertIsNone(alive['exit_code'])
            self.assertEqual(alive['identity']['created_filetime'],(4<<32)|123)
            kernel.OpenProcess.assert_called_with(0x1000|0x100000,False,100)
            kernel.WaitForSingleObject.return_value=0;exited=d.os_process(100)
            self.assertFalse(exited['alive']);self.assertEqual(exited['exit_code'],259)
            self.assertEqual(kernel.CloseHandle.call_count,2)
    def test_windows_access_denial_is_unknown_and_invalid_pid_is_rejected(self):
        def first(handle,entry):entry._obj.th32ProcessID=9;return 1
        kernel=SimpleNamespace(**{k:mock.Mock(return_value=0) for k in ('OpenProcess','GetProcessTimes','WaitForSingleObject','GetExitCodeProcess')},
            CloseHandle=mock.Mock(return_value=1),CreateToolhelp32Snapshot=mock.Mock(return_value=202),
            Process32FirstW=mock.Mock(side_effect=first),Process32NextW=mock.Mock())
        with mock.patch.object(d.platform,'system',return_value='Windows'),mock.patch.object(ctypes,'WinDLL',create=True,return_value=kernel),mock.patch.object(ctypes,'get_last_error',create=True,return_value=5):
            with self.assertRaises(OSError) as caught:d.os_process(9)
            self.assertEqual(caught.exception.winerror,5);self.assertEqual(caught.exception.observation_stage,'OpenProcess')
            kernel.CreateToolhelp32Snapshot.assert_called_once_with(2,0)
            kernel.Process32NextW.assert_not_called();kernel.CloseHandle.assert_called_once_with(202)
        for pid in (0,-1,True,'12'):
            with self.assertRaises(ValueError):d.os_process(pid)

if __name__=='__main__':unittest.main()
