"""Scan source files and verify the public app payload; never print credentials."""
from pathlib import Path
import argparse,json,sys
ROOT=Path(__file__).resolve().parents[1];APP=ROOT/'app'
sys.path.insert(0,str(APP))
import install_update,release_check
def entries():
    values=[]
    excluded={'.git','__pycache__','.pytest_cache','dist','artifacts','test-results','.venv','venv'}
    for p in sorted(ROOT.rglob('*')):
        rel=p.relative_to(ROOT)
        if any(x in excluded for x in rel.parts):continue
        if p.is_symlink():raise ValueError('Linked file is not publishable: '+str(rel))
        if p.is_file():values.append((rel.as_posix(),p.read_bytes(),'100644'))
    return values
def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--staged',action='store_true');args=parser.parse_args()
    install_update.payload(APP)
    release_check.package_entries(APP)
    values=release_check.staged_entries(ROOT) if args.staged else entries()
    findings=release_check.check_entries(values)
    print(json.dumps({'status':'BLOCKED' if findings else 'PASSED','files_checked':len(values),'findings':findings,'scope':'Public source hashes and heuristic credential/path checks, not exhaustive secret detection.'},indent=2))
    return bool(findings)
if __name__=='__main__':raise SystemExit(main())
