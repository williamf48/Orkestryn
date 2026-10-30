"""Typed DAG workloads and bounded order search for 1-32 infrastructure requests."""
from __future__ import annotations
import copy,itertools,time
import change_coordinator as c
import simulator as sim
VERSION='agent-rehearsal-workflow-planner-v1'
MAX_REQUESTS=32
MAX_EVALUATIONS=96

def request(actor,value):
    if not isinstance(value,dict) or set(value)!={'id','tool','parameters','priority','after'}:raise ValueError('Each request needs id, tool, parameters, priority and after.')
    base=c.validate_intent(actor,{k:v for k,v in value.items() if k!='after'});parents=value['after']
    if not isinstance(parents,list) or len(parents)>MAX_REQUESTS or any(not isinstance(x,str) for x in parents) or len(parents)!=len(set(parents)):raise ValueError('after must contain distinct request IDs.')
    for key in parents:c.component(key)
    if base['id'] in parents:raise ValueError('A request cannot depend on itself.')
    return {**base,'after':list(parents)}

def topology(intents,preference=None):
    by_id={i['id']:i for i in intents}
    if len(by_id)!=len(intents):raise ValueError('Request IDs must be unique.')
    for i in intents:
        if not set(i.get('after',[])).issubset(by_id):raise ValueError('Unknown prerequisite ID.')
    rank={key:n for n,key in enumerate(preference or list(by_id))};done=[]
    while len(done)<len(intents):
        ready=[i for i in by_id if i not in done and set(by_id[i].get('after',[])).issubset(done)]
        if not ready:raise ValueError('The request dependency graph contains a cycle.')
        done.append(min(ready,key=lambda key:(rank.get(key,999),key)))
    return done

def validate_workflow(v):
    if not isinstance(v,dict) or set(v)!={'version','goal','requests'} or v['version']!=VERSION or not isinstance(v['goal'],str) or not 1<=len(v['goal'].strip())<=600:raise ValueError('Use the registered workflow version, goal and requests contract.')
    if not isinstance(v['requests'],list) or not 1<=len(v['requests'])<=MAX_REQUESTS:raise ValueError('Use 1-32 typed requests.')
    items=[]
    for value in v['requests']:
        if not isinstance(value,dict) or 'actor' not in value:raise ValueError('A trusted caller actor is required for each request slot.')
        items.append(request(value['actor'],{k:x for k,x in value.items() if k!='actor'}))
    topology(items)
    if sum(i['tool']=='set_capacity_limit' for i in items)>1:raise ValueError('One cost-limit request per registered workload.')
    jobs=[i['parameters']['dependency_id'] for i in items if i['tool']=='add_dependency']
    if len(jobs)!=len(set(jobs)):raise ValueError('Dependency job identities must be distinct.')
    return copy.deepcopy(v)

def profile(name='balanced'):
    if name not in {'balanced','bulk','dependency-chain','deployment-heavy'}:raise ValueError('Unknown workload profile.')
    rows=[{'actor':actor,**value,'after':[]} for actor,value in c.default_intents()]
    rows[3]['parameters']['dependency_id']='C-mission-job'
    if name=='bulk':
        rows=[rows[1],rows[2]]
        rows += [{'actor':'A','id':'reserve-'+str(n),'tool':'reserve_capacity','parameters':{'units':1},'priority':2,'after':[]} for n in range(8)]
        rows += [{'actor':'C','id':'job-'+str(n),'tool':'add_dependency','parameters':{'dependency_id':'customer-job-'+str(n)},'priority':3,'after':[]} for n in range(18)]
    if name=='dependency-chain':
        rows=[rows[0],rows[1],rows[4]]+[{'actor':'C','id':'job-'+str(n),'tool':'add_dependency','parameters':{'dependency_id':'pipeline-stage-'+str(n)},'priority':3,'after':['job-'+str(n-1)] if n else []} for n in range(10)]
        rows += [{**c.default_intents()[2][1],'actor':'C','after':['job-9']}]
    if name=='deployment-heavy':
        rows[0]['priority']=5;rows[1]['priority']=1
    return validate_workflow({'version':VERSION,'goal':'Coordinate the '+name+' inference workload while preserving other owners.','requests':rows})

def evaluate(state,intents,order,weights=None,cache=None):
    by_id={i['id']:i for i in intents}
    if not isinstance(order,list) or any(not isinstance(k,str) for k in order) or len(order)!=len(by_id) or len(set(order))!=len(order) or set(order)!=set(by_id):raise ValueError('Order every current request ID exactly once.')
    current=copy.deepcopy(state);accepted=[];actions=[];rows=[];frames=[copy.deepcopy(state)];route_taken=False
    for key in order:
        i=by_id[key];before=copy.deepcopy(current)
        if not set(i.get('after',[])).issubset(accepted):result={'status':'WAIT','code':'PREREQUISITE_NOT_SELECTED','reason':'Every prerequisite must have a confirmed selected action earlier in this plan.'}
        else:
            memo=(sim.digest(current),key,route_taken);out=cache.get(memo) if cache is not None else None
            if out is None:
                out=c.trial(current,i,route_taken)
                if cache is not None:cache[memo]=out
            current=copy.deepcopy(out['state']);result=out['result']
        selected=result['status']=='SUCCEEDED';rows.append({'id':key,'status':'SELECTED' if selected else 'WAIT','code':result['code'],'reason':result['reason']})
        if selected:
            accepted.append(key);route_taken=route_taken or i['tool']=='set_route'
            actions.append({'intent_id':key,'actor':i['actor'],'run_id':i['run_id'],'tool':i['tool'],'args':c.arguments(i,before),
                'approval_id':'AP-'+i['run_id'],'action_key':c.action_key(i),'before_digest':sim.digest(before),'after_digest':sim.digest(current)})
        frames.append(copy.deepcopy(current))
    if not c.reference.audit_frames(frames):raise ValueError('Independent frame invariant failure.')
    weights=weights or {i['id']:i['priority'] for i in intents};new=c.reference.commitments(current)-c.reference.commitments(state)
    waits=[{**row,'retry_now_code':row['code'],'next_step':'Complete or separately review the named prerequisite.' if row['code']=='PREREQUISITE_NOT_SELECTED' else c.next_step(row['code']),
            'blocking_facts':{**c.blocking_facts(current,by_id[row['id']]),'prerequisites':by_id[row['id']].get('after',[])}} for row in rows if row['status']=='WAIT']
    return {'order':order,'accepted':accepted,'waiting':waits,'actions':actions,'objective':[sum(weights[k] for k in accepted),len(accepted),-max(0,new)],
        'committed_units':c.reference.commitments(current),'capacity_limit':current['capacity_limit'],'final_state':current,'independent_frames_checked':len(frames),'reference_agrees':True}

def plan(state,intents,at=None):
    if not 1<=len(intents)<=MAX_REQUESTS:raise ValueError('Use 1-32 requests.')
    topology(intents);at=time.time() if at is None else at
    weights={i['id']:i['priority']+min(2,max(0,int((at-i.get('submitted_unix',at))//60))) for i in intents}
    ids=[i['id'] for i in intents];cache={};seen={}
    def score(order):
        key=tuple(order)
        if key not in seen:seen[key]=evaluate(state,intents,list(order),weights,cache)
        return seen[key]
    fifo=score(ids)
    if len(ids)<=6:
        for order in itertools.permutations(sorted(ids)):score(order)
        exact=True;algorithm='EXHAUSTIVE';budget=720
    else:
        exact=False;algorithm='BOUNDED_ORDER_SEARCH';budget=MAX_EVALUATIONS;by={i['id']:i for i in intents}
        preferences=[ids,sorted(ids,key=lambda k:(-weights[k],k)),sorted(ids,key=lambda k:(by[k]['tool']!='set_capacity_limit',-weights[k],k)),
            sorted(ids,key=lambda k:(by[k]['tool']!='reserve_capacity',by[k]['parameters'].get('units',0),-weights[k],k)),list(reversed(ids))]
        for order in preferences:score(topology(intents,order))
        frontier=list(seen)
        while frontier and len(seen)<budget:
            frontier=sorted(frontier,key=lambda key:(tuple(-v for v in seen[key]['objective']),key))[:8];new=[]
            for key in frontier:
                for pos in range(len(ids)-1):
                    order=list(key);order[pos],order[pos+1]=order[pos+1],order[pos];order=topology(intents,order);candidate=tuple(order)
                    if candidate not in seen:score(order);new.append(candidate)
                    if len(seen)>=budget:break
                if len(seen)>=budget:break
            frontier=new
    best=min(seen.values(),key=lambda o:(tuple(-v for v in o['objective']),o['order']))
    return {'selected':best,'fifo':fifo,'orders_compared':len(seen),'weights':weights,'conflicts':c.conflicts(intents),'planned_unix':at,
        'priority_gain_over_fifo':best['objective'][0]-fifo['objective'][0],'additional_requests_over_fifo':len(best['accepted'])-len(fifo['accepted']),
        'algorithm':algorithm,'global_optimum_proven':exact,'evaluation_budget':budget,'budget_exhausted':not exact and len(seen)>=budget,
        'objective_policy':'Priority plus capped age bonus, then count, then fewer new commitments. DAG prerequisites must be selected first. Large batches use bounded search, with no global optimality claim.'}
