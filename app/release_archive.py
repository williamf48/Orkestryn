#!/usr/bin/env python3
"""Package only verified public manifest entries; never archive a whole workspace."""
import argparse
import hashlib
import json
from pathlib import Path
import re
from zipfile import ZipFile, ZIP_DEFLATED

import install_update
import release_check


def build(root, output):
    root=Path(root).resolve()
    files,version=install_update.payload(root)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}',version):
        raise ValueError('The package version is not a safe release tag.')
    entries=release_check.package_entries(root)
    if release_check.check_entries(entries):
        raise ValueError('The verified package needs a local publication review.')
    names={name for name,_,_ in entries}
    if not set(files).issubset(names):raise ValueError('The public manifest is incomplete.')
    output=Path(output).absolute();output.mkdir(parents=True,exist_ok=True)
    target=output/('Orkestryn-'+version+'.zip')
    # Do not overwrite a previous release or accidentally follow an output link.
    with target.open('xb') as stream:
        with ZipFile(stream,'w',ZIP_DEFLATED) as archive:
            for name,data,_ in sorted(entries):archive.writestr(name,data)
    with ZipFile(target) as archive:
        if archive.testzip() is not None or set(archive.namelist())!=names:
            raise ValueError('The release archive did not verify.')
    return target,{'file':target.name,'files':len(entries),'bytes':target.stat().st_size,
                   'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'package_version':version}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:_,record=build(Path(__file__).resolve().parent,args.output)
    except (ValueError,OSError,KeyError):
        print('Release packaging stopped. Use a complete unmodified release and a new output directory; inspect release_check.py --package.')
        return 1
    print(json.dumps(record));return 0


if __name__=='__main__':raise SystemExit(main())
