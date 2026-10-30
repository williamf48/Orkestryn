from test_paths import linked_path
from pathlib import Path
from unittest import mock
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import contextlib
import io
import json
import tempfile
import threading
import time
import unittest

import change_coordinator as c
import dashboard as d

class DashboardTests(unittest.TestCase):
    def test_only_fixed_free_demo_and_policy_benchmark_launches_are_allowed(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            for name in ('change_coordinator.py','coordination_benchmark.py'):(root/name).write_text('# fixture')
            lab=d.ScenarioLab(d.EvidenceStore(root))
            for kind in ('coordination','coordination_benchmark'):
                command=lab.command({'kind':kind});self.assertNotIn('--live',command)
                for extra in ({'live':True},{'actor':'A'},{'ttl':3600},{'plan':'other'},{'execute':True},{'output':'../outside'}):
                    with self.assertRaises(d.EvidenceError):lab.command({'kind':kind,**extra})
    def test_preview_is_local_guarded_and_read_only_even_when_real_launches_are_disabled(self):
        with tempfile.TemporaryDirectory(prefix='coordination ui ') as root:
            root=Path(root)
            with contextlib.redirect_stdout(io.StringIO()):path,value=c.demo(root/c.FOLDER)
            plan=value['plans'][-1];before={str(p.relative_to(path)):c.sha(p) for p in path.rglob('*') if p.is_file()}
            server=d.server_for(root,0,read_only=True);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base='http://127.0.0.1:'+str(server.server_port)
            payload={'run':path.name,'plan_digest':plan['plan_digest'],'intent_id':'deploy-large','units':5}
            def post(payload,headers=None):
                request=Request(base+'/api/coordination-preview',method='POST',data=json.dumps(payload).encode(),headers=headers or {'Content-Type':'application/json','Origin':base,'X-Rehearsal-Token':server.session_token})
                with urlopen(request,timeout=20) as response:return json.load(response)
            try:
                with self.assertRaises(HTTPError) as caught:post(payload,{'Content-Type':'application/json'})
                self.assertEqual(caught.exception.code,403)
                for extra in ({'units':True},{'units':1001},{'plan_digest':'old'},{'run':'../outside'},{'actor':'A'}):
                    with self.assertRaises(HTTPError):post({**payload,**extra})
                result=post(payload);self.assertEqual(len(result['result']['selected']['accepted']),5);self.assertFalse(result['live_effects']);self.assertTrue(result['requires_new_intent_and_exact_approval'])
                with mock.patch.object(c,'open_broker',side_effect=AssertionError('public reader accessed private service')):
                    with urlopen(base+'/api/run?kind=coordination&id='+path.name,timeout=5) as response:view=json.load(response)
                self.assertTrue(view['journal_matches_final']);self.assertEqual(view['summary']['lifecycle'],'STOPPED')
                with self.assertRaises(HTTPError):urlopen(base+'/coordination_runs/'+path.name+'/runtime/.access.json',timeout=5)
                self.assertEqual(before,{str(p.relative_to(path)):c.sha(p) for p in path.rglob('*') if p.is_file()})
            finally:server.shutdown();server.server_close();server.lab.close();thread.join(5)
    def test_actual_browser_free_demo_creates_a_completed_batch_with_teardown(self):
        with tempfile.TemporaryDirectory(prefix='coordination launch ') as root:
            root=Path(root);source=Path(d.__file__).resolve().parent
            import shutil
            for path in source.glob('*.py'):shutil.copyfile(path,root/path.name)
            server=d.server_for(root,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base='http://127.0.0.1:'+str(server.server_port)
            try:
                request=Request(base+'/api/jobs',method='POST',data=b'{"kind":"coordination"}',headers={'Content-Type':'application/json','Origin':base,'X-Rehearsal-Token':server.session_token})
                with urlopen(request,timeout=5) as response:self.assertEqual(response.status,202)
                deadline=time.monotonic()+60
                while server.lab.thread.is_alive():server.lab.thread.join(.1);self.assertLess(time.monotonic(),deadline)
                job=server.lab.status()['job'];self.assertEqual(job['status'],'COMPLETED',job);self.assertEqual((job['result']['selected'],job['result']['waiting'],job['result']['model_calls']),(4,1,0))
                path=root/c.FOLDER/job['result']['run'];self.assertTrue(c.sessions.read_file(path,'teardown.json')['all_workers_stopped'])
            finally:server.shutdown();server.server_close();server.lab.close();thread.join(5)
    def test_linked_preview_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as root,tempfile.TemporaryDirectory() as outside:
            root=Path(root)
            with linked_path(root/c.FOLDER,outside,directory=True):
                with self.assertRaises(d.EvidenceError):d.EvidenceStore(root).directory('coordination','run')

if __name__=='__main__':unittest.main()
