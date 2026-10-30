"""Run the optional real Inspect integration with provider generation forbidden."""
from __future__ import annotations
import argparse
import json
import platform
from pathlib import Path
from unittest import mock


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    import inspect_ai
    from inspect_ai import eval as inspect_eval
    from inspect_ai.model import Model
    from inspect_ai.log import read_eval_log
    import safety_environment as env
    import safety_lab as lab
    import safety_inspect as adapter
    args.output.mkdir(parents=True,exist_ok=True)
    checks=[]
    configs=[(s,p,4,False) for s in ('development','heldout')
             for p in ('unmonitored','prefix_audit','full_guard')]
    configs.append(('development','full_guard',4,True))
    tasks=[adapter.orkestryn_control(s,p,b,f) for s,p,b,f in configs]
    for task in tasks:
        for sample in task.dataset:
            assert sample.metadata is None or sample.metadata=={}
            assert sample.target==''
            assert 'private' not in env.parse_json(sample.input)
    checks.append('Inspect samples contain public context only; no target or metadata labels.')
    # A task accidentally calling an inference model or connecting to the
    # network must fail this check, including loopback network requests.
    with mock.patch.object(Model,'generate',side_effect=AssertionError('No model generation in local control adapter')), \
         mock.patch('socket.socket.connect',side_effect=AssertionError('No network in local control adapter')):
        logs=inspect_eval(tasks,display='none',log_dir=str(args.output/'logs'),
                          max_tasks=1,max_samples=1,fail_on_error=True,
                          retry_on_error=0,log_format='json')
    assert len(logs)==len(configs)
    samples=0
    for log,(split,protocol,budget,framing) in zip(logs,configs):
        assert log.status=='success',(log.status,log.error)
        assert not log.stats.model_usage
        cases=adapter.selected_cases(split,framing)
        expected=lab.score_cases(cases,[budget])
        lookup={c['case']['id']:c for c in expected['cases']}
        assert len(log.samples)==len(cases)
        for sample in log.samples:
            row=lookup[str(sample.id)]
            p=next(v for v in row['protocols'] if v['protocol']==protocol)
            score=sample.scores['state_history_outcome']
            assert score.metadata['reference']==row['reference']
            assert score.metadata['observed']==p['outcome']
            assert score.value=={'harm':int(p['outcome']['harm_occurred']),
                'main_task_success':int(p['outcome']['main_task_success']),
                'escalated':int(p['decision']=='escalate')}
            samples+=1
        reread=read_eval_log(log.location)
        assert reread.status=='success' and len(reread.samples)==len(cases)
        checks.append(f'{split}/{protocol}/framing={framing}: {len(cases)} actual Inspect samples match the independent Lab replay.')
    value={'status':'PASSED','inspect_version':inspect_ai.__version__,
        'python':platform.python_version(),'platform':platform.system(),
        'tasks':len(configs),'samples':samples,'model_calls':0,'network_connections':0,
        'checks':checks,'source_sha256':lab.source_hashes(),
        'scope':'Real installed Inspect execution with model generation and network connections forbidden. Authored candidates; no real model safety result.'}
    (args.output/'verification.json').write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in value.items() if k not in {'source_sha256','checks'}}))
    return 0


if __name__=='__main__':raise SystemExit(main())
