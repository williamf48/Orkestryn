#!/usr/bin/env python3
"""Restartable fixed-lease background observer; never approves service effects."""
from __future__ import annotations
import argparse,hashlib,hmac,http.client,json,secrets,sqlite3,sys,time
from http.server import HTTPServer,BaseHTTPRequestHandler
from pathlib import Path
import operations_workspace as ops
import named_access
import local_service as local
import durable_runtime as durable
import team_workspace as ws
import runtime_sessions as sessions

class Client:
    def __init__(self,path):self.path=Path(path).absolute()
    def request(self,endpoint,payload=None):
        a=sessions.read_file(self.path,'.supervisor-access.json')
        return local.http_json(a['port'],endpoint,payload,a['token'])

def step(path):
    current=ops.read(path)
    if current['live'] and current['phase'].endswith('_REQUESTING'):return current['phase']
    auth=named_access.Access(Path(path).parent)
    try:
        with ops.opened(path,auth=auth) as run:
            s=run.guard()
            if s['phase']=='MONITORING':run.observe()
            elif s['phase']=='INCIDENT_RECORDED':
                if s['live']:run.notify('recovery','The measured incident is ready. Review the one remaining paid Recovery proposal in the enabled browser workspace or request it explicitly in the terminal.')
                else:run.advance()
            elif s['phase']=='AWAITING_RECOVERY_APPROVAL':run.notify('manager','Recovery proposed bounded cleanup. Exact manager approval is required.')
            return run.state()['phase']
    finally:auth.close()

def serve(path,interval=5):
    path=Path(path).absolute()
    if path.resolve()!=path:raise ValueError('Supervisor path cannot be linked.')
    lock=sqlite3.connect(path/'.operations-supervisor-lock.sqlite3',timeout=.2);lock.execute('BEGIN EXCLUSIVE')
    value=ops.read(path)
    if value['historical_operations'] or value['lifecycle']!='LEASED' or value['expires_unix']<=time.time():raise ValueError('This mission is stopped, expired or historical.')
    token=secrets.token_urlsafe(32);stopping=[False];source=ops.source_digest();started=time.time();last=None;error=None
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def answer(self,status,data):
            raw=json.dumps(data).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def authorized(self):return self.headers.get('Host')=='127.0.0.1:'+str(self.server.server_port) and not self.headers.get('Origin') and hmac.compare_digest(self.headers.get('X-Rehearsal-Token',''),token)
        def do_GET(self):
            if not self.authorized():return self.answer(403,{'error':'Local capability required.'})
            if self.path!='/health':return self.answer(404,{'error':'Unknown endpoint.'})
            return self.answer(200,{'status':'RUNNING','run':path.name,'source_digest':source})
        def do_POST(self):
            if not self.authorized():return self.answer(403,{'error':'Local capability required.'})
            if self.path!='/stop':return self.answer(404,{'error':'Unknown endpoint.'})
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=1024:return self.answer(400,{'error':'Invalid body.'})
            request=json.loads(self.rfile.read(size));
            if request!={'run':path.name}:return self.answer(400,{'error':'Exact run required.'})
            stopping[0]=True;return self.answer(200,{'status':'STOPPING'})
    server=HTTPServer(('127.0.0.1',0),Handler);server.timeout=.2
    ws.write_json(path/'.supervisor-access.json',{'port':server.server_port,'token':token,'pid':__import__('os').getpid(),'os_identity':durable.os_process(__import__('os').getpid())['identity']});(path/'.supervisor-access.json').chmod(0o600)
    deadline=time.monotonic()+max(0,value['expires_unix']-time.time());next_tick=0
    try:
        while not stopping[0] and time.monotonic()<deadline and time.time()<value['expires_unix']:
            if ops.source_digest()!=source:error='SOURCE_CHANGED';break
            current=ops.read(path)
            if current['lifecycle']!='LEASED':break
            if time.monotonic()>=next_tick:
                try:last=step(path);error=None
                except (ValueError,OSError,sqlite3.Error,http.client.HTTPException) as exc:error=getattr(exc,'code',type(exc).__name__)
                next_tick=time.monotonic()+interval
            ws.write_json(path/'supervision.json',{'status':'RUNNING','run':path.name,'source_digest':source,'started_unix':started,'heartbeat_unix':time.time(),'expires_unix':value['expires_unix'],'last_phase':last,'error':error,'model_calls_authorized':0,
                'scope':'Background observations and scripted recommendations only. No effect approvals, live API credentials, automatic paid requests, worker replacement or lease extension.'})
            server.handle_request()
    finally:
        server.server_close();lock.close();ws.write_json(path/'supervision.json',{'status':'STOPPED','run':path.name,'started_unix':started,'stopped_unix':time.time(),'last_phase':last,'error':error,'scope':'Supervisor stopped. Original workers retain their own fixed lease.'})

def launch(path,interval=5):
    path=Path(path).absolute();value=ops.read(path)
    if type(interval) is not int or not 1<=interval<=30:raise ValueError('Use a 1-30 second monitoring interval.')
    if value['historical_operations'] or value['lifecycle']!='LEASED' or value['lease_elapsed']:raise ValueError('Cannot monitor a stopped, expired or historical mission.')
    try:
        current=Client(path).request('/health')
        if current['source_digest']!=ops.source_digest():raise ValueError('A different supervisor source is already active.')
        return {**current,'reused':True}
    except (OSError,ValueError,http.client.HTTPException):pass
    process=durable.spawn_child([durable.restart.worker_interpreter(),'-B','-S',str(Path(__file__).absolute()),'serve','--path',str(path),'--interval',str(interval)],path/'operations-supervisor.log')
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        try:
            current=Client(path).request('/health')
            if current['source_digest']==ops.source_digest():return {**current,'reused':False}
        except (ValueError,OSError,http.client.HTTPException):pass
        if process.poll() is not None:raise ValueError('Supervisor exited during startup; inspect its local log.')
        time.sleep(.05)
    raise ValueError('Supervisor startup was not confirmed.')

def stop(path):
    try:result=Client(path).request('/stop',{'run':Path(path).name})
    except (ValueError,OSError,http.client.HTTPException):return {'status':'UNREACHABLE','scope':'No saved PID is terminated. Original worker lease remains bounded.'}
    end=time.monotonic()+5
    while time.monotonic()<end:
        try:
            value=sessions.read_file(path,'supervision.json')
            if value['status']=='STOPPED':
                access=sessions.read_file(path,'.supervisor-access.json')
                if durable.native_process.wait_for_exit(access.get('pid'),access.get('os_identity'),max(0,end-time.monotonic()),observer=durable.os_process):return value
                return {'status':'EXIT_UNCONFIRMED','scope':'The observer reported stop, but native exit/file release is unconfirmed.'}
        except (ValueError,OSError):pass
        time.sleep(.05)
    return result

def status(path):
    try:
        value=sessions.read_file(path,'supervision.json');value['heartbeat_stale']=value.get('status')=='RUNNING' and time.time()-value.get('heartbeat_unix',0)>40;return value
    except (ValueError,OSError):return {'status':'NOT_STARTED'}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('serve',));p.add_argument('--path',type=Path,required=True);p.add_argument('--interval',type=int,default=5);a=p.parse_args();serve(a.path,a.interval)
