"""Real loopback tests of the research API's authentication and effect boundary."""
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from http.cookiejar import CookieJar
from urllib.request import Request,build_opener,HTTPCookieProcessor
from urllib.error import HTTPError
from unittest import mock

import operations_server
import safety_lab
import safety_live
import browser_proposals


class SafetyHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='research HTTP ');self.root=Path(self.temp.name)
        self.server=operations_server.Server(self.root)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.client=build_opener(HTTPCookieProcessor(CookieJar()));self.csrf=''
        self.case=safety_lab.dataset()['cases'][0]['id']
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(5);self.temp.cleanup()
    def get(self,path='/api/safety',client=None,raw=False):
        with (client or self.client).open(self.base+path,timeout=30) as response:
            value=response.read();return value if raw else json.loads(value)
    def post(self,value,path='/api/safety',client=None,csrf=None,origin=None):
        req=Request(self.base+path,data=json.dumps(value).encode(),method='POST',
            headers={'Content-Type':'application/json','Origin':origin or self.base,'X-CSRF-Token':self.csrf if csrf is None else csrf})
        with (client or self.client).open(req,timeout=30) as response:return json.load(response)
    def setup(self):
        value=self.post({'name':'researcher','password':'A long research password!','bootstrap':self.server.bootstrap},path='/api/setup')
        self.csrf=value['csrf'];return value
    def test_dashboard_has_navigation_and_private_research_data_requires_login(self):
        page=self.get('/',raw=True)
        self.assertIn(b'id="safetynav"',page)
        self.assertIn(b'href="/safety',page)
        self.assertIn(b'aria-label="Safety Lab sections"',self.get('/safety',raw=True))
        with self.assertRaises(HTTPError) as caught:self.get()
        self.assertEqual(caught.exception.code,401)
        self.setup()
        with mock.patch.object(browser_proposals,'RoleClient',side_effect=AssertionError('No startup request')):
            state=self.get()
        self.assertEqual(len(state['catalog']),48);self.assertEqual(state['attempts'],[])
        self.assertEqual(self.get('/api/state')['runs'],[])
    def test_viewer_cannot_evaluate_import_or_authorize_a_model_request(self):
        self.setup();self.post({'name':'reader','password':'A long reader password!','roles':['viewer']},path='/api/account')
        client=build_opener(HTTPCookieProcessor(CookieJar()))
        token=self.post({'name':'reader','password':'A long reader password!'},path='/api/login',client=client,csrf='')['csrf']
        self.assertEqual(self.get(client=client)['principal']['name'],'reader')
        for body in ({'action':'evaluate'},{'action':'paid-review','case_id':self.case,'kind':'monitor','output_cap':512}):
            with self.assertRaises(HTTPError) as caught:self.post(body,client=client,csrf=token)
            self.assertEqual(caught.exception.code,403)
        self.assertEqual(self.get()['runs'],[])
    def test_csrf_origin_unknown_fields_and_path_injection_have_no_effect(self):
        self.setup()
        for options in ({'csrf':'bad'},{'origin':'https://untrusted.example'}):
            with self.assertRaises(HTTPError) as caught:self.post({'action':'evaluate'},**options)
            self.assertEqual(caught.exception.code,403)
        for body in ({'action':'evaluate','output':'../outside'},{'action':'evaluate','case_id':'../../outside'},
                     {'action':'evaluate','nebius_api_key':'not a supported field'},
                     {'action':'paid-review','case_id':self.case,'kind':'shell','output_cap':512}):
            with self.assertRaises(HTTPError) as caught:self.post(body)
            self.assertEqual(caught.exception.code,400)
        self.assertEqual(self.get()['runs'],[])
    def test_free_real_api_evaluation_report_and_export_recompute_without_service_effects(self):
        self.setup()
        with mock.patch.object(browser_proposals,'RoleClient',side_effect=AssertionError('No free request')):
            result=self.post({'action':'evaluate','split':'heldout','budgets':[1,4,8]})
            saved=self.get('/api/safety/report?run='+result['id'])
            raw=self.get('/api/safety/evidence?run='+result['id'],raw=True)
            page=self.get('/safety/report?run='+result['id'],raw=True)
        self.assertEqual(len(saved['results']['cases']),24)
        self.assertEqual(safety_lab.verify_bytes(raw)['status'],'RECOMPUTED')
        self.assertIn(b'Honest tasks completed',page)
        self.assertEqual(self.get('/api/state')['runs'],[])
        self.assertEqual(result['workspace_attempts_at_evaluation']['recorded_attempts'],0)
    def test_case_information_barrier_and_queries_are_bounded(self):
        self.setup();view=self.get('/api/safety/case?id='+self.case)
        self.assertEqual(set(view),{'id','public_context','view_digest','scope'})
        self.assertNotIn('private',view['public_context'])
        for path in ('/api/safety/case?id=../outside','/api/safety/case?id=a&id=b',
                     '/api/safety/report?run=../outside','/api/safety/report?run=a&extra=b','/api/safety?extra=x'):
            with self.subTest(path=path),self.assertRaises(HTTPError) as caught:self.get(path)
            self.assertEqual(caught.exception.code,400)
    def test_paid_preview_is_free_and_stale_or_disabled_confirmation_never_requests(self):
        self.setup()
        with mock.patch.object(browser_proposals,'RoleClient',side_effect=AssertionError('No paid request')):
            preview=self.post({'action':'paid-review','case_id':self.case,'kind':'monitor','output_cap':1024})
            self.assertIn('digest',preview);self.assertNotIn('private',preview['request']['context'])
            with self.assertRaises(HTTPError):self.post({'action':'paid-attempt','case_id':self.case,'kind':'monitor','output_cap':1024,
                 'review_digest':'stale','confirmed':True,'nebius_api_key':'fixture-without-live-access'})
            self.assertEqual(self.get()['attempts'],[])
            self.server.allow_browser_keys=False
            with self.assertRaises(HTTPError):self.post({'action':'paid-review','case_id':self.case,'kind':'monitor','output_cap':1024})
    def test_authenticated_paid_result_records_one_attempt_and_no_api_key(self):
        self.setup();key='research-api-fixture-no-live-access'
        client=mock.Mock();client._key=key
        client.ask.return_value=(json.dumps({'decision':'allow','risk_score':1,'reason':'The supplied actions preserve policy.'}),
                                 {'finish_reason':'stop','refused':False,'total_tokens':101})
        with mock.patch.object(browser_proposals,'RoleClient',return_value=client):
            preview=self.post({'action':'paid-review','case_id':self.case,'kind':'monitor','output_cap':512})
            body={'action':'paid-attempt','case_id':self.case,'kind':'monitor','output_cap':512,
                  'review_digest':preview['digest'],'confirmed':True,'nebius_api_key':key}
            self.assertEqual(self.post(body)['status'],'REPLIED')
            with self.assertRaises(HTTPError):self.post(body)
        state=self.get();self.assertEqual(len(state['attempts']),1)
        self.assertEqual(state['attempts'][0]['approved_by']['name'],'researcher')
        self.assertNotIn(key,json.dumps(state));self.assertEqual(client.ask.call_count,1)
        with safety_lab.Lab(self.root) as lab:self.assertNotIn(key.encode(),(lab.root/'research.sqlite3').read_bytes())

    def test_paired_analysis_is_available_through_the_shared_app_api(self):
        self.setup()
        result=self.post({'action':'evaluate','case_id':self.case,'budgets':[4],'framing':True})
        self.assertEqual(len(result['results']['cases']),3)
        self.assertEqual(len(result['results']['framing_analysis']['rows']),10)
        self.assertEqual(self.get('/api/state')['runs'],[])
        with self.assertRaises(HTTPError):self.post({'action':'evaluate','framing':'true'})

    def test_visibility_change_invalidates_paid_review_before_any_client(self):
        self.setup()
        preview=self.post({'action':'paid-review','case_id':self.case,'kind':'monitor','output_cap':512,'visibility':'actions_only'})
        self.assertEqual(preview['request']['visibility'],'actions_only')
        with mock.patch.object(browser_proposals,'RoleClient',side_effect=AssertionError('No paid request')):
            with self.assertRaises(HTTPError):self.post({'action':'paid-attempt','case_id':self.case,'kind':'monitor','output_cap':512,
                'visibility':'full_context','review_digest':preview['digest'],'confirmed':True,'nebius_api_key':'fixture-only-key'})
        self.assertEqual(self.get()['attempts'],[])

    def test_revoked_manager_cannot_use_a_previously_prepared_research_review(self):
        self.setup()
        created=self.post({'name':'second','password':'A second long password!','roles':['manager','viewer']},path='/api/account')
        client=build_opener(HTTPCookieProcessor(CookieJar()))
        token=self.post({'name':'second','password':'A second long password!'},path='/api/login',client=client,csrf='')['csrf']
        preview=self.post({'action':'paid-review','case_id':self.case,'kind':'monitor','output_cap':512},client=client,csrf=token)
        subject=self.get(client=client)['principal']['subject']
        self.post({'subject':subject,'roles':['viewer']},path='/api/account-change')
        with mock.patch.object(browser_proposals,'RoleClient',side_effect=AssertionError('No revoked request')):
            with self.assertRaises(HTTPError):self.post({'action':'paid-attempt','case_id':self.case,'kind':'monitor','output_cap':512,
                'review_digest':preview['digest'],'confirmed':True,'nebius_api_key':'fixture-only-key'},client=client,csrf=token)
        self.assertEqual(self.get()['attempts'],[])


if __name__=='__main__':unittest.main()
