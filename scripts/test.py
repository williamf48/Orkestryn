"""Run every registered offline test class in a fresh module process."""
from pathlib import Path
import argparse,concurrent.futures,hashlib,importlib,json,os,subprocess,sys,tempfile,time,unittest
ROOT=Path(__file__).resolve().parents[1];APP=ROOT/'app';sys.path.insert(0,str(APP))
import project
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs',type=int,default=4);parser.add_argument('--output',type=Path)
    parser.add_argument('--module',choices=list(project.MODULES));args=parser.parse_args()
    if args.module:
        before=project.source_hashes()
        module=importlib.import_module(args.module)
        suite=unittest.defaultTestLoader.loadTestsFromTestCase(getattr(module,project.MODULES[args.module]))
        expected=suite.countTestCases()
        result=unittest.TextTestRunner(verbosity=2,buffer=True).run(suite)
        passed=result.wasSuccessful() and result.testsRun==expected and not result.skipped and before==project.source_hashes()
        value={'module':args.module,'passed':passed,'tests_run':result.testsRun,'expected_tests':expected,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped)}
        (args.output/(args.module+'.json')).write_text(json.dumps(value))
        return 0 if passed else 1
    if not 1<=args.jobs<=8:parser.error('--jobs must be between 1 and 8')
    output=(args.output or Path(tempfile.mkdtemp(prefix='orkestryn-tests-'))).resolve();output.mkdir(parents=True,exist_ok=True)
    before=project.source_hashes();started=time.monotonic();environment=os.environ.copy()
    for key in ('NEBIUS_API_KEY','OPENAI_API_KEY'):environment.pop(key,None)
    environment.update(PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
    def run(name):
        with (output/(name+'.log')).open('w') as log:
            result=subprocess.run([sys.executable,'-B','-S',str(Path(__file__).resolve()),'--module',name,'--output',str(output)],cwd=APP,env=environment,stdout=log,stderr=subprocess.STDOUT,timeout=300)
        p=output/(name+'.json')
        return json.loads(p.read_text()) if p.exists() else {'module':name,'passed':False,'tests_run':0,'failures':0,'errors':1,'skipped':0,'exit_code':result.returncode}
    results=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for future in concurrent.futures.as_completed([pool.submit(run,n) for n in project.MODULES]):
            value=future.result();results.append(value);print(value['module']+': '+('PASSED' if value['passed'] else 'FAILED'),flush=True)
    value={'passed':all(r['passed'] for r in results) and before==project.source_hashes(),'tests_run':sum(r['tests_run'] for r in results),'failures':sum(r['failures'] for r in results),'errors':sum(r['errors'] for r in results),'skipped':sum(r['skipped'] for r in results),'source_unchanged':before==project.source_hashes(),'model_calls':0,'elapsed_seconds':round(time.monotonic()-started,3),'module_results':sorted(results,key=lambda r:r['module'])}
    (output/'summary.json').write_text(json.dumps(value,indent=2)+'\n');print(json.dumps({k:v for k,v in value.items() if k!='module_results'}));print('Test records: '+str(output))
    return 0 if value['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
