"""Prepared credentials and real local judge HTTP flow; all provider clients are fixtures."""
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import hosted_tests
import judge_setup as setup
import named_access
import operations_evidence
import operations_outcomes
import operations_workspace as ops
import release_check
import restart_checker


class JudgeSetupTests(unittest.TestCase):
    setUp = hosted_tests.HostedTests.setUp
    tearDown = hosted_tests.HostedTests.tearDown

    def start(self):
        self.settings_dir=self.root/'judge_access_private'
        self.server=setup.local_server(self.settings_dir)
        self.settings=setup.load(self.settings_dir)
        self.root=self.server.root
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def request(self,path,body=None,account=None,headers=None):
        host='127.0.0.1:'+str(self.server.server_port)
        default={'Host':host}
        if body is not None:default.update({'Origin':'http://'+host,'Content-Type':'application/json'})
        if account:default.update({'Cookie':account['cookie'],'X-CSRF-Token':account['csrf']})
        default.update(headers or {})
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=40)
        try:
            connection.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,default)
            response=connection.getresponse();raw=response.read()
            data=json.loads(raw) if response.headers.get('Content-Type','').startswith('application/json') else raw.decode()
            return response.status,data,dict(response.headers)
        finally:connection.close()

    def login(self,name):
        status,data,headers=self.request('/api/login',{'name':name,'password':self.settings['accounts'][name]})
        self.assertEqual(status,200,data)
        return {'csrf':data['csrf'],'cookie':headers['Set-Cookie'].split(';')[0],'principal':data['principal']}

    def test_prepare_generates_distinct_credentials_and_never_overwrites(self):
        directory=self.root/'judge_access_private';settings=setup.prepare(directory)
        self.assertEqual(set(settings['accounts']),{'workspace-admin','judge-manager','judge-customer'})
        passwords=list(settings['accounts'].values());self.assertEqual(len(set(passwords)),3)
        self.assertTrue(all(len(p)>=40 for p in passwords))
        before={p.name:p.read_bytes() for p in directory.iterdir()}
        with self.assertRaises(FileExistsError):setup.prepare(directory)
        self.assertEqual(before,{p.name:p.read_bytes() for p in directory.iterdir()})
        if os.name!='nt':
            self.assertEqual(directory.stat().st_mode&0o777,0o700)
            self.assertTrue(all(p.stat().st_mode&0o777==0o600 for p in directory.iterdir()))

    def test_private_loader_rejects_extra_missing_duplicate_and_unsafe_fields(self):
        directory=self.root/'private';settings=setup.prepare(directory);path=directory/'credentials.json'
        invalid=[{**settings,'NEBIUS_API_KEY':'fixture'}, {'version':setup.VERSION,'accounts':{}},
                 {'version':setup.VERSION,'accounts':{name:'same password all' for name in setup.hosted_server.ACCOUNTS}},
                 {'version':setup.VERSION,'accounts':{**settings['accounts'],'judge-manager':'bad\nsecret secret'}}]
        for value in invalid:
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):setup.load(directory)
        path.write_text('{"version":"a","version":"b","accounts":{}}')
        with self.assertRaises(ValueError):setup.load(directory)

    def test_hosting_export_reuses_credentials_validates_origin_and_excludes_provider_key(self):
        directory=self.root/'private';settings=setup.prepare(directory)
        with self.assertRaises(ValueError):setup.export_hosting(directory,'http://insecure.example')
        target=setup.export_hosting(directory,'https://rehearsal.example')
        values=dict(line.split('=',1) for line in (target/'hosting.env').read_text().splitlines())
        self.assertEqual(values['AR_PUBLIC_ORIGIN'],'https://rehearsal.example')
        self.assertEqual(values['AR_LIVE_PROPOSALS'],'1');self.assertNotIn('NEBIUS_API_KEY',values)
        for name,password in settings['accounts'].items():self.assertEqual(values[setup.hosted_server.ACCOUNTS[name][0]],password)
        self.assertIn('https://rehearsal.example',(target/'DEVPOST_TESTING_PRIVATE.txt').read_text())
        second=setup.export_hosting(directory,'https://rehearsal.example');self.assertNotEqual(target,second)
        self.assertEqual(setup.load(directory),settings)

    def test_cli_setup_and_host_export_do_not_print_secrets(self):
        directory=self.root/'private';output=io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(setup.main(['prepare','--directory',str(directory)]),0)
            self.assertEqual(setup.main(['hosting','--directory',str(directory),'--origin','https://rehearsal.example']),0)
        for password in setup.load(directory)['accounts'].values():self.assertNotIn(password,output.getvalue())

    def test_scripted_launcher_makes_no_provider_request_and_requires_password(self):
        with mock.patch.object(ops,'RoleClient') as provider:self.start();provider.assert_not_called()
        self.assertEqual(self.request('/api/state')[0],401)
        self.assertEqual(self.request('/api/public-status')[1]['setup_required'],False)
        self.assertFalse(self.request('/api/public-status')[1]['live_proposals_enabled'])
        self.assertEqual(self.request('/api/login',{'name':'judge-manager','password':''})[0],403)
        self.assertEqual(self.request('/api/state',account=self.login('judge-manager'))[1]['runs'],[])

    def test_all_generated_accounts_have_distinct_appropriate_roles(self):
        self.start();manager=self.login('judge-manager');customer=self.login('judge-customer');admin=self.login('workspace-admin')
        self.assertNotEqual(manager['principal']['subject'],customer['principal']['subject'])
        self.assertNotIn('owner_C',manager['principal']['roles']);self.assertNotIn('admin',manager['principal']['roles'])
        self.assertEqual(set(customer['principal']['roles']),{'owner_C','viewer'})
        self.assertEqual(set(admin['principal']['roles']),{'admin','viewer'})
        self.assertEqual(self.request('/api/account',{'name':'intruder','password':'Fixture long password!','roles':['admin']},manager)[0],403)

    def test_existing_installation_and_generated_passwords_survive_restart(self):
        marker=self.root/'existing-installation.txt';marker.write_text('Original local workspace remains untouched')
        self.start();manager=self.login('judge-manager')
        before=(self.settings_dir/'credentials.json').read_bytes()
        self.server.shutdown();self.server.server_close();self.thread.join(5)
        self.server=setup.local_server(self.settings_dir)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.assertEqual(self.request('/api/state',account=manager)[0],200)
        self.assertEqual((self.settings_dir/'credentials.json').read_bytes(),before)
        self.assertEqual(marker.read_text(),'Original local workspace remains untouched')

    def test_bad_settings_cannot_reset_accounts_or_open_listener(self):
        self.start();before=(self.root/'.control_access/accounts.sqlite3').read_bytes()
        path=self.settings_dir/'credentials.json';path.write_text('invalid private data')
        with mock.patch.object(setup.operations_server,'Server') as listener:
            with self.assertRaises(ValueError):setup.local_server(self.settings_dir)
            listener.assert_not_called()
        self.assertEqual((self.root/'.control_access/accounts.sqlite3').read_bytes(),before)

    def test_live_startup_only_constructs_clients_and_waits_for_separate_reviews(self):
        directory=self.root/'private'
        with mock.patch.object(ops,'RoleClient') as provider,mock.patch.object(setup.operations_server,'Server') as listener:
            setup.local_server(directory,live=True,key='fixture-only-key')
            self.assertEqual(provider.call_count,3)
            provider.return_value.ask.assert_not_called()
            self.assertEqual(set(listener.call_args.kwargs['proposal_providers']),set(ops.ROLES))
            self.assertEqual(provider.call_args.args[2:],(8192,True))

    def test_no_redirected_or_echoed_key_prompt(self):
        with mock.patch.dict(os.environ,{},clear=True),mock.patch.object(setup.terminal_guard,'interactive_input',return_value=False),mock.patch.object(setup,'getpass') as prompt:
            with self.assertRaises(ValueError):setup.local_server(self.root/'private',live=True)
            prompt.assert_not_called()

    def test_invalid_launch_settings_fail_before_creating_private_data(self):
        for extra in ({'port':-1},{'port':70000},{'output_tokens':10001},{'output_tokens':True}):
            with self.assertRaises(ValueError):setup.local_server(self.root/'private',**extra)
        self.assertFalse((self.root/'private').exists())

    def test_password_judges_complete_recovery_and_export_without_credentials(self):
        hosted_tests.HostedTests.test_both_judge_accounts_finish_recovery_and_preserve_customer_work(self)
        path=next(p for p in self.root.iterdir() if p.is_dir() and not p.name.startswith('.'))
        report=operations_outcomes.build(path)
        self.assertEqual(report['comparison_basis']['distinct_handoff_accounts'],2)
        self.assertEqual(set(report['comparison_basis']['handoff_approval_modes'].values()),{'NAMED_LOCAL_ACCOUNT'})
        raw,_=operations_evidence.build_bytes(path)
        self.assertEqual(operations_evidence.verify_bytes(raw)['status'],'INTEGRITY_CHECKED')
        from zipfile import ZipFile
        with ZipFile(io.BytesIO(raw)) as archive:
            for name in archive.namelist():
                for password in self.settings['accounts'].values():self.assertNotIn(password.encode(),archive.read(name))

    def test_generated_passwords_cannot_enter_publication_under_another_filename(self):
        settings=setup.prepare(self.root/'private')
        for password in settings['accounts'].values():
            result=release_check.check_entries([('notes.md',('Password: '+password).encode(),'100644')])
            self.assertEqual(len(result),1);self.assertNotIn(password,json.dumps(result))
        for name in ('judge_access_private/any.txt','credentials.json','JUDGE_CREDENTIALS_PRIVATE.txt','hosting.env','DEVPOST_TESTING_PRIVATE.txt'):
            self.assertIsNotNone(release_check.path_problem(name))

    def test_owned_workers_do_not_inherit_hosted_account_passwords(self):
        values={key:'fixture-only' for key in ('AR_ADMIN_PASSWORD','AR_MANAGER_PASSWORD','AR_CUSTOMER_PASSWORD','NEBIUS_API_KEY','OPENAI_API_KEY')}
        with mock.patch.dict(os.environ,values):
            setup.hosted_server.forget_account_secrets()
            self.assertTrue(set(values).isdisjoint(restart_checker.child_env()))


if __name__=='__main__':unittest.main()
