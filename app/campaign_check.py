#!/usr/bin/env python3
"""Focused native publication/campaign check; no model calls or installed edits."""
from pathlib import Path
from datetime import datetime,timezone
import argparse,json,os,platform,sys,time,unittest,uuid

CASES=['factory_publication_tests.PublicationTests',
       'dashboard.DashboardTests.test_http_frozen_team_evaluation_scores_all_cases_and_serves_guarded_case_replay']

class Tee:
    def __init__(self,*streams):self.streams=streams
    def write(self,text):
        for stream in self.streams:stream.write(text)
        return len(text)
    def flush(self):
        for stream in self.streams:stream.flush()

def main(argv=None):
    import project,install_update
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parent/'diagnostic_runs')
    args=parser.parse_args(argv)
    root=args.output.absolute()
    if root.resolve()!=root:raise ValueError('Diagnostic output cannot be linked.')
    now=datetime.now(timezone.utc);directory=root/(now.strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
    directory.mkdir(parents=True,exist_ok=False)
    record={'status':'RUNNING','created_at':now.isoformat(),'platform':platform.system(),'python_version':platform.python_version(),
            'mode':'FOCUSED PUBLICATION AND CAMPAIGN CHECK; NOT FULL ACCEPTANCE','tests_run':0,'model_calls':0,
            'native_windows':'FOCUSED_CHECK_ONLY' if platform.system()=='Windows' else 'NOT_RUN_ON_THIS_PLATFORM',
            'scope':'Original failing HTTP six-case workflow plus publication regressions. Installed source is read-only; temporary test copies are removed by the tests.'}
    started=time.monotonic()
    try:
        _,version=install_update.payload(Path(__file__).resolve().parent)
        manifest=json.loads((Path(__file__).resolve().parent/'bundle_manifest.json').read_text(encoding='utf-8'))
        before=project.source_hashes()
        if before!=manifest['source_sha256']:raise ValueError('Source hashes differ from the complete installed package; no tests started.')
        record.update(package_version=version,source_sha256=before,package_verification='MATCH')
        # Keys are not needed by these fixed offline checks and never enter their
        # child processes. This affects only this invocation's environment.
        for name in ('NEBIUS_API_KEY','OPENAI_API_KEY'):os.environ.pop(name,None)
        print('ORKESTRYN - FOCUSED WINDOWS CAMPAIGN CHECK',flush=True)
        print('Package: '+version+' | source: MATCH | zero provider requests',flush=True)
        with (directory/'console.txt').open('w',encoding='utf-8') as log:
            result=unittest.TextTestRunner(stream=Tee(sys.stdout,log),verbosity=2,buffer=True).run(unittest.defaultTestLoader.loadTestsFromNames(CASES))
        unchanged=before==project.source_hashes()
        record.update(status='PASSED' if result.wasSuccessful() and unchanged else 'FAILED',tests_run=result.testsRun,
            failure_count=len(result.failures),error_count=len(result.errors),skipped_count=len(result.skipped),source_unchanged=unchanged,
            failure_details=[{'test':test.id(),'traceback':detail} for test,detail in result.failures],
            error_details=[{'test':test.id(),'traceback':detail} for test,detail in result.errors])
    except (OSError,ValueError,KeyError) as error:
        record.update(status='FAILED_BEFORE_TESTS',error=type(error).__name__+': '+str(error))
    record['elapsed_seconds']=round(time.monotonic()-started,3)
    (directory/'summary.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    print('Campaign check: '+record['status']+' | '+str(directory),flush=True)
    print('This focused result does not certify the complete native Windows acceptance suite.',flush=True)
    return 0 if record['status']=='PASSED' else 1

if __name__=='__main__':raise SystemExit(main())
