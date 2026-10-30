"""Single-account judge evaluation, account migration and exact role approvals."""
import io
import json
from pathlib import Path
import threading
import time
import unittest
from unittest import mock
from zipfile import ZipFile

import browser_proposals
import browser_proposals_tests
import downloadable_demo as demo
import hosted_tests
import judge_evaluation as evaluation
import judge_setup
import judge_setup_tests
import named_access
import operations_evidence
import operations_outcomes
import operations_workspace as ops
import release_check


class JudgeEvaluationTests(unittest.TestCase):
    setUp = hosted_tests.HostedTests.setUp
    tearDown = hosted_tests.HostedTests.tearDown
    request = judge_setup_tests.JudgeSetupTests.request

    def start(self):
        self.settings_dir = self.root / 'private-demo'
        with mock.patch.object(demo, 'package_version', return_value='TEST_ONLY'):
            self.server, self.guide, _ = demo.prepare_demo(self.settings_dir)
        self.settings = judge_setup.load(self.settings_dir)
        self.judge = evaluation.load(self.settings_dir / evaluation.PRIVATE_DIRECTORY / 'credentials.json')
        self.root = self.server.root
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def login(self, name='judge-admin'):
        password = self.judge['password'] if name == evaluation.NAME else self.settings['accounts'][name]
        status, data, headers = self.request('/api/login', {'name':name, 'password':password})
        self.assertEqual(status, 200, data)
        return {'csrf':data['csrf'], 'cookie':headers['Set-Cookie'].split(';')[0], 'principal':data['principal']}

    def approve(self, run, kind, account, **extra):
        status, review, _ = self.request('/api/review', {'run':run, 'kind':kind, **extra}, account)
        self.assertEqual(status, 200, review)
        status, result, _ = self.request('/api/action', {'action':'approve-review', 'run':run, 'kind':kind,
            'digest':review['digest'], 'phrase':review['phrase'], **extra}, account)
        self.assertIn(status, (200, 202), result)
        if kind.startswith('proposal-'):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                value = self.server.proposal_jobs().get(run)
                if value and value['status'] not in {'QUEUED', 'RUNNING'}:
                    break
                time.sleep(.02)
            self.assertEqual(value['status'], 'FINISHED', value)

    def flow(self, live=False):
        self.start();account = self.login()
        request = {'action':'create', 'profile':'balanced'}
        if live:
            request.update(proposal_mode='live', nebius_api_key=browser_proposals_tests.KEY)
        status, created, _ = self.request('/api/action', request, account)
        self.assertEqual(status, 200, created);run = created['run']
        if live:
            self.assertEqual(len(browser_proposals_tests.Connection.calls), 0)
            self.approve(run, 'proposal-factory', account)
            self.approve(run, 'proposal-governance', account)
        self.approve(run, 'batch', account)
        self.approve(run, 'fault-candidate', account)
        auth = named_access.Access(self.root)
        try:
            with ops.opened(self.root / run, auth=auth) as mission:
                mission.observe();mission.observe()
                if not live:
                    mission.advance()
        finally:
            auth.close()
        if live:
            self.approve(run, 'proposal-recovery', account)
        self.approve(run, 'recovery', account)
        self.approve(run, 'failover-manager', account)
        before = ops.read(self.root / run)
        self.assertEqual(before['service']['actual_at_outcome']['candidate_percent'], 20)
        self.assertEqual(set(before['operations']['emergency']['grants']), {'manager'})
        self.approve(run, 'failover-owner_C', account)
        protected = ops.read(self.root / run)['service']['policy_snapshot']['state']['deployments']['candidate-v2']['dependencies']
        self.assertIn({'id':'C-mission-job', 'owner_run':'route-C'}, protected)
        self.approve(run, 'job', account, job='C-mission-job')
        saved = ops.read(self.root / run)
        self.assertEqual(saved['phase'], 'RECOVERED_SERVICE_ACTIVE')
        self.assertEqual(saved['service']['metrics']['committed'], 40)
        grants = saved['operations']['emergency']['grants']
        self.assertEqual({g['principal']['subject'] for g in grants.values()}, {account['principal']['subject']})
        self.assertEqual(ops.verify(self.root / run)['status'], 'CONSISTENT')
        report = operations_outcomes.build(self.root / run)
        self.assertEqual(report['comparison_basis']['distinct_handoff_accounts'], 1)
        self.assertTrue(any('Single-account evaluation' in text for text in report['limitations']))
        self.assertEqual(self.request('/api/action', {'action':'stop', 'run':run}, account)[0], 200)
        self.assertTrue(ops.read(self.root / run)['teardown']['all_workers_stopped'])
        return run

    def test_recommended_account_has_full_permissions_but_startup_has_no_model_calls(self):
        with mock.patch.object(ops, 'RoleClient') as sdk, mock.patch.object(browser_proposals.http.client, 'HTTPSConnection') as connection:
            self.start();sdk.assert_not_called();connection.assert_not_called()
        account = self.login();state = self.request('/api/state', account=account)[1]
        self.assertEqual(set(account['principal']['roles']), named_access.ROLES)
        self.assertEqual(state['evaluation'], evaluation.metadata())
        self.assertEqual(len(state['users']), 4);self.assertEqual(state['runs'], [])
        page = self.guide.read_text()
        self.assertIn('Recommended for judges', page);self.assertIn('single-account evaluation', page)
        self.assertIn(self.judge['password'], page)
        self.assertNotIn(self.judge['password'], json.dumps(state))
        # Account administration also uses the existing authenticated exact API.
        status, _, _ = self.request('/api/account', {'name':'judge-viewer-fixture', 'password':'Viewer fixture password!', 'roles':['viewer']}, account)
        self.assertEqual(status, 200)

    def test_one_judge_finishes_scripted_recovery_with_separate_handoff_reviews(self):
        run = self.flow()
        self.assertEqual(ops.read(self.root / run)['model_calls'], 0)

    def test_one_judge_finishes_three_mocked_nvidia_roles_without_bypassing_effect_reviews(self):
        connection = browser_proposals_tests.Connection
        connection.calls=[];connection.status=200;connection.echo=False
        with mock.patch.object(browser_proposals.http.client, 'HTTPSConnection', connection):
            run = self.flow(live=True)
        self.assertEqual(len(connection.calls), 3)
        self.assertEqual(ops.read(self.root / run)['model_calls'], 3)
        self.assertEqual(self.server.browser_keys.entries, {})

    def test_upgrade_adds_one_account_without_changing_old_passwords_sessions_or_evidence(self):
        directory = self.root / 'legacy-demo'
        legacy = judge_setup.local_server(directory);legacy.server_close()
        settings = judge_setup.load(directory);root = directory / 'operations_runs'
        before = (directory / 'credentials.json').read_bytes()
        auth = named_access.Access(root)
        try:
            token, _, _ = auth.login('judge-manager', settings['accounts']['judge-manager'])
            rows = auth.db.execute('SELECT id,name,password,roles,active FROM users ORDER BY name').fetchall()
        finally:auth.close()
        marker = root / 'old-evidence.txt';marker.write_text('Keep prior evidence')
        value = evaluation.provision(directory, root, settings)
        self.assertEqual((directory / 'credentials.json').read_bytes(), before)
        auth = named_access.Access(root)
        try:
            self.assertEqual(auth.db.execute('SELECT id,name,password,roles,active FROM users WHERE name!=? ORDER BY name', (evaluation.NAME,)).fetchall(), rows)
            self.assertEqual(auth.session(token)[0]['name'], 'judge-manager')
            self.assertEqual(auth.count(), 4)
        finally:auth.close()
        self.assertEqual(marker.read_text(), 'Keep prior evidence')
        self.assertEqual(evaluation.provision(directory, root, settings), value)

    def test_restart_preserves_revoked_roles_disabled_account_and_generated_password(self):
        self.start();account = self.login();before = (self.settings_dir / evaluation.PRIVATE_DIRECTORY / 'credentials.json').read_bytes()
        auth = named_access.Access(self.root)
        try:
            token, _, admin = auth.login('workspace-admin', self.settings['accounts']['workspace-admin'])
            auth.change(admin, account['principal']['subject'], roles=['viewer'], active=False);auth.logout(token)
        finally:auth.close()
        value = evaluation.provision(self.settings_dir, self.root, self.settings)
        self.assertEqual(value, self.judge)
        self.assertEqual((self.settings_dir / evaluation.PRIVATE_DIRECTORY / 'credentials.json').read_bytes(), before)
        self.assertEqual(self.request('/api/login', {'name':evaluation.NAME, 'password':value['password']})[0], 403)
        auth = named_access.Access(self.root)
        try:
            roles, active = auth.db.execute('SELECT roles,active FROM users WHERE name=?', (evaluation.NAME,)).fetchone()
            self.assertEqual(json.loads(roles), ['viewer']);self.assertEqual(active, 0)
        finally:auth.close()

    def test_separate_accounts_keep_their_existing_permissions(self):
        self.start();manager=self.login('judge-manager');owner=self.login('judge-customer');admin=self.login('workspace-admin')
        self.assertNotIn('owner_C', manager['principal']['roles'])
        self.assertNotIn('manager', owner['principal']['roles'])
        self.assertEqual(set(admin['principal']['roles']), {'admin','viewer'})
        self.assertEqual(self.request('/api/action', {'action':'create'}, admin)[0], 403)
        self.assertEqual(self.request('/api/action', {'action':'create'}, owner)[0], 403)

    def test_unknown_existing_judge_account_is_never_overwritten_or_promoted(self):
        directory=self.root/'legacy';server=judge_setup.local_server(directory);server.server_close();settings=judge_setup.load(directory)
        root=directory/'operations_runs';auth=named_access.Access(root)
        try:
            token,_,admin=auth.login('workspace-admin',settings['accounts']['workspace-admin'])
            user=auth.create('judge-admin','Existing judge password!', ['viewer'], admin);auth.logout(token)
        finally:auth.close()
        with self.assertRaises(ValueError):evaluation.provision(directory,root,settings)
        auth=named_access.Access(root)
        try:self.assertEqual(auth.user(user['subject'])['roles'],['viewer'])
        finally:auth.close()
        self.assertFalse((directory/evaluation.PRIVATE_DIRECTORY/'credentials.json').exists())

    def test_interrupted_creation_reuses_the_original_private_password(self):
        directory=self.root/'legacy';server=judge_setup.local_server(directory);server.server_close();settings=judge_setup.load(directory);root=directory/'operations_runs'
        with mock.patch.object(named_access.Access,'create',side_effect=OSError('Controlled creation interruption')):
            with self.assertRaises(OSError):evaluation.provision(directory,root,settings)
        path=directory/evaluation.PRIVATE_DIRECTORY/'credentials.json';before=path.read_bytes()
        value=evaluation.provision(directory,root,settings)
        self.assertEqual(path.read_bytes(),before)
        auth=named_access.Access(root)
        try:
            token,_,principal=auth.login('judge-admin',value['password']);auth.logout(token)
            self.assertEqual(set(principal['roles']),named_access.ROLES)
        finally:auth.close()

    def test_invalid_private_settings_fail_without_resetting_accounts(self):
        self.start();path=self.settings_dir/evaluation.PRIVATE_DIRECTORY/'credentials.json'
        auth=named_access.Access(self.root)
        try:before=auth.db.execute('SELECT id,password,roles,active FROM users ORDER BY name').fetchall()
        finally:auth.close()
        for raw in ('{}', '{"version":"x","version":"y"}', json.dumps({**self.judge,'password':'invalid\npassword'})):
            path.write_text(raw)
            with self.assertRaises(ValueError):evaluation.provision(self.settings_dir,self.root,self.settings)
        auth=named_access.Access(self.root)
        try:self.assertEqual(auth.db.execute('SELECT id,password,roles,active FROM users ORDER BY name').fetchall(),before)
        finally:auth.close()

    def test_new_account_requires_the_original_administrator_authorization(self):
        directory=self.root/'legacy';server=judge_setup.local_server(directory);server.server_close();settings=judge_setup.load(directory);root=directory/'operations_runs'
        changed={'version':settings['version'],'accounts':{**settings['accounts'],'workspace-admin':'Wrong administrator password!'}}
        with self.assertRaises(named_access.AccessDenied):evaluation.provision(directory,root,changed)
        auth=named_access.Access(root)
        try:self.assertEqual(auth.count(),3)
        finally:auth.close()
        self.assertFalse((directory/evaluation.PRIVATE_DIRECTORY/'credentials.json').exists())

    def test_judge_password_stays_out_of_public_pages_state_and_decompressed_evidence(self):
        run=self.flow();account=self.login()
        public=[self.request('/')[1],json.dumps(self.request('/api/state',account=account)[1])]
        self.assertEqual(self.request('/'+self.guide.name)[0],404)
        raw,_=operations_evidence.build_bytes(self.root/run)
        self.assertEqual(operations_evidence.verify_bytes(raw)['status'],'INTEGRITY_CHECKED')
        with ZipFile(io.BytesIO(raw)) as archive:
            public.extend(archive.read(n).decode() for n in archive.namelist())
        for text in public:
            self.assertNotIn(self.judge['password'],text)
            for password in self.settings['accounts'].values():self.assertNotIn(password,text)
        self.assertIsNotNone(release_check.path_problem(evaluation.PRIVATE_DIRECTORY+'/anything.txt'))
        self.assertIsNotNone(release_check.path_problem('copied/'+evaluation.PRIVATE_DIRECTORY+'/credentials.json'))

    def test_private_welcome_escapes_judge_password_and_regular_servers_do_not_advertise_evaluation(self):
        directory=self.root/'legacy';server=judge_setup.local_server(directory)
        try:
            self.assertIsNone(server.evaluation)
            settings=judge_setup.load(directory)
            text=demo.welcome_html(settings,server.server_port,{'version':evaluation.VERSION,'name':evaluation.NAME,'password':'<script>secret fixture</script>'})
            self.assertIn('&lt;script&gt;',text);self.assertNotIn('<script>',text)
        finally:server.server_close()


if __name__ == '__main__':unittest.main()
