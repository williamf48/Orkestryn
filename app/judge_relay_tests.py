"""Offline relay boundaries. All upstream replies are mocks; no model calls."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import downloadable_demo
import judge_relay as relay
import judge_setup
import operations_proposals
import operations_workspace as ops
import relay_demo
import release_check

SETTINGS = {'version':relay.VERSION, 'origin':'https://evaluation.example.com',
            'username':'judge', 'password':'test-fixture-password-for-local-relay-only'}


def review(role='factory'):
    value = {'kind':'paid_proposal', 'role':role, 'mission_id':'fixture-run-1',
        'settings':ops.factory.inference.inference_settings(8192, True),
        'operations_source':ops.source_digest(), 'expires_unix':time.time()+1800,
        'context':{'fixture':'Not a live provider request.'}}
    value['context_digest'] = ops.sim.digest(value['context'])
    value['digest'] = ops.sim.digest(value)
    return value


def response(role='factory'):
    return {'schema_version':relay.VERSION, 'role':role,
        'request_id':hashlib.sha256(('fixture-run-1:'+role).encode()).hexdigest(),
        'model_requested':ops.factory.inference.MODEL,
        'message':{'content':'<think>private reasoning</think>{"fixture":true}', 'refusal':None},
        'metadata':{'finish_reason':'stop','refused':False,'total_tokens':17}}


class Connection:
    calls = []
    value = None
    status = 200
    def __init__(self,*args,**kwargs):self.closed=False;self.__class__.calls.append((args,kwargs,self))
    def request(self,*args,**kwargs):self.sent=(args,kwargs)
    def getresponse(self):return self
    def read(self,limit):return json.dumps(self.value).encode()[:limit]
    def close(self):self.closed=True


class RelayTests(unittest.TestCase):
    def setUp(self):
        Connection.calls=[];Connection.value=response();Connection.status=200

    def test_three_clients_are_offline_at_construction_and_match_registered_contracts(self):
        with mock.patch.object(relay.http.client,'HTTPSConnection') as network:
            clients=relay.providers(SETTINGS)
            job=operations_proposals.Jobs(Path('.'),clients)
            self.assertTrue(job.enabled);self.assertEqual(set(clients),set(ops.ROLES));network.assert_not_called();job.close()
        for role, client in clients.items():
            self.assertEqual(client.system_prompt,ops.PROMPTS[role]);self.assertTrue(client.live)
            self.assertEqual(client.request_settings,ops.factory.inference.inference_settings(8192,True))

    def test_url_rejects_insecure_local_credential_and_redirect_paths(self):
        for bad in ('http://evaluation.example.com','https://user:secret@evaluation.example.com',
                    'https://127.0.0.1','https://localhost','https://service.local','https://evaluation.example.com/path',
                    'https://evaluation.example.com?key=secret','https://evaluation.example.com:444'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):relay.origin(bad)

    def test_private_connection_is_exclusive_strict_and_blocked_from_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);relay.save(path,SETTINGS);self.assertEqual(relay.load(path),SETTINGS)
            with self.assertRaises(FileExistsError):relay.save(path,SETTINGS)
            data=(path/relay.CONNECTION).read_bytes()
            self.assertIsNotNone(release_check.path_problem(relay.CONNECTION))
            self.assertIsNotNone(release_check.path_problem('RELAY_OWNER_PRIVATE.txt'))
            (path/relay.CONNECTION).write_text('{"version":"x","version":"y"}')
            with self.assertRaises(ValueError):relay.load(path)
            self.assertIn(SETTINGS['password'].encode(),data)

    def test_missing_extra_or_provider_key_settings_are_rejected(self):
        for value in ({**SETTINGS,'NEBIUS_API_KEY':'not-accepted'}, {k:v for k,v in SETTINGS.items() if k!='password'},
                      {**SETTINGS,'password':'short'}, {**SETTINGS,'username':'judge:other'}):
            with self.assertRaises(ValueError):relay.validate(value)

    def test_request_requires_exact_review_and_is_consumed_once(self):
        client=relay.RoleClient('factory',SETTINGS)
        with self.assertRaises(relay.RelayError):client.ask({'fixture':'Not a live provider request.'})
        client.bind_review(review())
        with mock.patch.object(relay.http.client,'HTTPSConnection',Connection):
            content,metadata=client.ask(review()['context'])
            self.assertEqual(content,'{"fixture":true}');self.assertEqual(metadata['total_tokens'],17)
            self.assertEqual(metadata['relay_origin'],SETTINGS['origin']);self.assertTrue(metadata['marked_thinking_content_removed'])
            with self.assertRaises(relay.RelayError):client.ask(review()['context'])
        self.assertEqual(len(Connection.calls),1);self.assertTrue(Connection.calls[0][2].closed)
        args,kwargs=Connection.calls[0][2].sent
        payload=json.loads(kwargs['body']);self.assertEqual(args,('POST','/v1/proposal'))
        self.assertEqual(payload['prompt_digest'],hashlib.sha256(ops.PROMPTS['factory'].encode()).hexdigest())
        self.assertNotIn('NEBIUS_API_KEY',kwargs['body'].decode());self.assertNotIn(SETTINGS['password'],kwargs['body'].decode())

    def test_changed_context_and_review_cannot_send(self):
        client=relay.RoleClient('factory',SETTINGS);value=review()
        value['context_digest']='0'*64
        with self.assertRaises(ValueError):client.bind_review(value)
        client.bind_review(review())
        with mock.patch.object(relay.http.client,'HTTPSConnection') as network:
            with self.assertRaises(relay.RelayError):client.ask({'different':'context'})
            network.assert_not_called()

    def test_redirect_and_error_are_never_followed_or_retried(self):
        for status in (301,302,307,401,429,500):
            Connection.calls=[];Connection.status=status;client=relay.RoleClient('factory',SETTINGS);value=review();client.bind_review(value)
            with mock.patch.object(relay.http.client,'HTTPSConnection',Connection):
                with self.assertRaisesRegex(relay.RelayError,'RELAY_HTTP_'+str(status)):client.ask(value['context'])
            self.assertEqual(len(Connection.calls),1);self.assertTrue(Connection.calls[0][2].closed)

    def test_transport_exception_is_private_and_not_retried(self):
        client=relay.RoleClient('factory',SETTINGS);value=review();client.bind_review(value)
        with mock.patch.object(relay.http.client,'HTTPSConnection',Connection), mock.patch.object(Connection,'request',side_effect=OSError(SETTINGS['password'])) as sent:
            with self.assertRaises(relay.RelayError) as result:client.ask(value['context'])
            self.assertNotIn(SETTINGS['password'],str(result.exception));self.assertEqual(sent.call_count,1)

    def test_response_must_belong_to_same_role_attempt_and_model(self):
        for field,bad in (('request_id','0'*64),('role','recovery'),('model_requested','other')):
            Connection.value={**response(),field:bad};client=relay.RoleClient('factory',SETTINGS);value=review();client.bind_review(value)
            with mock.patch.object(relay.http.client,'HTTPSConnection',Connection):
                with self.assertRaises(relay.RelayError):client.ask(value['context'])

    def test_received_content_and_metadata_redact_evaluation_password(self):
        Connection.value=response();Connection.value['message']['content']=SETTINGS['password']
        Connection.value['metadata']['response_id']=SETTINGS['password']
        client=relay.RoleClient('factory',SETTINGS);value=review();client.bind_review(value)
        with mock.patch.object(relay.http.client,'HTTPSConnection',Connection):content,metadata=client.ask(value['context'])
        self.assertNotIn(SETTINGS['password'],content+json.dumps(metadata))

    def test_launcher_startup_no_sdk_key_or_network_and_preserves_restart_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);relay.save(root,SETTINGS)
            marker=root/'original-workspace.txt';marker.write_text('preserve')
            with mock.patch.object(downloadable_demo,'package_version',return_value='fixture'), mock.patch.object(relay.http.client,'HTTPSConnection') as network:
                server,guide,_=relay_demo.prepare(root)
                original=judge_setup.load(root/'relay_workspace')
                self.assertTrue(server.proposals.enabled);server.server_close();network.assert_not_called()
                server,other,_=relay_demo.prepare(root);server.server_close();network.assert_not_called()
            self.assertEqual(judge_setup.load(root/'relay_workspace'),original)
            self.assertNotEqual(guide,other);self.assertEqual(marker.read_text(),'preserve')
            text=guide.read_text();self.assertNotIn(SETTINGS['password'],text)
            for password in original['accounts'].values():self.assertIn(password,text)

    def test_owner_preparation_generates_private_evaluation_login_without_provider_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'new';path=relay_demo.prepare_owner(root,SETTINGS['origin']);value=relay.load(root)
            self.assertTrue(path.is_file());self.assertEqual(value['username'],'judge')
            self.assertNotEqual(value['password'],SETTINGS['password'])
            with self.assertRaises(FileExistsError):relay_demo.prepare_owner(root,SETTINGS['origin'])

    def test_hidden_prompt_has_no_echo_fallback_or_password_argument(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(relay_demo.terminal_guard,'interactive_input',return_value=True), mock.patch.object(relay_demo,'getpass',side_effect=relay_demo.GetPassWarning('unsafe')):
            with self.assertRaises(relay_demo.GetPassWarning):relay_demo.setup(Path(tmp),SETTINGS['origin'])
            self.assertFalse((Path(tmp)/relay.CONNECTION).exists())

    def test_role_request_binds_review_only_after_existing_manager_and_phase_checks(self):
        # Full guarded mission path; the mock returns a valid registered Factory
        # response through the actual relay client. Subsequent roles remain unrequested.
        with tempfile.TemporaryDirectory() as tmp:
            path=ops.create(Path(tmp),live=True,settings=ops.factory.inference.inference_settings(8192,True))
            import named_access
            auth=named_access.Access(Path(tmp))
            try:
                manager=auth.create('manager-fixture','private-password-12345',['manager','viewer'])
                with ops.opened(path,auth=auth) as run:
                    value=run.proposal_review('factory')
                    fixture,_=ops.ScriptedRole('factory').ask(value['context'])
                    Connection.value={**response(),'request_id':hashlib.sha256((value['mission_id']+':factory').encode()).hexdigest(),
                                      'message':{'content':fixture,'refusal':None}}
                    client=relay.RoleClient('factory',SETTINGS)
                    with mock.patch.object(relay.http.client,'HTTPSConnection',Connection):
                        result=run.request_proposal('factory',value['digest'],manager,client)
                    self.assertEqual(result['phase'],'QUEUE_READY');self.assertEqual(len(result['calls']),1)
                    self.assertEqual(result['calls'][0]['status'],'VALIDATED')
                    run.stop()
            finally:auth.close()

    def test_all_three_relay_clients_complete_guarded_http_recovery_with_mock_upstream(self):
        # Exercise the existing authenticated UI API through the new clients.
        # Only the HTTPS transport is replaced; no provider inference is claimed.
        from operations_proposals_tests import ProposalTests
        import operations_supervisor
        import operations_evidence
        import zipfile
        helper=ProposalTests('test_disabled_workspace_rejects_live_creation');helper.setUp()
        captured=[]
        def send(connection,*args,**kwargs):
            connection.sent=(args,kwargs);body=json.loads(kwargs['body']);captured.append(body)
            content,_=ops.ScriptedRole(body['role']).ask(json.loads(body['context']))
            connection.value={**response(body['role']),'request_id':body['request_id'],
                              'message':{'content':content,'refusal':None}}
        try:
            helper.server.proposals.close();helper.providers=relay.providers(SETTINGS)
            helper.server.proposals=operations_proposals.Jobs(helper.root,helper.providers)
            with mock.patch.object(relay.http.client,'HTTPSConnection',Connection), mock.patch.object(Connection,'request',send):
                run=helper.create();self.assertEqual(captured,[])
                helper.one(run,'factory');self.assertEqual(len(captured),1)
                helper.one(run,'governance');helper.effect(run,'batch');helper.effect(run,'fault-candidate')
                with ops.opened(helper.root/run) as mission:mission.observe();mission.observe()
                operations_supervisor.step(helper.root/run)
                helper.one(run,'recovery');helper.effect(run,'recovery')
                helper.effect(run,'failover-manager');helper.effect(run,'failover-owner_C')
                helper.effect(run,'job',job='C-mission-job')
            summary=ops.read(helper.root/run)
            self.assertEqual([v['role'] for v in captured],list(ops.ROLES))
            self.assertEqual(summary['phase'],'RECOVERED_SERVICE_ACTIVE')
            self.assertEqual(summary['service']['metrics']['committed'],40)
            self.assertEqual(ops.verify(helper.root/run)['status'],'CONSISTENT')
            raw,_=operations_evidence.build_bytes(helper.root/run)
            self.assertEqual(operations_evidence.verify_bytes(raw)['status'],'INTEGRITY_CHECKED')
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                for name in archive.namelist():self.assertNotIn(SETTINGS['password'].encode(),archive.read(name))
        finally:helper.tearDown()


if __name__=='__main__':unittest.main()
