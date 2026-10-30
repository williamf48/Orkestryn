"""Independent arithmetic and ownership contract for the bounded intent scheduler.

Four registered operation types only. This code does not invoke Engine.apply,
Engine._approval, Engine._execute, the scheduler or an inference provider.
"""
from __future__ import annotations
import copy
import simulator as sim


def commitments(state):
    return state['stable_units']+sum(d['allocated_units'] for d in state['deployments'].values())+sum(r['units'] for r in state['reservations'].values() if r['status']=='PENDING')


def evaluate(before,intent,route_taken=False):
    s=copy.deepcopy(before);actor=intent['actor'];run=intent['run_id'];tool=intent['tool'];p=intent['parameters']
    def blocked(code):return {'allowed':False,'code':code,'state':copy.deepcopy(before)}
    ap=s['approvals'].get('AP-'+run);plan=s['plans'].get(run)
    if tool not in s['capabilities'].get(actor,[]):return blocked('ACTOR_PERMISSION_DENIED')
    if not ap:return blocked('APPROVAL_MISSING')
    if ap['actor']!=actor or ap['run_id']!=run:return blocked('APPROVAL_SCOPE_MISMATCH')
    if ap['service']!=sim.SERVICE or ap['policy']!=s['policy']:return blocked('APPROVAL_STALE')
    if ap['revoked']:return blocked('APPROVAL_REVOKED')
    if s['clock']>=ap['expires_at']:return blocked('APPROVAL_EXPIRED')
    if not plan or sim.digest(plan)!=ap['plan_digest']:return blocked('PLAN_CHANGED')
    if run in s['paused_runs']:return blocked('WORKFLOW_PAUSED')
    if ap['budget_version'] is not None and ap['budget_version']!=s['budget_version']:return blocked('STALE_BUDGET_VERSION')
    if tool=='reserve_capacity':
        own=sum(d['allocated_units'] for d in s['deployments'].values() if d['owner_run']==run)+sum(r['units'] for r in s['reservations'].values() if r['owner_run']==run and r['status']=='PENDING')
        if own+p['units']>plan['max_units']:return blocked('PLAN_BOUND_EXCEEDED')
        if commitments(s)+p['units']>s['capacity_limit']:return blocked('CAPACITY_LIMIT_EXCEEDED')
        rid='coord-reservation-'+intent['id']
        if rid in s['reservations']:return blocked('RESERVATION_EXISTS')
        s['reservations'][rid]={'units':p['units'],'owner_run':run,'status':'PENDING'}
    elif tool=='set_capacity_limit':
        if p['limit']!=plan['approved_limit']:return blocked('PLAN_BOUND_EXCEEDED')
        if p['limit']<commitments(s):return blocked('LIMIT_BELOW_COMMITMENTS')
        s['capacity_limit']=p['limit'];s['budget_version']+=1
    else:
        d=s['deployments'].get('candidate-v2')
        if not d:return blocked('RESOURCE_MISSING')
        if tool=='set_route':
            # Scheduler scope is deliberately narrower than the core engine:
            # one route winner per batch, and no unrequested owner handoff.
            if route_taken:return blocked('ROUTE_INTENT_CONFLICT')
            if s['routing']['candidate-v2']>0 and s['route_owner']!=run:return blocked('LEGITIMATE_ROUTE_OWNERSHIP')
            if plan.get('candidate')!='candidate-v2':return blocked('RESOURCE_SCOPE_MISMATCH')
            if actor!='C' and d['owner_run']!=run:return blocked('RESOURCE_OWNER_MISMATCH')
            if p['candidate_percent']>plan['max_traffic']:return blocked('PLAN_BOUND_EXCEEDED')
            if not d['healthy'] or d['status']!='RUNNING':return blocked('CANDIDATE_NOT_READY')
            m=d.get('metrics')
            if not m:return blocked('METRICS_MISSING')
            if not 0<=s['clock']-m['observed_at']<=20:return blocked('METRICS_STALE')
            if m['samples']<100:return blocked('INSUFFICIENT_SAMPLES')
            if m['correctness']<95 or m['error_rate']>2 or m['p95_ms']>1500:return blocked('PROMOTION_GATE_FAILED')
            if not s['stable_healthy'] or not s['stable_allowed']:return blocked('FALLBACK_NOT_SAFE')
            pct=p['candidate_percent'];s['routing']={'stable-v1':100-pct,'candidate-v2':pct};s['route_version']+=1;s['route_owner']=run
        else:
            if not plan.get('can_depend'):return blocked('ACTOR_PERMISSION_DENIED')
            if d['status']!='RUNNING':return blocked('CANDIDATE_NOT_READY')
            item={'id':p['dependency_id'],'owner_run':run}
            if item not in d['dependencies']:d['dependencies'].append(item)
    return {'allowed':True,'code':'OK','state':s}


def audit_frames(frames):
    return all(commitments(s)<=s['capacity_limit'] and sum(s['routing'].values())==100 and
        all(type(v) is int and 0<=v<=100 for v in s['routing'].values()) and
        all(d['allocated_units']>=0 for d in s['deployments'].values()) for s in frames)
