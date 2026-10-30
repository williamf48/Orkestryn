"""Download start/restart and real scripted evaluation; no provider or browser use."""
import contextlib
import io
import json
import os
from pathlib import Path
import threading
import unittest
from unittest import mock
from zipfile import ZipFile

import downloadable_demo as demo
import hosted_tests
import judge_setup
import judge_setup_tests
import named_access
import operations_evidence
import operations_workspace as ops
import release_check


class DownloadableTests(unittest.TestCase):
    setUp = hosted_tests.HostedTests.setUp
    tearDown = hosted_tests.HostedTests.tearDown
    request = judge_setup_tests.JudgeSetupTests.request
    login = judge_setup_tests.JudgeSetupTests.login

    def start(self):
        self.settings_dir = self.root / 'private-demo'
        with mock.patch.object(demo, 'package_version', return_value='TEST_ONLY'):
            self.server, self.guide, _ = demo.prepare_demo(self.settings_dir)
        self.settings = judge_setup.load(self.settings_dir)
        self.root = self.server.root
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def test_one_click_demo_ignores_provider_key_and_needs_only_local_passwords(self):
        with mock.patch.dict(os.environ, {'NEBIUS_API_KEY':'PRIVATE_TEST_OWNER_VALUE'}), mock.patch.object(ops, 'RoleClient') as client:
            self.start();client.assert_not_called()
        page = self.guide.read_text()
        self.assertIn('Free scripted rehearsal', page)
        self.assertNotIn('PRIVATE_TEST_OWNER_VALUE', page)
        self.assertEqual(self.request('/api/state')[0], 401)
        account = self.login('judge-manager')
        status, state, _ = self.request('/api/state', account=account)
        self.assertEqual(status, 200);self.assertFalse(state['live_proposals_enabled'])
        self.assertFalse(state['inference_enabled']);self.assertEqual(state['runs'], [])

    def test_private_welcome_credentials_are_not_served_over_http(self):
        self.start();page = self.guide.read_text()
        self.assertIn('http://127.0.0.1:' + str(self.server.server_port), page)
        for password in self.settings['accounts'].values():
            self.assertIn(password, page)
            self.assertNotIn(password, self.request('/')[1])
        self.assertEqual(self.request('/' + self.guide.name)[0], 404)
        self.assertIsNotNone(release_check.path_problem(self.guide.name))
        self.assertIsNotNone(release_check.path_problem('copied/' + self.guide.name))

    def test_full_downloadable_scripted_flow_uses_both_accounts_and_exports_no_passwords(self):
        hosted_tests.HostedTests.test_both_judge_accounts_finish_recovery_and_preserve_customer_work(self)
        path = next(p for p in self.root.iterdir() if p.is_dir() and not p.name.startswith('.'))
        raw, _ = operations_evidence.build_bytes(path)
        result = operations_evidence.verify_bytes(raw)
        self.assertEqual(result['status'], 'INTEGRITY_CHECKED')
        self.assertEqual(result['proposal_mode'], 'SCRIPTED_FIXTURE')
        with ZipFile(io.BytesIO(raw)) as archive:
            for name in archive.namelist():
                for password in self.settings['accounts'].values():self.assertNotIn(password.encode(), archive.read(name))

    def test_restart_keeps_accounts_sessions_revocations_and_old_evidence(self):
        self.start();manager = self.login('judge-manager');customer = self.login('judge-customer')
        credentials = (self.settings_dir / 'credentials.json').read_bytes()
        old_guide = self.guide;old_content = old_guide.read_bytes()
        marker = self.root / 'saved-evidence.txt';marker.write_text('Existing saved evidence')
        auth = named_access.Access(self.root)
        try:
            admin_token, _, admin = auth.login('workspace-admin', self.settings['accounts']['workspace-admin'])
            subject = manager['principal']['subject']
            auth.change(admin, subject, roles=['viewer']);auth.logout(admin_token)
        finally:auth.close()
        self.server.shutdown();self.server.server_close();self.thread.join(5)
        with mock.patch.object(demo, 'package_version', return_value='TEST_ONLY'):
            self.server, self.guide, _ = demo.prepare_demo(self.settings_dir)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True);self.thread.start()
        state = self.request('/api/state', account=self.login('judge-manager'))[1]
        self.assertEqual(state['principal']['roles'], ['viewer'])
        self.assertEqual(self.request('/api/state', account=customer)[0], 200)
        self.assertEqual((self.settings_dir / 'credentials.json').read_bytes(), credentials)
        self.assertEqual(old_guide.read_bytes(), old_content);self.assertNotEqual(self.guide, old_guide)
        self.assertEqual(marker.read_text(), 'Existing saved evidence')

    def test_data_directory_uses_local_appdata_without_touching_main_operations(self):
        chosen = demo.default_directory({'LOCALAPPDATA':str(self.root / 'Local data')})
        self.assertEqual(chosen, self.root / 'Local data' / 'AgentRehearsal' / 'DownloadableDemo')
        fallback = demo.default_directory({})
        self.assertIn('judge_access_private', fallback.parts)
        self.assertNotEqual(chosen, ops.ROOT / ops.FOLDER)
        self.assertNotEqual(fallback, ops.ROOT / ops.FOLDER)
        self.assertEqual(demo.default_directory({'LOCALAPPDATA':'relative-path'}), fallback)

    def test_welcome_escapes_password_content_and_rejects_nonlocal_link_parameters(self):
        settings = judge_setup.prepare(self.root / 'private')
        settings['accounts']['judge-manager'] = '<script>private fixture only</script>'
        body = demo.welcome_html(settings, 12345)
        self.assertIn('&lt;script&gt;', body);self.assertNotIn('<script>', body)
        for port in (True, 0, -1, 65536, '443', 'https://example.invalid'):
            with self.assertRaises(ValueError):demo.welcome_html(settings, port)

    def test_incomplete_package_stops_before_credentials_or_listener(self):
        directory = self.root / 'new-private';output = io.StringIO()
        with mock.patch.object(demo.install_update, 'payload', side_effect=ValueError('Incomplete fixture')), mock.patch.object(judge_setup, 'local_server') as listener, contextlib.redirect_stdout(output):
            self.assertEqual(demo.main(['--directory', str(directory), '--no-browser']), 1)
            listener.assert_not_called()
        self.assertFalse(directory.exists());self.assertIn('Extract the complete ZIP', output.getvalue())

    def test_guide_write_failure_closes_new_listener_without_resetting_credentials(self):
        directory = self.root / 'new-private';server = mock.Mock()
        settings = judge_setup.prepare(directory);before = (directory / 'credentials.json').read_bytes()
        server.server_port = 12345
        with mock.patch.object(demo, 'package_version', return_value='TEST_ONLY'), mock.patch.object(judge_setup, 'local_server', return_value=server), mock.patch.object(judge_setup, 'private_write', side_effect=OSError('Fixture write denial')), mock.patch.object(demo.judge_evaluation, 'provision', return_value={'version':demo.judge_evaluation.VERSION,'name':'judge-admin','password':'Fixture judge administrator password!'}):
            with self.assertRaises(OSError):demo.prepare_demo(directory)
        server.server_close.assert_called_once()
        self.assertEqual((directory / 'credentials.json').read_bytes(), before)

    def test_cli_does_not_print_passwords_or_automatically_open_browser_when_disabled(self):
        output = io.StringIO();directory = self.root / 'new-private';server = mock.Mock()
        server.server_port = 12345;server.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(demo, 'package_version', return_value='TEST_ONLY'), mock.patch.object(judge_setup, 'local_server', side_effect=lambda path, **_: (judge_setup.prepare(path), server)[1]), mock.patch.object(demo.webbrowser, 'open') as browser, mock.patch.object(demo.judge_evaluation, 'provision', return_value={'version':demo.judge_evaluation.VERSION,'name':'judge-admin','password':'Fixture judge administrator password!'}), contextlib.redirect_stdout(output):
            self.assertEqual(demo.main(['--directory', str(directory), '--no-browser']), 0)
            browser.assert_not_called();server.server_close.assert_called_once()
        for password in judge_setup.load(directory)['accounts'].values():self.assertNotIn(password, output.getvalue())

    def test_optional_download_check_runs_real_scripted_recovery_and_confirms_shutdown(self):
        with mock.patch.object(demo, 'package_version', return_value='TEST_ONLY'), mock.patch.object(ops, 'RoleClient') as client:
            result = demo.check_demo(self.root / 'finite-check')
        client.assert_not_called();self.assertEqual(result['status'], 'PASSED')
        self.assertEqual(result['model_calls'], 0);self.assertTrue(result['owned_workers_confirmed_stopped'])
        self.assertEqual(result['archive_integrity'], 'INTEGRITY_CHECKED')


if __name__ == '__main__':unittest.main()
