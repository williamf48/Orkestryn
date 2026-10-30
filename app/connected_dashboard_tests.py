from test_paths import linked_path
"""Connected dashboard boundaries and actual public saved-state integration."""
from pathlib import Path
from unittest import mock
import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError

import connected_team as c
import dashboard as d
import durable_runtime as durable


class DashboardTests(unittest.TestCase):
    def test_free_launch_commands_are_fixed_and_reject_live_owner_restart_and_reserved_inputs(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            for name in ('connected_team.py','connected_evaluation.py'):(root/name).write_text('# fixed fixture')
            lab=d.ScenarioLab(d.EvidenceStore(root))
            team=lab.command({'kind':'connected','scenario':'dependency'});self.assertIn('300',team);self.assertNotIn('--live',team)
            evaluation=lab.command({'kind':'connected_evaluation','split':'development'});self.assertNotIn('--live',evaluation)
            for payload in ({'kind':'connected','scenario':'routing','ttl':3600},{'kind':'connected','scenario':'routing','live':True},
                            {'kind':'connected','scenario':'routing','decision':'withdraw_route'},{'kind':'connected','scenario':'routing','restart':True},
                            {'kind':'connected_evaluation','split':'confirmation'},{'kind':'connected_evaluation','split':'development','live':True}):
                with self.assertRaises(d.EvidenceError):lab.command(payload)
    def test_saved_connected_view_reads_public_evidence_without_loading_private_capabilities(self):
        with tempfile.TemporaryDirectory(prefix='connected dashboard ') as root:
            root=Path(root)
            with contextlib.redirect_stdout(io.StringIO()):path,summary=c.run(root/c.FOLDER,ttl=120)
            try:
                before=c.Client(path).status();store=d.EvidenceStore(root)
                with mock.patch.object(c,'Client',side_effect=AssertionError('Dashboard read private capability')):
                    view=store.run('connected',path.name);catalog=store.catalog()
                self.assertTrue(view['journal_matches_final']);self.assertEqual(view['session']['runtime_id'],before['runtime_id'])
                self.assertEqual(catalog['runs']['connected'][0]['id'],path.name)
                self.assertNotIn(c.Client(path).access['token'],json.dumps(view));self.assertNotIn(durable.Runtime(path/'runtime').token,json.dumps(view))
                with self.assertRaises(d.EvidenceError):store.run('connected','../outside')
                server=d.server_for(root,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                base='http://127.0.0.1:'+str(server.server_port)
                try:
                    with urlopen(base,timeout=4) as response:html=response.read().decode();self.assertIn('Connected team',html);self.assertIn('tab="connected"',html)
                    with urlopen(base+'/api/run?kind=connected&id='+path.name,timeout=5) as response:self.assertEqual(json.load(response)['session']['runtime_id'],before['runtime_id'])
                    with self.assertRaises(HTTPError):urlopen(base+'/connected_team_runs/'+path.name+'/.access.json',timeout=4)
                finally:server.shutdown();server.server_close();server.lab.close();thread.join(5)
                self.assertEqual(c.Client(path).status()['actual_at_outcome']['workers'],before['actual_at_outcome']['workers'])
            finally:self.assertTrue(c.stop(path)['teardown']['all_workers_stopped'])
    def test_failed_team_remains_visible_without_inventing_a_continuing_runtime(self):
        class Invalid(c.ScriptedRole):
            def ask(self,context):return '{"bad":"contract"}',{'finish_reason':'stop','refused':False}
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);providers={r:Invalid(r,'routing') if r=='factory' else c.ScriptedRole(r,'routing') for r in c.team.ROLES}
            with contextlib.redirect_stdout(io.StringIO()):path,summary=c.run(root/c.FOLDER,providers,scenario='routing',ttl=120)
            store=d.EvidenceStore(root);catalog=store.catalog();self.assertEqual(catalog['runs']['connected'][0]['id'],path.name)
            view=store.run('connected',path.name);self.assertTrue(view['partial_team']);self.assertEqual(view['summary']['role_requests'],1)
            self.assertFalse((path/'runtime').exists());self.assertNotIn('session',view)
    def test_linked_connected_output_folder_is_rejected_before_launcher(self):
        with tempfile.TemporaryDirectory() as root,tempfile.TemporaryDirectory() as outside:
            root=Path(root)
            (root/'connected_team.py').write_text('# fixture')
            with linked_path(root/c.FOLDER,outside,directory=True):
                lab=d.ScenarioLab(d.EvidenceStore(root))
                with self.assertRaises(d.EvidenceError):lab.command({'kind':'connected','scenario':'routing'})

if __name__=='__main__':unittest.main()
