"""Path-guard fixtures; model OS link facts only when Windows denies creation.

Native link construction is attempted first. A missing Windows symlink privilege
is recorded explicitly; the same production guards then receive modelled OS path
facts. Other filesystem errors still fail. No administrator privilege is needed.
"""
from contextlib import contextmanager,ExitStack
from pathlib import Path
from unittest import mock
OBSERVATIONS=[]

@contextmanager
def linked_path(link,target,directory=False):
    link,target=Path(link).absolute(),Path(target).absolute();native=False
    try:
        link.symlink_to(target,target_is_directory=directory);native=True
    except OSError as exc:
        if getattr(exc,'winerror',None)!=1314:raise
    if native:
        OBSERVATIONS.append({'case':link.name,'boundary':'NATIVE_OS_LINK','directory':directory})
        try:yield
        finally:
            if link.is_symlink():link.unlink()
        return
    original_resolve,original_link=Path.resolve,Path.is_symlink
    if directory:link.mkdir()
    else:link.write_text('Modelled Windows link boundary; not public content.',encoding='utf-8')
    def resolve(path,*args,**kwargs):
        path=Path(path).absolute()
        if path==link or link in path.parents:return original_resolve(target/path.relative_to(link),*args,**kwargs)
        return original_resolve(path,*args,**kwargs)
    def is_link(path):return Path(path).absolute()==link or original_link(path)
    OBSERVATIONS.append({'case':link.name,'boundary':'MODELLED_OS_LINK_FACTS; WINDOWS_PRIVILEGE_1314','directory':directory})
    try:
        with mock.patch.object(Path,'resolve',resolve),mock.patch.object(Path,'is_symlink',is_link):yield
    finally:
        if directory:link.rmdir()
        else:link.unlink()
