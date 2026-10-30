from test_paths import linked_path
from pathlib import Path
from unittest import mock
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from zipfile import ZipFile
import contextlib
import io
import json
import shutil
import tempfile
import threading
import time
import unittest

import acceptance
import dashboard as d
import mission_control as m
import change_coordinator as c

class DashboardTests(unittest.TestCase):
    def test_only_the_fixed_free_mission_may_be_launched_from_the_dashboard(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);(root/'mission_control.py').write_text('# fixture')
            lab=d.ScenarioLab(d.EvidenceStore(root));command=lab.command({'kind':'mission'})
            self.assertIn('demo',command);self.assertNotIn('--live',command)
            for extra in ({'live':True},{'mission':'other'},{'fault':'candidate'},{'approve':True},{'ttl':3600},{'output':'../outside'}):
                with self.assertRaises(d.EvidenceError):lab.command({'kind':'mission',**extra})
    def test_saved_mission_reads_public_evidence_only_and_exports_no_private_controls(self):
        with tempfile.TemporaryDirectory(prefix='mission ui ') as root:
            root=Path(root)
            with contextlib.redirect_stdout(io.StringIO()):path,value=m.demo(root/m.FOLDER)
            before={str(p.relative_to(path)):c.sha(p) for p in path.rglob('*') if p.is_file()}
            server=d.server_for(root,0,read_only=True);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base='http://127.0.0.1:'+str(server.server_port)
            try:
                with mock.patch.object(c,'open_broker',side_effect=AssertionError('private runtime accessed')),mock.patch.object(m,'Store',side_effect=AssertionError('private database accessed')):
                    with urlopen(base+'/api/run?kind=mission&id='+path.name,timeout=10) as response:view=json.load(response)
                self.assertEqual(view['summary']['phase'],'RECOVERED_SERVICE_ACTIVE');self.assertTrue(view['journal_matches_final']);self.assertEqual(view['summary']['service']['metrics']['committed'],40)
                for relative in ('mission.sqlite3','service/'+value['child']+'/runtime/.access.json'):
                    with self.assertRaises(HTTPError):urlopen(base+'/mission_runs/'+path.name+'/'+relative,timeout=5)
                with urlopen(base,timeout=5) as response:page=response.read().decode();self.assertIn('Mission control',page);self.assertIn('missionView(ticket)',page)
                request=Request(base+'/api/jobs',data=b'{"kind":"mission"}',method='POST',headers={'Origin':base,'Content-Type':'application/json','X-Rehearsal-Token':server.session_token})
                with self.assertRaises(HTTPError):urlopen(request,timeout=5)
                self.assertEqual(before,{str(p.relative_to(path)):c.sha(p) for p in path.rglob('*') if p.is_file()})
            finally:server.shutdown();server.server_close();server.lab.close();thread.join(5)
            result=acceptance.evidence_export(root/'mission-evidence.zip',[path])
            with ZipFile(root/'mission-evidence.zip') as z:
                self.assertTrue(any(n.endswith('summary.json') for n in z.namelist()))
                self.assertFalse(any('runtime/' in n or n.endswith('.sqlite3') or n.endswith('.env') for n in z.namelist()))
            self.assertGreater(result['records'],2)
    def test_actual_free_mission_job_completes_all_roles_and_native_cleanup(self):
        with tempfile.TemporaryDirectory(prefix='mission launch ') as root:
            root=Path(root)
            for path in Path(d.__file__).resolve().parent.glob('*.py'):shutil.copyfile(path,root/path.name)
            server=d.server_for(root,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base='http://127.0.0.1:'+str(server.server_port)
            try:
                request=Request(base+'/api/jobs',data=b'{"kind":"mission"}',method='POST',headers={'Origin':base,'Content-Type':'application/json','X-Rehearsal-Token':server.session_token})
                with urlopen(request,timeout=5) as response:self.assertEqual(response.status,202)
                deadline=time.monotonic()+60
                while server.lab.thread.is_alive():server.lab.thread.join(.1);self.assertLess(time.monotonic(),deadline)
                job=server.lab.status()['job'];self.assertEqual(job['status'],'COMPLETED',job);self.assertEqual(job['result']['roles'],3);self.assertEqual(job['result']['model_calls'],0)
                self.assertTrue(m.read(root/m.FOLDER/job['result']['run'])['teardown']['all_workers_stopped'])
            finally:server.shutdown();server.server_close();server.lab.close();thread.join(5)
    def test_linked_mission_root_is_rejected_before_a_launch(self):
        with tempfile.TemporaryDirectory() as root,tempfile.TemporaryDirectory() as outside:
            root=Path(root)
            with linked_path(root/m.FOLDER,outside,directory=True):
                with self.assertRaises(d.EvidenceError):d.EvidenceStore(root).directory('mission','run')

if __name__=='__main__':unittest.main()
