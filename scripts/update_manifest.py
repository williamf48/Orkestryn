"""Refresh hashes of the fixed public application payload after source changes."""
from pathlib import Path
import hashlib, json
ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'app'
def update():
    old=json.loads((APP/'bundle_manifest.json').read_text())
    hashes={}
    for name in old['file_sha256']:
        p=APP/name
        if p.is_symlink() or not p.is_file():raise ValueError('Missing or linked public payload: '+name)
        hashes[name]=hashlib.sha256(p.read_bytes()).hexdigest()
    import sys
    sys.path.insert(0,str(APP));import project
    value={'package_version':old['package_version'],'file_sha256':hashes,'source_sha256':project.source_hashes(),'repository_files_sha256':{}}
    (APP/'bundle_manifest.json').write_text(json.dumps(value,indent=2,sort_keys=True)+'\n')
    print('Updated public payload manifest: '+str(len(hashes))+' files')
if __name__=='__main__':update()
