"""Build a source ZIP and portable app from verified public source."""
from pathlib import Path
import argparse,hashlib,json,subprocess,sys,zipfile
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'app'))
import build_desktop,release_check
from check_repository import entries
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,default=ROOT/'dist');args=p.parse_args()
    result=subprocess.run([sys.executable,str(ROOT/'scripts/check_repository.py')],cwd=ROOT)
    if result.returncode:return result.returncode
    output=args.output.resolve()
    if output==ROOT or output==ROOT/'app' or ROOT/'app' in output.parents:raise ValueError('Build outside application source.')
    output.mkdir(parents=True,exist_ok=True)
    version=json.loads((ROOT/'app/bundle_manifest.json').read_text())['package_version']
    archive=output/('Orkestryn-'+version+'-source.zip');portable=output/'Orkestryn.pyz'
    if archive.exists() or portable.exists():raise ValueError('Choose a new output directory; existing artifacts are preserved.')
    public=entries()
    if release_check.check_entries(public):raise ValueError('Public source scan failed.')
    built=build_desktop.build(ROOT/'app',portable)
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data,_ in public:z.writestr('orkestryn/'+name,data)
    records=[{'file':f.name,'bytes':f.stat().st_size,'sha256':hashlib.sha256(f.read_bytes()).hexdigest()} for f in [archive,portable]]
    (output/'SHA256SUMS.txt').write_text(''.join(r['sha256']+'  '+r['file']+'\n' for r in records))
    print(json.dumps({'status':'BUILT','package_version':version,'artifacts':records,'model_calls':0},indent=2))
    return 0
if __name__=='__main__':raise SystemExit(main())
