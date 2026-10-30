#!/usr/bin/env python3
"""Loopback named-user control API; exact source/state-bound browser reviews."""
from __future__ import annotations
import argparse,copy,http.client,json,re,secrets,sqlite3,threading,time,webbrowser
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
import operations_workspace as ops
import operations_supervisor as supervisor
import operations_ui
import operations_outcomes as outcomes
import named_access
import inference_gateway as gateway
import workflow_planner as planner
import operations_proposals
import operations_evidence
import browser_proposals
import safety_http

def public_origin(value):
    """An explicit HTTPS origin, never inferred from forwarded request headers."""
    if not isinstance(value,str):raise ValueError('Set one exact HTTPS public origin.')
    try:
        parsed=urlsplit(value);port=parsed.port
        labels=(parsed.hostname or '').split('.')
        valid=all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label) for label in labels)
        canonical='https://'+(parsed.hostname or '')+(':'+str(port) if port is not None else '')
        if (parsed.scheme!='https' or not valid or len(labels)<2 or port in {0,443}
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment or canonical!=value):raise ValueError
    except (ValueError,TypeError):raise ValueError('Use an exact HTTPS origin such as https://your-service.example, without a path or trailing slash.') from None
    return value

class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,root,port=0,provider=None,max_calls=16,proposal_providers=None,*,https_origin=None,bind_host='127.0.0.1',allow_browser_keys=True):
        self.root=ops.directory_for(root);self.root.mkdir(parents=True,exist_ok=True)
        self.https_origin=public_origin(https_origin) if https_origin is not None else None
        if bind_host not in {'127.0.0.1','0.0.0.0'} or (bind_host!='127.0.0.1' and not self.https_origin):
            raise ValueError('A network listener requires an explicit HTTPS public origin.')
        if type(max_calls) is not int or not 1<=max_calls<=32:raise ValueError('Use a 1-32 inference request cap.')
        self.provider=provider;self.max_calls=max_calls;self.bootstrap=secrets.token_urlsafe(32);self.setup_lock=threading.Lock()
        self.proposals=operations_proposals.Jobs(self.root,proposal_providers)
        if type(allow_browser_keys) is not bool:raise ValueError('Use an explicit browser key-entry capability.')
        self.allow_browser_keys=allow_browser_keys
        self.browser_keys=browser_proposals.Registry(self.root)
        self.proposal_submit_lock=threading.Lock()
        self.evaluation=None
        auth=named_access.Access(self.root)
        try:
            if self.https_origin and not auth.count():raise ValueError('Configure the hosted accounts before exposing the server.')
        finally:auth.close()
        super().__init__((bind_host,port),Handler)
    def server_close(self):
        self.browser_keys.close();self.proposals.close();super().server_close()
    def proposal_jobs(self):return {**self.proposals.snapshot(),**self.browser_keys.snapshot()}
    def jobs_for(self,state,token,principal):
        access=state['operations'].get('browser_access')
        if access:
            if access.get('owner')!=principal['subject']:raise named_access.AccessDenied('Only the original manager can use this workflow API key.')
            return self.browser_keys.get(state['mission_id'],token,principal)
        return self.proposals
    def proposal_access(self):
        """Public capability metadata only; never serialize a client or key."""
        return {'provider':'Nebius Token Factory','model':ops.factory.inference.MODEL,
                'available':self.proposals.enabled,'status':'CONFIGURED' if self.proposals.enabled else 'OWNER_SETUP_REQUIRED',
                'funded_by':'WORKSPACE_OWNER','judge_key_required':False,'startup_model_calls':0,
                'browser_key_entry':self.allow_browser_keys,'browser_key_storage':'PROCESS_MEMORY_ONLY'}

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_):pass
    def reply(self,status,data,html=False,cookie=None):
        raw=data.encode() if html else json.dumps(data,allow_nan=False).encode();self.send_response(status)
        for k,v in [('Content-Type','text/html; charset=utf-8' if html else 'application/json'),('Content-Length',str(len(raw))),('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),('Referrer-Policy','no-referrer'),('X-Frame-Options','DENY')]:self.send_header(k,v)
        if cookie:self.send_header('Set-Cookie',cookie)
        self.end_headers();self.wfile.write(raw)
    def reply_archive(self,raw,filename):
        self.send_response(200)
        for key,value in [('Content-Type','application/zip'),('Content-Length',str(len(raw))),
                          ('Content-Disposition','attachment; filename="'+filename+'"'),
                          ('Cache-Control','no-store, private'),('X-Content-Type-Options','nosniff'),
                          ('Referrer-Policy','no-referrer'),('X-Frame-Options','DENY')]:
            self.send_header(key,value)
        self.end_headers();self.wfile.write(raw)
    def origin(self,post=False):
        origin=self.server.https_origin or 'http://127.0.0.1:'+str(self.server.server_port)
        host=urlsplit(origin).netloc
        # Duplicate headers and proxy-supplied origins never expand this boundary.
        hosts=self.headers.get_all('Host',[]);origins=self.headers.get_all('Origin',[])
        return hosts==[host] and (origins==[origin] if post else not origins or origins==[origin])
    def session_cookie(self,token='',logout=False):
        return ('ar_session='+token+'; HttpOnly; SameSite=Strict; Path=/; Max-Age='
                +('0' if logout else '1800')+('; Secure' if self.server.https_origin else ''))
    def token(self):
        cookie=SimpleCookie();cookie.load(self.headers.get('Cookie',''));return cookie.get('ar_session').value if cookie.get('ar_session') else ''
    def auth(self,store,write=False):return store.session(self.token(),self.headers.get('X-CSRF-Token','') if write else None)
    def body(self):
        length=int(self.headers.get('Content-Length','0'))
        if not 1<=length<=32768 or self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('Send a bounded JSON operation.')
        self.connection.settimeout(10);return json.loads(self.rfile.read(length),parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Nonfinite JSON.')))
    def run_path(self,run):return ops.directory_for(self.server.root,run)
    def do_GET(self):
        # The platform health probe reports no account, run, key or model data.
        if self.path=='/healthz':return self.reply(200,{'status':'ok'})
        if not self.origin():return self.reply(403,{'error':'Use the exact local workspace origin.'})
        auth=named_access.Access(self.server.root)
        try:
            path=urlsplit(self.path).path
            if safety_http.get(self,path,auth):return
            if path=='/':return self.reply(200,operations_ui.PAGE.replace('__BOOTSTRAP__',self.server.bootstrap if not self.server.https_origin and not auth.count() else '').replace('__HOSTED__','true' if self.server.https_origin else 'false'),html=True)
            if path=='/api/public-status':return self.reply(200,{'setup_required':not self.server.https_origin and not auth.count(),'hosted':bool(self.server.https_origin),'inference_enabled':self.server.provider is not None,'live_proposals_enabled':self.server.proposals.enabled})
            if path=='/api/evidence':
                self.auth(auth)
                query=parse_qs(urlsplit(self.path).query,keep_blank_values=True,max_num_fields=1)
                if set(query)!={'run'} or len(query['run'])!=1 or not query['run'][0]:
                    raise ValueError('Choose one exact run for an evidence download.')
                raw,filename=operations_evidence.build_bytes(self.run_path(query['run'][0]))
                return self.reply_archive(raw,filename)
            if path in {'/api/outcome','/outcome'}:
                self.auth(auth)
                query=parse_qs(urlsplit(self.path).query,keep_blank_values=True,max_num_fields=2)
                if set(query)-{'run','baseline'} or 'run' not in query or any(len(v)!=1 or not v[0] for v in query.values()):raise ValueError('Choose one exact run and an optional baseline.')
                report=outcomes.build(self.run_path(query['run'][0]),self.run_path(query['baseline'][0]) if 'baseline' in query else None)
                return self.reply(200,outcomes.render(report) if path=='/outcome' else report,html=path=='/outcome')
            if path!='/api/state':return self.reply(404,{'error':'Unknown endpoint.'})
            principal,csrf=self.auth(auth);runs=[];background={};credentials={}
            self.server.browser_keys.reap()
            for folder in sorted(self.server.root.iterdir(),key=lambda p:p.name,reverse=True):
                if not folder.is_dir() or folder.name.startswith('.'):continue
                try:
                    value=ops.read(folder);runs.append({'id':folder.name,'summary':value});background[folder.name]=supervisor.status(folder)
                    if value['operations'].get('browser_access'):
                        credentials[folder.name]=self.server.browser_keys.status(folder.name,self.token(),principal)
                except (ValueError,OSError,KeyError):continue
                if len(runs)>=50:break
            row=auth.db.execute('SELECT expires FROM sessions WHERE token=?',(browser_proposals.session_id(self.token()),)).fetchone()
            if row is None:raise named_access.AccessDenied('Sign in again.')
            session={'expires_unix':row[0],'server_now_unix':time.time(),'duration_seconds':1800,'renewed_by_reads':False}
            return self.reply(200,{'principal':principal,'csrf':csrf,'session':session,'evaluation':self.server.evaluation,'runs':runs,'supervision':background,'proposal_credentials':credentials,'inference_enabled':self.server.provider is not None,'live_proposals_enabled':self.server.proposals.enabled,'proposal_access':self.server.proposal_access(),'proposal_settings':self.server.proposals.settings,'proposal_jobs':self.server.proposal_jobs(),'request_cap':min(self.server.max_calls,16),'users':[{'subject':uid,'name':name,'roles':json.loads(roles),'active':bool(active)} for uid,name,roles,active in auth.db.execute('SELECT id,name,roles,active FROM users')] if 'admin' in principal['roles'] else []})
        except named_access.AccessDenied:self.reply(401,{'error':'Sign in again.'})
        except operations_evidence.SnapshotChanged:self.reply(409,{'code':'EVIDENCE_CHANGED','error':'The run changed during export. Refresh and download a fresh snapshot.'})
        except (ValueError,OSError,KeyError,TypeError,sqlite3.Error):self.reply(400,{'error':'The saved workspace could not be read.'})
        finally:auth.close()
    def do_POST(self):
        if not self.origin(True):return self.reply(403,{'error':'Use the same local workspace origin.'})
        auth=named_access.Access(self.server.root)
        try:
            body=self.body();endpoint=urlsplit(self.path).path
            if not isinstance(body,dict):raise ValueError('Send one JSON operation object.')
            if endpoint in {'/api/login','/api/setup'}:
                if not isinstance(body,dict) or set(body)-{'name','password','bootstrap'}:raise ValueError('Use account name and password.')
                if endpoint=='/api/setup':
                    with self.server.setup_lock:
                        if self.server.https_origin or auth.count() or not secrets.compare_digest(str(body.get('bootstrap','')),self.server.bootstrap):raise named_access.AccessDenied('Initial setup is unavailable.')
                        auth.create(body.get('name'),body.get('password'),sorted(named_access.ROLES));self.server.bootstrap=''
                token,csrf,principal=auth.login(body.get('name'),body.get('password'));return self.reply(200,{'csrf':csrf,'principal':principal},cookie=self.session_cookie(token))
            principal,csrf=self.auth(auth,True)
            if safety_http.post(self,endpoint,body,principal,auth):return
            if endpoint=='/api/logout':
                auth.logout(self.token());self.server.browser_keys.forget(token=self.token())
                return self.reply(200,{'status':'SIGNED_OUT'},cookie=self.session_cookie(logout=True))
            if endpoint=='/api/account':
                if set(body)!={'name','password','roles'}:raise ValueError('Use the exact account contract.')
                user=auth.create(body['name'],body['password'],body['roles'],principal);return self.reply(200,{'user':user})
            if endpoint=='/api/account-change':
                if set(body)-{'subject','roles','active'} or 'subject' not in body:raise ValueError('Use the exact account change contract.')
                auth.change(principal,body['subject'],body.get('roles'),body.get('active'));self.server.browser_keys.reap();return self.reply(200,{'status':'UPDATED'})
            if endpoint=='/api/action' and body.get('action')=='create':
                auth.require(principal,'manager')
                if set(body)-{'action','profile','workflow','proposal_mode','duration_minutes','nebius_api_key'}:raise ValueError('Use the registered workflow creation fields.')
                minutes=body.get('duration_minutes',ops.DEFAULT_RUN_DURATION_MINUTES)
                if type(minutes) is not int or minutes not in ops.RUN_DURATION_MINUTES:raise ValueError('Choose a run duration of 15, 30 or 60 minutes.')
                mode=body.get('proposal_mode','scripted')
                if mode not in {'scripted','live'}:raise ValueError('Choose scripted or live proposals.')
                key=None
                if 'nebius_api_key' in body:
                    if mode!='live' or not self.server.allow_browser_keys:raise ValueError('API key entry is available only for the NVIDIA proposal mode.')
                    key=browser_proposals.checked_key(body.pop('nebius_api_key'))
                if mode=='live' and key is None and not self.server.proposals.enabled:raise ValueError('Enter a Nebius API key for NVIDIA mode, or use configured workspace access.')
                workflow=planner.validate_workflow(body['workflow']) if 'workflow' in body else planner.profile(body.get('profile','balanced'))
                settings=ops.factory.inference.inference_settings(8192,True) if key else self.server.proposals.settings if mode=='live' else {}
                path=ops.create(self.server.root,workflow,live=mode=='live',ttl=minutes*60,settings=settings,credential_owner=principal['subject'] if key else None)
                if key:self.server.browser_keys.attach(path.name,self.token(),principal,key,auth)
                with ops.opened(path,auth=auth) as run:value=run.publish() if mode=='live' else run.advance()
                return self.reply(200,{'run':path.name,'phase':value['phase']})
            if endpoint=='/api/proposal-access':
                auth.require(principal,'manager')
                if not self.server.allow_browser_keys or set(body)!={'run','nebius_api_key'}:raise ValueError('Use the NVIDIA key field for one saved workflow.')
                key=browser_proposals.checked_key(body.pop('nebius_api_key'));path=self.run_path(body['run'])
                with ops.opened(path,auth=auth) as run:
                    saved=run.guard();access=saved['operations'].get('browser_access')
                    if not access or access.get('owner')!=principal['subject']:raise named_access.AccessDenied('Reconnect with the original manager account for this workflow.')
                    role={'DRAFT':'factory','QUEUE_READY':'governance','INCIDENT_RECORDED':'recovery'}.get(saved['phase'])
                    if not role:raise ValueError('Reconnect only before the next unattempted proposal.')
                    proposal=run.proposal_review(role)
                    if proposal['settings']!=ops.factory.inference.inference_settings(8192,True):raise ValueError('Use this workflow original NVIDIA settings.')
                    self.server.browser_keys.attach(path.name,self.token(),principal,key,auth)
                    run.save(saved,'NVIDIA_ACCESS_CONNECTED',principal=principal,scope='Session memory only; zero model calls.')
                return self.reply(200,{'status':'CONNECTED','model_calls':0})
            if endpoint=='/api/requests':
                if set(body)!={'run','actor','id','tool','parameters','priority','after'}:raise ValueError('Use the exact typed request contract.')
                with ops.opened(self.run_path(body['run']),auth=auth) as run:result=run.submit_request(body['actor'],{k:v for k,v in body.items() if k not in {'run','actor'}},principal)
                return self.reply(200,result)
            if endpoint=='/api/infer':
                if set(body)!={'run','id','text'}:raise ValueError('Use an immutable task ID and ticket text.')
                auth.require(principal,'consumer')
                path=self.run_path(body['run'])
                with ops.opened(path,auth=auth) as run:
                    # The enabled cap is persisted once before the first task.
                    s=run.guard();s['operations']['task_budget']['calls']=min(s['operations']['task_budget']['calls'],self.server.max_calls);run.save(s,'TASK_BUDGET_CONFIRMED')
                    result=run.infer(body['id'],body['text'],principal,self.server.provider or gateway.Scripted())
                return self.reply(200,result)
            if endpoint not in {'/api/review','/api/action'}:return self.reply(404,{'error':'Unknown operation.'})
            path=self.run_path(body.get('run'));action=body.get('action');kind=body.get('kind')
            if endpoint=='/api/action' and action=='approve-review' and isinstance(kind,str) and kind.startswith('proposal-'):
                if set(body)-{'action','run','kind','digest','phrase'} or body.get('phrase') is not None:raise ValueError('Use the exact proposal review contract.')
                with self.server.proposal_submit_lock:
                    jobs=self.server.jobs_for(ops.read(path),self.token(),principal)
                    ops.sim.require(not any(v['run']!=path.name and v['status'] in {'QUEUED','RUNNING'} for v in self.server.proposal_jobs().values()),'PROPOSAL_BUSY','One live proposal is already running. Inspect its saved status.')
                    job=jobs.submit(path,kind.removeprefix('proposal-'),body.get('digest'),principal,auth)
                return self.reply(202,{'status':job['status'],'job':job})
            if endpoint=='/api/action' and action in {'supervise','stop-supervisor'}:
                if set(body)!={'action','run'}:raise ValueError('Use the fixed observer contract.')
                auth.require(principal,'manager');result=supervisor.launch(path) if action=='supervise' else supervisor.stop(path);return self.reply(200,result)
            with ops.opened(path,auth=auth) as run:
                if endpoint=='/api/review':
                    if set(body)-{'run','kind','job'}:raise ValueError('Unexpected review fields.')
                    if isinstance(kind,str) and kind.startswith('proposal-'):
                        auth.require(principal,'manager')
                        jobs=self.server.jobs_for(run.state(),self.token(),principal)
                        if not jobs.enabled:raise ValueError('Live proposals are not enabled in this workspace.')
                        proposal=run.proposal_review(kind.removeprefix('proposal-'));digest=proposal['digest'];phrase=None
                        if proposal['settings']!=jobs.settings:raise ValueError('Restart with the workflow original proposal settings.')
                    elif kind=='batch':auth.require(principal,'manager');proposal,phrase=run.batch_proposal();digest=proposal['plan_digest']
                    elif kind=='recovery':auth.require(principal,'manager');proposal,phrase=run.recovery_proposal();digest=proposal['incident']['digest']
                    elif kind in {'failover-manager','failover-owner_C'}:auth.require(principal,kind.removeprefix('failover-'));proposal=run.failover_proposal();digest=proposal['digest'];phrase=None
                    elif kind=='fault-candidate':
                        auth.require(principal,'manager');value=run.guard()
                        if value['phase']!='MONITORING':raise ValueError('Inject only while monitoring a development run.')
                        proposal={'run':path.name,'fault':'candidate','phase':value['phase'],'snapshot':value['service']['policy_snapshot'] if 'service' in value else run.publish()['service']['policy_snapshot'],'scope':'Explicit development harness fault: stop the owned candidate. No cloud failure is claimed.'};digest=ops.sim.digest(proposal);phrase=None
                    elif kind=='job':auth.require(principal,'owner_C');proposal=run.job_proposal(body.get('job'));digest=proposal['digest'];phrase=None
                    else:raise ValueError('Unknown exact review.')
                    return self.reply(200,{'proposal':proposal,'digest':digest,'phrase':phrase,'scope':proposal.get('scope','Review the complete selected and waiting actions. Approval binds this exact immutable plan.')})
                if action=='approve-review':
                    if set(body)-{'action','run','kind','digest','phrase','job'}:raise ValueError('Unexpected approval fields.')
                    if kind=='batch':
                        p,phrase=run.batch_proposal()
                        if body.get('digest')!=p['plan_digest'] or body.get('phrase')!=phrase:raise ValueError('The reviewed batch changed.')
                        value=run.approve_batch(phrase,principal)
                    elif kind=='recovery':
                        p,phrase=run.recovery_proposal()
                        if body.get('digest')!=p['incident']['digest'] or body.get('phrase')!=phrase:raise ValueError('The incident review changed.')
                        value=run.approve_recovery(phrase,principal)
                    elif kind in {'failover-manager','failover-owner_C'}:value=run.approve_failover(body.get('digest'),principal,kind.removeprefix('failover-'))
                    elif kind=='fault-candidate':
                        auth.require(principal,'manager');value=run.publish();proposal={'run':path.name,'fault':'candidate','phase':value['phase'],'snapshot':value['service']['policy_snapshot'],'scope':'Explicit development harness fault: stop the owned candidate. No cloud failure is claimed.'}
                        if ops.sim.digest(proposal)!=body.get('digest'):raise ValueError('The reviewed harness state changed.')
                        value=run.simulate_failure('candidate')
                    elif kind=='job':value=run.approve_job(body.get('digest'),body.get('job'),principal)
                    else:raise ValueError('Unknown review kind.')
                else:
                    if set(body)!={'action','run'}:raise ValueError('Use the fixed operation contract.')
                    auth.require(principal,'manager')
                    if action=='advance':value=run.advance()
                    elif action=='supervise':value=supervisor.launch(path)
                    elif action=='stop-supervisor':value=supervisor.stop(path)
                    elif action=='stop':supervisor.stop(path);value=run.stop()
                    else:raise ValueError('Unsupported operation.')
            if endpoint=='/api/action' and action=='stop':self.server.browser_keys.forget(run=path.name)
            return self.reply(200,{'status':'RECORDED','phase':value.get('phase',value.get('status'))})
        except named_access.AccessDenied as exc:self.reply(403,{'error':str(exc)})
        except ops.sim.Denied as exc:
            # Policy denial is an expected outcome, not a dropped connection.
            # Preserve the exact guard and return its public code/reason.
            self.reply(410 if exc.code=='MISSION_EXPIRED_OR_STOPPED' else 409,{'code':exc.code,'error':exc.reason})
        except (ValueError,OSError,KeyError,TypeError,sqlite3.Error,http.client.HTTPException) as exc:self.reply(400,{'error':str(exc) if isinstance(exc,ValueError) else 'The operation could not be confirmed. Refresh the saved evidence.'})
        finally:auth.close()
    def do_PUT(self):self.reply(405,{'error':'Unsupported method.'})
    do_DELETE=do_PUT;do_PATCH=do_PUT

def serve(root,port=0,open_browser=False,provider=None,max_calls=16,proposal_providers=None,*,allow_browser_keys=True):
    server=Server(root,port,provider,max_calls,proposal_providers,allow_browser_keys=allow_browser_keys);url='http://127.0.0.1:'+str(server.server_port);print('Operations workspace: '+url,flush=True)
    if open_browser:webbrowser.open(url)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--port',type=int,default=0);a=p.parse_args();serve(a.root,a.port)
