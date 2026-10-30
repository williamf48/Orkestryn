"""Session-scoped Nebius keys in process memory; no SDK, disk key or retries."""
from __future__ import annotations
import copy
import hashlib
import http.client
import json
import ssl
import sqlite3
import threading
import time
from types import SimpleNamespace

import named_access
import operations_proposals
import operations_workspace as ops


def checked_key(value):
    if not isinstance(value, str):raise ValueError('Enter a Nebius API key in the NVIDIA key field.')
    value = value.strip()
    if not 16 <= len(value) <= 4096 or any(not 33 <= ord(c) <= 126 for c in value):
        raise ValueError('Use a single valid API key without internal whitespace.')
    return value


class ProposalError(ValueError):
    pass


class RoleClient:
    live = True
    mode = 'LIVE NVIDIA VIA NEBIUS TOKEN FACTORY; SESSION KEY'
    def __init__(self, role, key):
        if role not in ops.ROLES:raise ValueError('Unknown registered NVIDIA role.')
        self.role = role
        self._key = checked_key(key)
        self.system_prompt = ops.PROMPTS[role]
        self.request_settings = ops.factory.inference.inference_settings(8192, True)

    def ask(self, context):
        settings = copy.deepcopy(self.request_settings)
        settings.update(settings.pop('extra_body', {}))
        body = json.dumps({'model':ops.factory.inference.MODEL, **settings,
                          'messages':[{'role':'system','content':self.system_prompt},
                                      {'role':'user','content':ops.sim.canonical(context)}]},
                         ensure_ascii=False, allow_nan=False).encode('utf-8')
        connection = None
        started = time.monotonic()
        try:
            # Fixed TLS destination; no redirects, proxy settings or SDK retries.
            connection = http.client.HTTPSConnection('api.tokenfactory.nebius.com',
                             timeout=120, context=ssl.create_default_context())
            connection.request('POST', '/v1/chat/completions', body=body,
                headers={'Authorization':'Bearer ' + self._key,'Content-Type':'application/json'})
            response = connection.getresponse()
            if response.status != 200:raise ProposalError('NVIDIA_HTTP_' + str(response.status) + '_NO_RETRY')
            raw = response.read(262145)
            if len(raw) > 262144:raise ProposalError('NVIDIA_RESPONSE_TOO_LARGE_NO_RETRY')
            result = json.loads(raw)
            choice = result['choices'][0];message = choice['message'];usage = result.get('usage') or {}
            if (not isinstance(message,dict) or message.get('content') is not None and not isinstance(message['content'],str)
                    or not isinstance(choice.get('finish_reason'),str)):
                raise ProposalError('NVIDIA_REPLY_UNCONFIRMED_NO_RETRY')
            content, reasoning, removed = ops.factory.inference.visible_answer(SimpleNamespace(
                **{k:v for k,v in message.items() if k in {'content','reasoning_content','refusal'}}))
            def tokens(value):return value if type(value) is int and value >= 0 else None
            def label(value):return ops.factory.inference.redact(value,self._key)[:160] if isinstance(value,str) else None
            metadata = {'source':self.mode,'model':label(result.get('model')),'response_id':label(result.get('id')),
                'finish_reason':label(choice['finish_reason']),'refused':bool(message.get('refusal')),
                'input_tokens':tokens(usage.get('prompt_tokens')),'output_tokens':tokens(usage.get('completion_tokens')),
                'total_tokens':tokens(usage.get('total_tokens')),
                'reasoning_tokens':tokens((usage.get('completion_tokens_details') or {}).get('reasoning_tokens')),
                'reasoning_content_present':reasoning,'marked_thinking_content_removed':removed,
                'latency_seconds':round(time.monotonic()-started,3)}
            return ops.factory.inference.redact(content,self._key), ops.factory.inference.redact(metadata,self._key)
        except ProposalError:
            raise
        except (ValueError, KeyError, IndexError, TypeError, AttributeError, OSError, http.client.HTTPException):
            raise ProposalError('NVIDIA_REQUEST_UNCONFIRMED_NO_RETRY') from None
        finally:
            if connection:connection.close()


def providers(key):
    key = checked_key(key)
    return {role:RoleClient(role,key) for role in ops.ROLES}


def session_id(token):return hashlib.sha256(token.encode('utf-8')).hexdigest()


class Registry:
    """Never persisted or included in snapshots. An active browser owns a key."""
    def __init__(self,root):
        self.root=root;self.entries={};self.history={};self.retired=[];self.lock=threading.RLock()
        self.stop=threading.Event();self.cleaner=None

    def attach(self,run,token,principal,key,auth):
        auth.require(principal,'manager')
        actual,_=auth.session(token)
        if actual['subject']!=principal['subject']:raise named_access.AccessDenied('Use your signed-in manager session.')
        sid=session_id(token)
        expires=auth.db.execute('SELECT expires FROM sessions WHERE token=?',(sid,)).fetchone()[0]
        saved=ops.read(self.root/run)
        if saved['operations'].get('browser_access')!={'owner':principal['subject'],'kind':'SESSION_KEY'}:
            raise named_access.AccessDenied('Use this manager session for its registered key workflow.')
        if saved['lifecycle']!='LEASED' or saved['lease_elapsed'] or saved['historical_operations']:
            raise ValueError('Connect a key only to an active current workflow.')
        expires=min(expires,saved['expires_unix'])
        enabled=providers(key)
        subject=principal['subject']
        def authorize(store,user):
            row=store.db.execute('SELECT user_id,expires FROM sessions WHERE token=?',(sid,)).fetchone()
            if not row or row[0]!=subject or row[1]<=time.time() or user['subject']!=subject:
                raise named_access.AccessDenied('Sign in again and reconnect NVIDIA access for this run.')
            store.require(user,'manager')
        job=operations_proposals.Jobs(self.root,enabled,authorize=authorize)
        self.reap()
        with self.lock:
            if self.stop.is_set():job.close();raise ValueError('The workspace is closing.')
            if len(self.entries)>=32:job.close();raise ValueError('Disconnect an existing NVIDIA session before adding another.')
            prior=self.entries.get((sid,run))
            if prior and any(v['status'] in {'QUEUED','RUNNING'} for v in prior['jobs'].snapshot().values()):
                job.close();raise ValueError('A proposal is running. Inspect its saved outcome before reconnecting.')
            if prior:self._retire(prior)
            self.entries[(sid,run)]={'subject':subject,'expires':expires,'jobs':job}
            if self.cleaner is None:
                self.cleaner=threading.Thread(target=self._clean,daemon=True);self.cleaner.start()
        return job

    def _retire(self,entry):
        # A request already in flight can retain its local key until it finishes.
        # Queued work rechecks the session before recording its paid attempt.
        entry['jobs'].close();entry['jobs'].providers.clear();self.retired.append(entry['jobs'])

    def reap(self):
        auth=named_access.Access(self.root)
        try:
            now=time.time();active={sid:uid for sid,uid in auth.db.execute('SELECT token,user_id FROM sessions WHERE expires>?',(now,))}
            managers={uid for uid,roles in auth.db.execute('SELECT id,roles FROM users WHERE active=1') if 'manager' in json.loads(roles)}
        finally:auth.close()
        with self.lock:
            for key,entry in list(self.entries.items()):
                if entry['expires']<=now or active.get(key[0])!=entry['subject'] or entry['subject'] not in managers:
                    self.entries.pop(key);self._retire(entry)

    def _clean(self):
        while not self.stop.wait(15):
            try:self.reap()
            except (ValueError,OSError,sqlite3.Error):pass

    def get(self,run,token,principal):
        self.reap()
        with self.lock:
            entry=self.entries.get((session_id(token),run))
            if not entry or entry['subject']!=principal['subject']:
                raise ValueError('Reconnect NVIDIA access for this run using its original manager account.')
            return entry['jobs']

    def status(self,run,token,principal):
        with self.lock:
            entry=self.entries.get((session_id(token),run))
            return bool(entry and entry['subject']==principal['subject'] and entry['expires']>time.time())

    def snapshot(self):
        with self.lock:
            result=copy.deepcopy(self.history)
            for job in list(self.retired):
                snapshot=job.snapshot();result.update(snapshot)
                if not any(v['status'] in {'QUEUED','RUNNING'} for v in snapshot.values()):
                    self.history.update(snapshot);self.retired.remove(job)
            for entry in self.entries.values():result.update(entry['jobs'].snapshot())
            return result

    def forget(self,*,token=None,run=None):
        sid=session_id(token) if token is not None else None
        with self.lock:
            for key,entry in list(self.entries.items()):
                if sid is not None and key[0]==sid or run is not None and key[1]==run:
                    self.entries.pop(key);self._retire(entry)

    def close(self):
        self.stop.set()
        if self.cleaner:self.cleaner.join(2)
        with self.lock:
            entries=list(self.entries.values());self.entries.clear()
            for entry in entries:self._retire(entry)
