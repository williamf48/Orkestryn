"""Evidence export and honest native/live acceptance boundaries."""
from pathlib import Path
from unittest import mock
from zipfile import ZipFile
import contextlib
import io
import json
import tempfile
import unittest

import acceptance as a
from test_paths import linked_path
import connected_team as c
import simulator as sim
import team_workspace as ws


class AcceptanceTests(unittest.TestCase):
    def test_export_whitelist_excludes_private_capabilities_environment_and_runtime_databases(self):
        with tempfile.TemporaryDirectory(prefix='acceptance export ') as root:
            root=Path(root);run=root/'run';run.mkdir()
            public={'summary.json':'{"status":"fixture"}','roles/factory/response.json':'{"visible_content":"public fixture"}','policy_snapshot.json':'{}'}
            private={'.access.json':'PRIVATE_CAPABILITY','runtime/runtime.json':'RUNTIME_CAPABILITY','runtime/summary.json':'NESTED_PRIVATE','runtime/runtime.sqlite3':'PRIVATE_DB','.env':'API_SECRET','owner_supervisor.log':'POSSIBLE_SECRET','coordinator.sqlite3':'POLICY_SQLITE_EXCLUDED'}
            for name,data in {**public,**private}.items():
                path=run/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(data)
            result=a.evidence_export(root/'export.zip',[run])
            with ZipFile(root/'export.zip') as archive:
                self.assertEqual(len(archive.namelist()),3);body=b''.join(archive.read(n) for n in archive.namelist())
                self.assertNotIn(b'PRIVATE',body);self.assertNotIn(b'SECRET',body)
            self.assertEqual(result['records'],3)
    def test_linked_public_evidence_is_not_exported(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);(root/'real.json').write_text('{"private":true}')
            with linked_path(root/'summary.json',root/'real.json'):
                result=a.evidence_export(root/'export.zip',[root]);self.assertEqual(result['records'],0)
    def test_live_and_live_only_require_console_before_credentials_or_output(self):
        with mock.patch.object(a.team.terminal_guard,'interactive_input',return_value=False),mock.patch.object(a,'getpass') as password,mock.patch.object(a.service,'new_directory') as directory:
            for kwargs in ({'live':True},{'live_only':True}):
                with self.assertRaises(ValueError):a.run(Path('/uncreated-fixture'),**kwargs)
            password.assert_not_called();directory.assert_not_called()
    def test_default_walkthrough_is_zero_call_and_never_claims_windows_from_linux(self):
        with tempfile.TemporaryDirectory() as root,mock.patch.object(a,'verify_sources',return_value={'status':'MATCH'}),mock.patch.object(a,'walkthrough',return_value={'status':'PASSED','model_calls':0}),mock.patch.object(a.platform,'system',return_value='Linux'),mock.patch.object(a,'live_runs') as live:
            path,s=a.run(Path(root),walkthrough_only=True)
            self.assertEqual(s['status'],'PASSED');self.assertEqual(s['native_windows'],'NOT_RUN_ON_THIS_PLATFORM');live.assert_not_called()
            self.assertFalse(s['confirmation_executed']);self.assertEqual(s['live']['status'],'NOT_RUN')
    def test_windows_walkthrough_only_does_not_certify_full_native_suite(self):
        with tempfile.TemporaryDirectory() as root,mock.patch.object(a,'verify_sources',return_value={'status':'MATCH'}),mock.patch.object(a,'walkthrough',return_value={'status':'PASSED','model_calls':0}),mock.patch.object(a.platform,'system',return_value='Windows'):
            path,s=a.run(Path(root),walkthrough_only=True);self.assertEqual(s['native_windows'],'WALKTHROUGH_ONLY')
    def test_source_mismatch_stops_before_suite_or_live_requests(self):
        with tempfile.TemporaryDirectory() as root,mock.patch.object(a,'verify_sources',return_value={'status':'EDITED_OR_MANIFEST_PENDING'}),mock.patch.object(a.subprocess,'run') as process,mock.patch.object(a,'live_runs') as live:
            path,s=a.run(Path(root));self.assertEqual(s['status'],'FAILED');process.assert_not_called();live.assert_not_called()
    def test_live_budgets_are_separate_capped_and_use_separate_exact_consents(self):
        old={'selected_set_completed':True,'model_calls':21};new={'status':'COMPLETED','model_calls':24}
        with tempfile.TemporaryDirectory() as root,mock.patch.object(a.team.terminal_guard,'interactive_input',return_value=True),mock.patch.dict(a.os.environ,{'NEBIUS_API_KEY':'LOCAL_FIXTURE_NO_ENDPOINT'}),mock.patch.object(a.service,'NemotronAgent'),mock.patch.object(a.team,'RoleClient'),mock.patch.object(a.original,'run_campaign',return_value=(Path(root)/'old',old)) as original,mock.patch.object(a.evaluation,'run_campaign',return_value=(Path(root)/'new',new)) as combined,contextlib.redirect_stdout(io.StringIO()):
            record,paths=a.live_runs(Path(root));self.assertEqual(record['attempt_ceiling'],54);self.assertEqual(record['actual_attempts'],45)
            self.assertEqual(original.call_args.kwargs['call_limit'],24);self.assertEqual(combined.call_args.kwargs['call_limit'],30)
            self.assertIs(original.call_args.kwargs['consent'],a.service.terminal_consent)
            self.assertIs(combined.call_args.kwargs['owner_consent'],a.evaluation.terminal_owner)
    def test_reserved_live_confirmation_replaces_combined_split_and_lowers_ceiling(self):
        with tempfile.TemporaryDirectory() as root,mock.patch.object(a.team.terminal_guard,'interactive_input',return_value=True),mock.patch.dict(a.os.environ,{'NEBIUS_API_KEY':'LOCAL_FIXTURE'}),mock.patch.object(a.service,'NemotronAgent'),mock.patch.object(a.team,'RoleClient'),mock.patch.object(a.original,'run_campaign',return_value=(Path(root)/'old',{'selected_set_completed':False,'model_calls':0})),mock.patch.object(a.evaluation,'run_campaign',return_value=(Path(root)/'new',{'status':'CALL_BUDGET','model_calls':0})) as combined,contextlib.redirect_stdout(io.StringIO()):
            record,_=a.live_runs(Path(root),'confirmation');self.assertEqual(record['attempt_ceiling'],42);self.assertEqual(combined.call_args.args[1],'confirmation');self.assertEqual(combined.call_args.kwargs['call_limit'],18)
    def test_child_output_is_visible_and_logged_before_waiting_for_exit(self):
        with tempfile.TemporaryDirectory() as root:
            captured=io.StringIO();child=mock.MagicMock();child.__enter__.return_value=child;child.stdout=io.BytesIO('current test … ERROR\n'.encode())
            def done():
                self.assertIn('current test … ERROR',captured.getvalue());return 7
            child.wait.side_effect=done
            with mock.patch.object(a.subprocess,'Popen',return_value=child),contextlib.redirect_stdout(captured):
                code=a.stream_command(['fixed-child'],Path(root)/'progress.log',{},Path(root))
            self.assertEqual(code,7);self.assertEqual((Path(root)/'progress.log').read_text(),captured.getvalue())
    def test_acceptance_retains_failure_and_exports_report_without_pretending_a_pass(self):
        with tempfile.TemporaryDirectory() as root,mock.patch.object(a,'verify_sources',side_effect=ValueError('fixture invalid source')):
            path,s=a.run(Path(root));self.assertEqual(s['status'],'FAILED');self.assertTrue((path/'Orkestryn-Evidence.zip').exists())
            self.assertIn('fixture invalid source',ws.read_json(path/'acceptance.json')['error'])

if __name__=='__main__':unittest.main()
