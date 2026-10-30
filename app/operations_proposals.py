"""Explicit asynchronous proposal attempts; durable attempt authority stays in Mission."""
from __future__ import annotations
import copy
import threading
import time
import uuid
import named_access
import operations_workspace as ops

class Jobs:
    def __init__(self,root,providers=None,*,authorize=None):
        self.root=root;self.providers=providers or {};self.settings={};self.lock=threading.Lock();self.jobs={};self.threads=[];self.closed=False
        self.authorize=authorize
        if self.providers:
            if set(self.providers)!=set(ops.ROLES):raise ValueError('Enable all three registered proposal roles.')
            self.settings=copy.deepcopy(self.providers['factory'].request_settings)
            if any(not p.live or p.system_prompt!=ops.PROMPTS[r] or p.request_settings!=self.settings for r,p in self.providers.items()):
                raise ValueError('Live roles must use distinct registered prompts and identical frozen settings.')
    @property
    def enabled(self):return bool(self.providers)
    def snapshot(self):
        with self.lock:return copy.deepcopy(self.jobs)
    def submit(self,path,role,digest,principal,auth):
        auth.require(principal,'manager')
        if not self.enabled or role not in self.providers:raise ValueError('Live proposals are not enabled in this workspace.')
        with self.lock:
            if self.closed:raise ValueError('The workspace is closing.')
            prior=self.jobs.get(path.name)
            if prior and prior['role']==role and prior['digest']==digest and prior['status'] in {'QUEUED','RUNNING','FINISHED'}:
                return copy.deepcopy(prior)
            ops.sim.require(not any(j['status'] in {'QUEUED','RUNNING'} for j in self.jobs.values()),
                'PROPOSAL_BUSY','One live proposal is already running. Inspect its saved status before starting another.')
            with ops.opened(path,auth=auth) as run:
                proposal=run.proposal_review(role)
                ops.sim.require(proposal['digest']==digest,'STALE_PROPOSAL_REVIEW','The proposal context changed. Open a fresh review.')
                if proposal['settings']!=self.settings:raise ValueError('Restart the workspace with this workflow original proposal settings.')
            job={'id':uuid.uuid4().hex,'run':path.name,'role':role,'digest':digest,'status':'QUEUED','submitted_unix':time.time()}
            self.jobs[path.name]=job
            worker=threading.Thread(target=self._run,args=(path,copy.deepcopy(principal),job['id']),daemon=True)
            self.threads=[t for t in self.threads if t.is_alive()];self.threads.append(worker)
            try:worker.start()
            except Exception:
                self.threads.remove(worker);job.update(status='FAILED',error_code='WORKER_NOT_STARTED')
                raise ValueError('The proposal worker did not start. No provider request was made.') from None
            return copy.deepcopy(job)
    def _run(self,path,principal,job_id):
        auth=None
        try:
            with self.lock:
                job=self.jobs[path.name]
                if self.closed:job.update(status='FAILED',error_code='WORKSPACE_CLOSED');return
                job['status']='RUNNING';role,digest=job['role'],job['digest']
            auth=named_access.Access(self.root)
            with ops.opened(path,auth=auth) as run:
                if self.authorize:self.authorize(auth,principal)
                result=run.request_proposal(role,digest,principal,self.providers[role])
            with self.lock:self.jobs[path.name].update(status='FINISHED',phase=result['phase'],finished_unix=time.time())
        except Exception as exc:
            # Provider exception text may contain request details. Public status uses codes only.
            with self.lock:
                if self.jobs.get(path.name,{}).get('id')==job_id:
                    self.jobs[path.name].update(status='FAILED',error_code=getattr(exc,'code',type(exc).__name__),
                        message='Proposal stopped. Inspect the saved attempt before opening another review. No automatic retry was made.',finished_unix=time.time())
        finally:
            if auth:auth.close()
    def close(self):
        with self.lock:self.closed=True
        # In-flight network requests cannot be declared cancelled. Their durable
        # started/received record governs recovery if the process exits.
        deadline=time.monotonic()+2
        for thread in self.threads:thread.join(max(0,deadline-time.monotonic()))
