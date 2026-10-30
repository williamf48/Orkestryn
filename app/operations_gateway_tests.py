from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
import copy,json,tempfile,unittest
import inference_gateway as g
import simulator as sim
class Provider(g.Scripted):
    live=True
    def __init__(self,headers=None):self.calls=0;self.headers=headers or {}
    def ask(self,profile,text):self.calls+=1;value,meta=super().ask(profile,text);return value,{**meta,'source':'MOCK API; NO NETWORK','total_tokens':12,'headers':self.headers}
class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.j=g.Journal(self.root,max_calls=2,token_budget=10000);self.principal={'subject':'test'};self.state=sim.initial_state();self.state.update(routing={'stable-v1':80,'candidate-v2':20},route_owner='route-C');self.actual={'runtime_id':'r','revision':1,'candidate_percent':20,'route_owner':'route-C','workers':{'candidate-v2':{'healthy':True},'stable-v1':{'healthy':True}}}
    def tearDown(self):self.j.close();self.tmp.cleanup()
    def run_task(self,key='task-1',provider=None,guard=lambda:None):return self.j.run(key,'Urgent route timeout',self.principal,provider or Provider(),self.state,self.actual,'source',guard)
    def test_task_routing_uses_current_profile_and_exact_replay_does_not_repeat_provider(self):
        provider=Provider();r=self.run_task(provider=provider);self.assertEqual(r['profile'],'candidate-v2');self.assertEqual(provider.calls,1);self.assertTrue(self.run_task(provider=provider)['replayed']);self.assertEqual(provider.calls,1)
        self.actual['candidate_percent']=0;self.state['routing']={'stable-v1':100,'candidate-v2':0};self.assertEqual(self.run_task('task-2',provider)['profile'],'stable-v1')
    def test_same_id_with_different_input_and_third_request_cannot_spend(self):
        provider=Provider();self.run_task(provider=provider)
        with self.assertRaises(ValueError):self.j.run('task-1','Different',self.principal,provider,self.state,self.actual,'source',lambda:None)
        self.run_task('task-2',provider)
        with self.assertRaisesRegex(ValueError,'budget'):self.run_task('task-3',provider)
        self.assertEqual(provider.calls,2)
    def test_uncertain_saved_attempt_is_not_retried(self):
        provider=Provider()
        # Inject a process-like interruption before the provider result is saved.
        class Death(BaseException):pass
        with mock.patch.object(provider,'ask',side_effect=Death()):
            with self.assertRaises(Death):self.run_task(provider=provider)
        result=self.run_task(provider=provider);self.assertTrue(result['replayed']);self.assertNotEqual(result['status'],'SUCCEEDED');self.assertEqual(provider.calls,0)
    def test_failed_request_retains_unknown_usage_and_local_reservation(self):
        provider=Provider();provider.ask=mock.Mock(side_effect=g.ProviderError(500))
        result=self.run_task(provider=provider);summary=self.j.summary();self.assertEqual(result['status'],'ERROR');self.assertEqual(summary['missing_usage_calls'],1);self.assertGreater(summary['charged_or_reserved_tokens'],0)
    def test_actual_rate_limit_headers_gate_next_request_without_retry(self):
        p=Provider({'x-ratelimit-remaining-requests':'0','retry-after':'60'});self.run_task(provider=p)
        with self.assertRaisesRegex(ValueError,'waiting'):self.run_task('task-2',p)
        self.assertEqual(p.calls,1);self.assertEqual(self.j.summary()['provider_rate_limit_observation']['headers']['x-ratelimit-remaining-requests'],'0')
    def test_missing_upstream_headers_are_unknown_not_invented_quota(self):
        self.run_task(provider=Provider());self.assertEqual(self.j.summary()['provider_rate_limit_observation']['status'],'UNKNOWN_NO_REAL_PROVIDER_HEADERS')
    def test_exhausted_provider_tokens_and_headerless_429_block_the_next_attempt(self):
        provider=Provider({'x-ratelimit-remaining-tokens':'0'});self.run_task(provider=provider)
        with self.assertRaisesRegex(ValueError,'waiting'):self.run_task('task-2',provider)
        self.assertEqual(provider.calls,1)
        self.j.close();self.root.joinpath('second').mkdir();self.j=g.Journal(self.root/'second',max_calls=2,token_budget=10000)
        provider=Provider();provider.ask=mock.Mock(side_effect=g.ProviderError(429))
        self.assertEqual(self.run_task(provider=provider)['metadata']['http_status'],429)
        with self.assertRaisesRegex(ValueError,'waiting'):self.run_task('task-2',provider)
        self.assertEqual(provider.ask.call_count,1)
    def test_usage_above_the_reservation_is_fully_charged_and_stops_further_calls(self):
        provider=Provider();content,metadata=provider.ask('candidate-v2','ticket');metadata['total_tokens']=20000
        provider.ask=mock.Mock(return_value=(content,metadata));self.run_task(provider=provider)
        self.assertEqual(self.j.summary()['reported_tokens'],20000);self.assertEqual(self.j.summary()['charged_or_reserved_tokens'],20000)
        with self.assertRaisesRegex(ValueError,'budget'):self.run_task('task-2',provider)
        self.assertEqual(provider.ask.call_count,1)
    def test_unavailable_slot_or_drift_guard_prevents_provider_access(self):
        p=Provider();self.actual['workers']['candidate-v2']['healthy']=False
        with self.assertRaises(ValueError):self.run_task(provider=p)
        self.assertEqual(p.calls,0);self.actual['workers']['candidate-v2']['healthy']=True
        with self.assertRaises(ValueError):self.run_task(provider=p,guard=lambda:(_ for _ in ()).throw(ValueError('state changed')))
        self.assertEqual(p.calls,0)
    def test_http_adapter_has_fixed_endpoint_single_attempt_cap_and_redacts_key(self):
        key='LOCAL_KEY_NEVER_SEND';content=json.dumps({'category':'traffic','priority':'high','summary':key});raw=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':content}}],'model':'nvidia/mock','usage':{'total_tokens':12},'id':'mock'}).encode()
        response=mock.MagicMock();response.__enter__.return_value=response;response.headers={'X-RateLimit-Limit-Requests':'10'};response.read.return_value=raw
        opener=mock.Mock();opener.open.return_value=response
        with mock.patch.object(g,'build_opener',return_value=opener):answer,metadata=g.Nebius(key,512).ask('stable-v1','a ticket')
        self.assertNotIn(key,answer);request=opener.open.call_args.args[0];self.assertEqual(request.full_url,'https://api.tokenfactory.nebius.com/v1/chat/completions');self.assertEqual(opener.open.call_count,1)
        payload=json.loads(request.data);self.assertFalse(payload['store']);self.assertEqual(payload['n'],1);self.assertEqual(payload['service_tier'],'default');self.assertEqual(metadata['headers']['x-ratelimit-limit-requests'],'10')
    def test_http_error_and_redirect_do_not_retry_or_follow_another_host(self):
        opener=mock.Mock();opener.open.side_effect=HTTPError('https://fixed',429,'rate',{'Retry-After':'10'},None)
        with mock.patch.object(g,'build_opener',return_value=opener):
            with self.assertRaises(g.ProviderError) as caught:g.Nebius('LOCAL_KEY',512).ask('stable-v1','ticket')
        self.assertEqual(caught.exception.status,429);self.assertEqual(opener.open.call_count,1);self.assertIsNone(g.NoRedirect().redirect_request(None,None,None,None,None,None))
if __name__=='__main__':unittest.main()
