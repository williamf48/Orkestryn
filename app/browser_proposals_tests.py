"""Explicit browser-key inputs, real local API/workers and mocked Nebius HTTP."""
import io
import json
from pathlib import Path
import tempfile
import time
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import build_opener,HTTPCookieProcessor
from http.cookiejar import CookieJar
from zipfile import ZipFile

import browser_proposals as keys
import named_access
import operations_evidence
import operations_workspace as ops
import operations_server_tests as server_tests
import operations_proposals_tests as proposal_tests

KEY='fixture-nebius-key-123456'


class Connection:
    calls=[];status=200;echo=False
    def __init__(self,*args,**kwargs):self.closed=False;self.__class__.calls.append((args,kwargs,self))
    def request(self,*args,**kwargs):
        self.sent=(args,kwargs);body=json.loads(kwargs['body'])
        role=next(r for r in ops.ROLES if body['messages'][0]['content']==ops.PROMPTS[r])
        content,_=ops.ScriptedRole(role).ask(json.loads(body['messages'][1]['content']))
        if self.echo:
            value=json.loads(content);value['reason']=KEY;content=json.dumps(value)
        self.value={'model':ops.factory.inference.MODEL,'id':KEY if self.echo else 'MOCK-NOT-LIVE',
            'choices':[{'finish_reason':'stop','message':{'content':content,'refusal':None}}],
            'usage':{'prompt_tokens':10,'completion_tokens':20,'total_tokens':30}}
    def getresponse(self):return self
    def read(self,limit):return json.dumps(self.value).encode()[:limit]
    def close(self):self.closed=True


class BrowserKeyTests(unittest.TestCase):
    get=server_tests.ServerTests.get;post=server_tests.ServerTests.post;setup=server_tests.ServerTests.setup
    setUp=server_tests.ServerTests.setUp;tearDown=server_tests.ServerTests.tearDown
    review=proposal_tests.ProposalTests.review;approve=proposal_tests.ProposalTests.approve;effect=proposal_tests.ProposalTests.effect;one=proposal_tests.ProposalTests.one

    def create(self,key=KEY):
        return self.post('/api/action',{'action':'create','proposal_mode':'live','nebius_api_key':key})['run']

    def settled(self,run):
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            value=self.server.proposal_jobs().get(run)
            if value and value['status'] not in {'QUEUED','RUNNING'}:return value
            time.sleep(.02)
        self.fail('The mocked proposal did not settle.')

    def test_key_connection_creates_live_draft_without_network_or_sdk(self):
        self.setup()
        with mock.patch.object(keys.http.client,'HTTPSConnection') as network:
            run=self.create();network.assert_not_called()
        state=self.get();saved=ops.read(self.root/run)
        self.assertEqual(saved['phase'],'DRAFT');self.assertEqual(saved['model_calls'],0)
        self.assertTrue(state['proposal_credentials'][run]);self.assertTrue(state['proposal_access']['browser_key_entry'])
        self.assertFalse(state['live_proposals_enabled']);self.assertNotIn(KEY,json.dumps(state))

    def test_only_nvidia_accepts_the_exact_new_key_field(self):
        self.setup()
        for body in ({'proposal_mode':'scripted','nebius_api_key':KEY},
                     {'proposal_mode':'live','api_key':KEY},{'proposal_mode':'live','NEBIUS_API_KEY':KEY}):
            with self.assertRaises(HTTPError) as result:self.post('/api/action',{'action':'create',**body})
            self.assertEqual(result.exception.code,400)
        self.assertEqual(self.get()['runs'],[])

    def test_invalid_key_values_fail_before_any_workflow_is_created(self):
        self.setup()
        for value in (None,123,{},'', 'short', 'spaces inside key value', 'x'*4097, 'x'*20+'\nsecret', 'é'*30):
            with self.subTest(value_type=type(value).__name__),self.assertRaises(HTTPError):self.create(value)
        self.assertEqual(self.get()['runs'],[]);self.assertEqual(self.server.browser_keys.entries,{})

    def test_trusted_server_can_disable_browser_key_entry(self):
        self.setup();self.server.allow_browser_keys=False
        with self.assertRaises(HTTPError):self.create()
        self.assertFalse(self.get()['proposal_access']['browser_key_entry']);self.assertEqual(self.get()['runs'],[])

    def test_viewer_cannot_supply_a_key_or_reconnect(self):
        self.setup();run=self.create()
        self.post('/api/account',{'name':'reader','password':'Long test password!','roles':['viewer']})
        client=build_opener(HTTPCookieProcessor(CookieJar()))
        csrf=self.post('/api/login',{'name':'reader','password':'Long test password!'},client,csrf='')['csrf']
        for endpoint,body in [('/api/action',{'action':'create','proposal_mode':'live','nebius_api_key':KEY}),
                              ('/api/proposal-access',{'run':run,'nebius_api_key':KEY})]:
            with self.assertRaises(HTTPError) as result:self.post(endpoint,body,client,csrf=csrf)
            self.assertEqual(result.exception.code,403)

    def test_another_manager_cannot_use_or_rebind_the_entered_key(self):
        self.setup();run=self.create()
        self.post('/api/account',{'name':'other-manager','password':'Long test password!','roles':['manager','viewer']})
        client=build_opener(HTTPCookieProcessor(CookieJar()))
        csrf=self.post('/api/login',{'name':'other-manager','password':'Long test password!'},client,csrf='')['csrf']
        for endpoint,body in [('/api/review',{'run':run,'kind':'proposal-factory'}),
                              ('/api/proposal-access',{'run':run,'nebius_api_key':KEY})]:
            with self.assertRaises(HTTPError) as result:self.post(endpoint,body,client,csrf=csrf)
            self.assertEqual(result.exception.code,403)
        self.assertEqual(ops.read(self.root/run)['calls'],[])

    def test_same_account_new_browser_needs_explicit_reconnection(self):
        self.setup();run=self.create();client=build_opener(HTTPCookieProcessor(CookieJar()))
        csrf=self.post('/api/login',{'name':'william','password':'Long local password!'},client,csrf='')['csrf']
        with self.assertRaises(HTTPError):self.post('/api/review',{'run':run,'kind':'proposal-factory'},client,csrf=csrf)
        with mock.patch.object(keys.http.client,'HTTPSConnection') as network:
            self.post('/api/proposal-access',{'run':run,'nebius_api_key':KEY},client,csrf=csrf)
            network.assert_not_called()
        self.assertEqual(ops.read(self.root/run)['calls'],[])

    def test_logout_discards_key_and_same_account_can_reconnect_saved_draft(self):
        self.setup();run=self.create();self.post('/api/logout',{})
        self.assertEqual(self.server.browser_keys.entries,{})
        logged=self.post('/api/login',{'name':'william','password':'Long local password!'},csrf='');self.csrf=logged['csrf']
        self.assertFalse(self.get()['proposal_credentials'][run])
        with self.assertRaises(HTTPError):self.review(run,'factory')
        self.post('/api/proposal-access',{'run':run,'nebius_api_key':KEY})
        self.assertTrue(self.get()['proposal_credentials'][run]);self.assertEqual(ops.read(self.root/run)['calls'],[])

    def test_session_expiry_reaps_the_key(self):
        self.setup();self.create();auth=named_access.Access(self.root)
        try:auth.db.execute('UPDATE sessions SET expires=0')
        finally:auth.close()
        self.server.browser_keys.reap();self.assertEqual(self.server.browser_keys.entries,{})

    def test_role_revocation_reaps_keys_and_blocks_pending_paid_work(self):
        principal=self.setup();run=self.create();auth=named_access.Access(self.root)
        try:auth.db.execute('UPDATE users SET roles=? WHERE id=?',(json.dumps(['viewer']),principal['subject']))
        finally:auth.close()
        self.server.browser_keys.reap();self.assertEqual(self.server.browser_keys.entries,{})
        with self.assertRaises(HTTPError):self.review(run,'factory')
        self.assertEqual(ops.read(self.root/run)['calls'],[])

    def test_stopping_the_run_discards_its_session_key(self):
        self.setup();run=self.create();self.post('/api/action',{'action':'stop','run':run})
        self.assertEqual(self.server.browser_keys.entries,{})

    def test_server_shutdown_discards_its_session_keys(self):
        self.setup();self.create();self.server.browser_keys.close()
        self.assertEqual(self.server.browser_keys.entries,{})

    def test_paid_attempt_uses_fixed_endpoint_prompt_and_frozen_settings_once(self):
        self.setup();run=self.create();Connection.calls=[];Connection.status=200;Connection.echo=False
        with mock.patch.object(keys.http.client,'HTTPSConnection',Connection):self.one(run,'factory')
        self.assertEqual(len(Connection.calls),1);args,kwargs,connection=Connection.calls[0]
        self.assertEqual(args,('api.tokenfactory.nebius.com',));self.assertEqual(kwargs['timeout'],120)
        sent,options=connection.sent;self.assertEqual(sent,('POST','/v1/chat/completions'))
        body=json.loads(options['body']);self.assertEqual(body['model'],ops.factory.inference.MODEL)
        self.assertEqual(body['messages'][0]['content'],ops.PROMPTS['factory']);self.assertEqual(body['max_tokens'],8192)
        self.assertEqual(body['chat_template_kwargs'],{'enable_thinking':True});self.assertTrue(connection.closed)
        self.assertNotIn(KEY,json.dumps(self.get()))

    def test_api_key_never_enters_saved_files_or_export_even_if_model_echoes_it(self):
        self.setup()
        # Test a long key too: redact before shortening provider identifiers,
        # otherwise a saved identifier could expose the beginning of a key.
        for supplied in (KEY,'long-session-key-'+'x'*240):
            with mock.patch(__name__+'.KEY',supplied):
                run=self.create(supplied);Connection.calls=[];Connection.status=200;Connection.echo=True
                try:
                    with mock.patch.object(keys.http.client,'HTTPSConnection',Connection):self.one(run,'factory')
                finally:Connection.echo=False
                raw,_=operations_evidence.build_bytes(self.root/run);prefix=supplied[:128].encode()
                with ZipFile(io.BytesIO(raw)) as archive:
                    for name in archive.namelist():self.assertNotIn(prefix,archive.read(name))
                for path in self.root.rglob('*'):
                    if path.is_file():self.assertNotIn(prefix,path.read_bytes(),path.name)
                self.post('/api/action',{'action':'stop','run':run})

    def test_failed_or_redirected_request_stops_without_automatic_retry(self):
        self.setup();run=self.create();Connection.calls=[];Connection.status=302;Connection.echo=False
        try:
            with mock.patch.object(keys.http.client,'HTTPSConnection',Connection):
                p=self.review(run,'factory');self.approve(run,'factory',p);job=self.settled(run)
                self.assertEqual(job['status'],'FAILED')
                with self.assertRaises(HTTPError):self.review(run,'factory')
            self.assertEqual(len(Connection.calls),1);self.assertNotIn(KEY,json.dumps(self.get()))
        finally:Connection.status=200

    def test_session_key_never_falls_back_to_a_configured_owner_provider(self):
        self.setup();run=self.create();self.server.browser_keys.forget(run=run)
        owner=mock.Mock();owner.enabled=True;self.server.proposals=owner
        with self.assertRaises(HTTPError):self.review(run,'factory')
        owner.submit.assert_not_called();self.assertEqual(ops.read(self.root/run)['calls'],[])
        self.server.proposals=__import__('operations_proposals').Jobs(self.root)

    def test_key_connection_does_not_authorize_a_paid_attempt(self):
        self.setup();run=self.create()
        with mock.patch.object(keys.http.client,'HTTPSConnection') as network:
            self.post('/api/proposal-access',{'run':run,'nebius_api_key':KEY})
            self.review(run,'factory');network.assert_not_called()
        self.assertEqual(ops.read(self.root/run)['calls'],[])

    def test_stale_csrf_cannot_connect_or_create_with_a_key(self):
        self.setup();run=self.create()
        with self.assertRaises(HTTPError) as result:self.post('/api/proposal-access',{'run':run,'nebius_api_key':KEY},csrf='wrong')
        self.assertEqual(result.exception.code,403)

    def test_key_lifetime_never_exceeds_the_original_run_deadline(self):
        self.setup()
        run=self.post('/api/action',{'action':'create','proposal_mode':'live','duration_minutes':15,'nebius_api_key':KEY})['run']
        entry=next(iter(self.server.browser_keys.entries.values()))
        self.assertEqual(entry['expires'],ops.read(self.root/run)['expires_unix'])

    def test_duplicate_session_key_approval_returns_existing_attempt_without_waiting(self):
        self.setup();run=self.create();Connection.calls=[];Connection.status=200;Connection.echo=False
        entered=threading.Event();release=threading.Event();original=Connection.request
        def hold(connection,*args,**kwargs):
            entered.set();self.assertTrue(release.wait(5));return original(connection,*args,**kwargs)
        try:
            with mock.patch.object(keys.http.client,'HTTPSConnection',Connection),mock.patch.object(Connection,'request',hold):
                p=self.review(run,'factory');first=self.approve(run,'factory',p);self.assertTrue(entered.wait(5))
                started=time.monotonic();second=self.approve(run,'factory',p)
                self.assertLess(time.monotonic()-started,2);self.assertEqual(first['job']['id'],second['job']['id'])
                release.set();self.assertEqual(self.settled(run)['status'],'FINISHED')
            self.assertEqual(len(Connection.calls),1)
        finally:release.set()

    def test_three_separate_paid_roles_keep_existing_effect_approvals_and_recovery(self):
        self.setup();run=self.create();Connection.calls=[];Connection.status=200;Connection.echo=False
        with mock.patch.object(keys.http.client,'HTTPSConnection',Connection):
            self.one(run,'factory');self.assertEqual(len(Connection.calls),1)
            self.one(run,'governance');self.effect(run,'batch');self.effect(run,'fault-candidate')
            with ops.opened(self.root/run) as mission:mission.observe();mission.observe()
            __import__('operations_supervisor').step(self.root/run)
            self.one(run,'recovery');self.effect(run,'recovery');self.effect(run,'failover-manager');self.effect(run,'failover-owner_C')
            self.effect(run,'job',job='C-mission-job')
        saved=ops.read(self.root/run)
        self.assertEqual(saved['phase'],'RECOVERED_SERVICE_ACTIVE');self.assertEqual(saved['service']['metrics']['committed'],40)
        self.assertEqual(len(Connection.calls),3);self.assertEqual(saved['usage']['reported_tokens'],90)
        self.assertEqual(ops.verify(self.root/run)['status'],'CONSISTENT')
        self.post('/api/action',{'action':'stop','run':run});self.assertEqual(self.server.browser_keys.entries,{})


if __name__=='__main__':unittest.main()
