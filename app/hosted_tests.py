"""Hosted API checks use loopback transport with explicit proxy headers, not TLS.

All account passwords and provider clients in this module are test fixtures.
No deployed-host, actual TLS or live-inference result is claimed here.
"""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import hosted_server as hosted
import named_access
import operations_server as web
import operations_workspace as ops

PASSWORDS = {'AR_ADMIN_PASSWORD':'Fixture administrator password!',
             'AR_MANAGER_PASSWORD':'Fixture manager password!',
             'AR_CUSTOMER_PASSWORD':'Fixture customer password!'}


class HostedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hosted evaluation ')
        self.root = Path(self.temp.name)
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.shutdown();self.server.server_close();self.thread.join(5)
        for path in self.root.iterdir():
            if path.is_dir() and not path.name.startswith('.'):
                try:
                    with ops.opened(path) as run:run.stop()
                except (ValueError,OSError):pass
        self.temp.cleanup()

    def start(self):
        hosted.provision(self.root, PASSWORDS)
        self.server = web.Server(self.root, https_origin='https://rehearsal.example')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(self, path, body=None, account=None, headers=None):
        defaults = {'Host':'rehearsal.example'}
        if body is not None:
            defaults.update({'Origin':'https://rehearsal.example','Content-Type':'application/json'})
        if account:
            defaults.update({'Cookie':account['cookie'],'X-CSRF-Token':account['csrf']})
        defaults.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=40)
        try:
            connection.request('POST' if body is not None else 'GET', path,
                               json.dumps(body) if body is not None else None, defaults)
            response = connection.getresponse();data=response.read()
            value=json.loads(data) if response.headers.get('Content-Type','').startswith('application/json') else data.decode()
            return response.status, value, dict(response.headers)
        finally:connection.close()

    def login(self, name):
        status, value, headers = self.request('/api/login', {'name':name,'password':PASSWORDS[hosted.ACCOUNTS[name][0]]})
        self.assertEqual(status,200,value)
        return {'csrf':value['csrf'],'cookie':headers['Set-Cookie'].split(';')[0],'principal':value['principal']}

    def test_only_explicit_canonical_https_origins_are_accepted(self):
        for value in ('https://rehearsal.example','https://rehearsal.example:8443'):
            self.assertEqual(web.public_origin(value),value)
        for value in (None,'','http://rehearsal.example','https://rehearsal.example/',
                      'https://user:secret@rehearsal.example','https://rehearsal.example/path',
                      'https://rehearsal.example?x=1','https://rehearsal.example#x',
                      'https://REHEARSAL.example','https://bad..example',
                      'https://bad-.example','https://rehearsal.example:0',
                      'https://rehearsal.example:443','https://rehearsal.example:99999'):
            with self.subTest(value=value),self.assertRaises(ValueError):web.public_origin(value)

    def test_network_binding_and_hosted_first_account_takeover_are_denied(self):
        with self.assertRaises(ValueError):web.Server(self.root,bind_host='0.0.0.0')
        with self.assertRaises(ValueError):web.Server(self.root,https_origin='https://rehearsal.example')
        auth=named_access.Access(self.root)
        try:self.assertEqual(auth.count(),0)
        finally:auth.close()

    def test_provisioned_judges_have_complete_distinct_workflow_roles(self):
        self.start();manager=self.login('judge-manager');owner=self.login('judge-customer')
        self.assertIn('manager',manager['principal']['roles'])
        self.assertNotIn('owner_C',manager['principal']['roles'])
        self.assertNotIn('admin',manager['principal']['roles'])
        self.assertEqual(set(owner['principal']['roles']),{'owner_C','viewer'})
        self.assertNotEqual(manager['principal']['subject'],owner['principal']['subject'])

    def test_restart_preserves_accounts_sessions_passwords_and_revoked_roles(self):
        self.start();manager=self.login('judge-manager');admin=self.login('workspace-admin')
        auth=named_access.Access(self.root)
        try:
            auth.change(admin['principal'],manager['principal']['subject'],['viewer'])
            before=auth.db.execute('SELECT id,password,roles FROM users ORDER BY name').fetchall()
            self.assertEqual(hosted.provision(self.root,{})['status'],'EXISTING_ACCOUNTS_PRESERVED')
            self.assertEqual(auth.db.execute('SELECT id,password,roles FROM users ORDER BY name').fetchall(),before)
            self.assertEqual(auth.session(manager['cookie'].split('=',1)[1])[0]['roles'],['viewer'])
        finally:auth.close()

    def test_missing_weak_or_duplicate_secrets_fail_before_any_account_is_added(self):
        for env in ({},{**PASSWORDS,'AR_CUSTOMER_PASSWORD':'short'},
                    {**PASSWORDS,'AR_CUSTOMER_PASSWORD':PASSWORDS['AR_MANAGER_PASSWORD']}):
            with self.assertRaises(ValueError):hosted.provision(self.root,env)
            auth=named_access.Access(self.root)
            try:self.assertEqual(auth.count(),0)
            finally:auth.close()

    def test_interrupted_provision_needs_admin_auth_and_does_not_reset_earlier_account(self):
        auth=named_access.Access(self.root)
        try:admin=auth.create('workspace-admin',PASSWORDS['AR_ADMIN_PASSWORD'],['admin','viewer'])
        finally:auth.close()
        with self.assertRaises(named_access.AccessDenied):
            hosted.provision(self.root,{**PASSWORDS,'AR_ADMIN_PASSWORD':'Wrong fixture administrator password!'})
        hosted.provision(self.root,PASSWORDS)
        auth=named_access.Access(self.root)
        try:self.assertEqual(auth.user(admin['subject'])['name'],'workspace-admin');self.assertEqual(auth.count(),3)
        finally:auth.close()

    def test_public_page_never_exposes_signup_or_bootstrap_and_health_is_minimal(self):
        self.start()
        status,page,_=self.request('/')
        self.assertEqual(status,200);self.assertNotIn(self.server.bootstrap,page)
        self.assertIn("const HOSTED='true'",page)
        self.assertEqual(self.request('/api/public-status')[1]['setup_required'],False)
        self.assertEqual(self.request('/api/setup',{'name':'intruder','password':'Intruder fixture password!','bootstrap':self.server.bootstrap})[0],403)
        self.assertEqual(self.request('/api/state')[0],401)
        status,health,_=self.request('/healthz',headers={'Host':'internal-health-probe'})
        self.assertEqual((status,health),(200,{'status':'ok'}))

    def test_https_session_cookie_and_logout_are_secure_and_revocable(self):
        self.start()
        status,value,headers=self.request('/api/login',{'name':'judge-manager','password':PASSWORDS['AR_MANAGER_PASSWORD']})
        cookie=headers['Set-Cookie']
        for flag in ('HttpOnly','SameSite=Strict','Path=/','Max-Age=1800','Secure'):self.assertIn(flag,cookie)
        account={'cookie':cookie.split(';')[0],'csrf':value['csrf']}
        self.assertEqual(self.request('/api/state',account=account)[0],200)
        status,_,headers=self.request('/api/logout',{},account)
        self.assertEqual(status,200);self.assertIn('Secure',headers['Set-Cookie']);self.assertIn('Max-Age=0',headers['Set-Cookie'])
        self.assertEqual(self.request('/api/state',account=account)[0],401)

    def test_wrong_hosts_origins_forwarded_spoofing_and_missing_csrf_have_no_effect(self):
        self.start();manager=self.login('judge-manager')
        for headers in ({'Host':'attacker.example'}, {'Origin':'https://attacker.example'},
                        {'Origin':'http://rehearsal.example'}, {'Origin':''},
                        {'Host':'127.0.0.1','X-Forwarded-Host':'rehearsal.example','X-Forwarded-Proto':'https'},
                        {'X-CSRF-Token':'wrong'}):
            self.assertEqual(self.request('/api/action',{'action':'create'},manager,headers)[0],403)
        self.assertEqual(self.request('/api/state',account=manager)[1]['runs'],[])

    def test_duplicate_host_or_origin_headers_are_denied(self):
        self.start()
        for duplicate in ('Host','Origin'):
            connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
            try:
                connection.putrequest('POST','/api/login',skip_host=True)
                connection.putheader('Host','rehearsal.example');connection.putheader('Origin','https://rehearsal.example')
                connection.putheader(duplicate,'rehearsal.example' if duplicate=='Host' else 'https://rehearsal.example')
                connection.putheader('Content-Length','2');connection.putheader('Content-Type','application/json');connection.endheaders(b'{}')
                response=connection.getresponse();self.assertEqual(response.status,403);response.read()
            finally:connection.close()

    def test_both_judge_accounts_finish_recovery_and_preserve_customer_work(self):
        self.start();manager=self.login('judge-manager');owner=self.login('judge-customer')
        status,created,_=self.request('/api/action',{'action':'create'},manager)
        self.assertEqual(status,200,created);run=created['run']
        def approve(kind,account,**extra):
            status,review,_=self.request('/api/review',{'run':run,'kind':kind,**extra},account)
            self.assertEqual(status,200,review)
            body={'action':'approve-review','run':run,'kind':kind,'digest':review['digest'],'phrase':review['phrase'],**extra}
            status,value,_=self.request('/api/action',body,account)
            self.assertEqual(status,200,value)
        approve('batch',manager);approve('fault-candidate',manager)
        auth=named_access.Access(self.root)
        try:
            with ops.opened(self.root/run,auth=auth) as mission:mission.observe();mission.observe();mission.advance()
        finally:auth.close()
        approve('recovery',manager)
        self.assertEqual(self.request('/api/review',{'run':run,'kind':'failover-owner_C'},manager)[0],403)
        approve('failover-manager',manager)
        saved=ops.read(self.root/run)
        self.assertEqual(saved['service']['actual_at_outcome']['candidate_percent'],20)
        approve('failover-owner_C',owner)
        saved=ops.read(self.root/run)
        self.assertEqual(saved['service']['policy_snapshot']['state']['deployments']['candidate-v2']['dependencies'][0]['id'],'C-mission-job')
        approve('job',owner,job='C-mission-job')
        self.assertEqual(ops.read(self.root/run)['service']['metrics']['committed'],40)
        self.assertEqual(ops.verify(self.root/run)['status'],'CONSISTENT')
        self.assertEqual(self.request('/api/action',{'action':'stop','run':run},manager)[0],200)
        self.assertTrue(ops.read(self.root/run)['teardown']['all_workers_stopped'])

    def test_server_configuration_makes_no_model_call_and_binds_the_platform_port(self):
        env={**PASSWORDS,'AR_PUBLIC_ORIGIN':'https://rehearsal.example','AR_STATE_ROOT':str(self.root),'PORT':'12345'}
        with mock.patch.object(hosted.operations_server,'Server') as factory,mock.patch.object(ops,'RoleClient') as role:
            hosted.configured_server(env)
            role.assert_not_called()
            self.assertEqual(factory.call_args.kwargs['port'],12345)
            self.assertEqual(factory.call_args.kwargs['bind_host'],'0.0.0.0')
            self.assertIsNone(factory.call_args.kwargs['proposal_providers'])

    def test_live_configuration_requires_key_and_only_constructs_three_clients(self):
        env={**PASSWORDS,'AR_PUBLIC_ORIGIN':'https://rehearsal.example','AR_STATE_ROOT':str(self.root),'AR_LIVE_PROPOSALS':'1'}
        with self.assertRaises(ValueError):hosted.configured_server(env)
        with mock.patch.object(hosted.operations_server,'Server'),mock.patch.object(ops,'RoleClient') as role:
            hosted.configured_server({**env,'NEBIUS_API_KEY':'fixture-only-value'})
            self.assertEqual(role.call_count,3)
            for client in role.return_value.ask.call_args_list:self.fail('No proposal may be requested at startup.')

    def test_hosted_judges_complete_three_fake_live_roles_without_supplying_any_key(self):
        # Real account/API/process flow; providers are explicitly fake and spend
        # zero credits. This is not a public TLS deployment or genuine SDK call.
        import time
        from operations_proposals_tests import FakeLive
        clients={role:FakeLive(role) for role in ops.ROLES}
        environment={**PASSWORDS,'AR_PUBLIC_ORIGIN':'https://rehearsal.example',
                     'AR_STATE_ROOT':str(self.root),'PORT':'0','AR_LIVE_PROPOSALS':'1',
                     'NEBIUS_API_KEY':'private-owner-fixture'}
        # configured_server validates its public port. Reserve an ordinary local
        # listener for the test while retaining the actual hosted configuration.
        original=web.Server
        def listener(*args,**kwargs):
            kwargs['port']=0
            return original(*args,**kwargs)
        environment['PORT']='10000'
        with mock.patch.object(ops,'RoleClient',side_effect=lambda role,*_:clients[role]) as constructor, mock.patch.object(hosted.operations_server,'Server',side_effect=listener):
            self.server=hosted.configured_server(environment)
        self.assertEqual(constructor.call_count,3)
        self.assertTrue(all(not client.calls for client in clients.values()))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        manager=self.login('judge-manager');customer=self.login('judge-customer')
        status,state,_=self.request('/api/state',account=manager)
        self.assertEqual(status,200);self.assertTrue(state['proposal_access']['available'])
        self.assertFalse(state['proposal_access']['judge_key_required'])
        self.assertNotIn('private-owner-fixture',json.dumps(state))
        status,value,_=self.request('/api/action',{'action':'create','proposal_mode':'live','duration_minutes':60},manager)
        self.assertEqual(status,200,value);run=value['run']
        self.assertTrue(all(not client.calls for client in clients.values()))
        def approve(kind,account=manager,**extra):
            status,review,_=self.request('/api/review',{'run':run,'kind':kind,**extra},account)
            self.assertEqual(status,200,review)
            status,value,_=self.request('/api/action',{'action':'approve-review','run':run,'kind':kind,
                'digest':review['digest'],'phrase':review['phrase'],**extra},account)
            self.assertIn(status,(200,202),value)
            if kind.startswith('proposal-'):
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    job=self.server.proposals.snapshot()[run]
                    if job['status'] not in {'QUEUED','RUNNING'}:break
                    time.sleep(.02)
                self.assertEqual(job['status'],'FINISHED',job)
        approve('proposal-factory');approve('proposal-governance');approve('batch');approve('fault-candidate')
        with ops.opened(self.root/run) as mission:mission.observe();mission.observe()
        approve('proposal-recovery');approve('recovery');approve('failover-manager');approve('failover-owner_C',customer)
        approve('job',customer,job='C-mission-job')
        status,value,_=self.request('/api/action',{'action':'stop','run':run},manager)
        self.assertEqual(status,200,value);saved=ops.read(self.root/run)
        self.assertEqual(saved['phase'],'RECOVERED_SERVICE_ACTIVE');self.assertTrue(saved['teardown']['all_workers_stopped'])
        self.assertEqual([len(clients[role].calls) for role in ops.ROLES],[1,1,1])
        self.assertEqual(ops.verify(self.root/run)['status'],'CONSISTENT')

    def test_unsafe_configuration_is_rejected_before_provision_or_listener(self):
        base={**PASSWORDS,'AR_PUBLIC_ORIGIN':'https://rehearsal.example','AR_STATE_ROOT':str(self.root)}
        for extra in ({'PORT':'not-a-port'},{'PORT':'0'},{'AR_MAX_OUTPUT_TOKENS':'10001'},
                      {'AR_LIVE_PROPOSALS':'yes'},{'AR_STATE_ROOT':str(Path(hosted.__file__).resolve().parent)}):
            with mock.patch.object(hosted,'provision') as provision,mock.patch.object(hosted.operations_server,'Server') as server:
                with self.assertRaises(ValueError):hosted.configured_server({**base,**extra})
                provision.assert_not_called();server.assert_not_called()
