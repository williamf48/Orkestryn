"""Bootstrap for the single-file distribution. Built by build_desktop.py."""
from __future__ import annotations
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import re
import shutil
import stat
import sys
import tempfile
import zipfile

PAYLOAD_SHA256 = '__ORK_PAYLOAD_SHA256__'
WORKER_FILES = {'local_service.py', 'durable_runtime.py', 'restart_checker.py', 'operations_supervisor.py'}


def configure_runtime():
    sys.dont_write_bytecode = True
    if getattr(sys, 'frozen', False):
        # Windows workers must use this bundled interpreter rather than a build
        # machine's Python path. The historical worker source stays unchanged.
        sys._base_executable = sys.executable


def private_root():
    if os.name == 'nt':
        value = os.environ.get('LOCALAPPDATA')
        return (Path(value) if value and Path(value).is_absolute() else Path.home() / 'AppData' / 'Local') / 'Orkestryn'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'Orkestryn'
    value = os.environ.get('XDG_DATA_HOME')
    return (Path(value) if value and Path(value).is_absolute() else Path.home() / '.local' / 'share') / 'Orkestryn'


def checked_directory(path):
    path = Path(os.path.abspath(path))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Use a private data directory without symbolic links.')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError('The private data directory is unavailable.')
    return path


def payload_bytes():
    if getattr(sys, 'frozen', False):
        return (Path(sys._MEIPASS) / 'payload.zip').read_bytes()
    with zipfile.ZipFile(Path(sys.argv[0]).absolute()) as archive:
        return archive.read('payload.zip')


def unpack(raw, cache, expected):
    """Extract a pinned, flat, complete public payload, never private run data."""
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise ValueError('The application payload failed its integrity check. Download a fresh copy.')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if not 1 <= len(entries) <= 1000 or sum(i.file_size for i in entries) > 64 * 1024 * 1024:
            raise ValueError('The application payload is outside the supported size.')
        names = [i.filename for i in entries]
        if len({n.casefold() for n in names}) != len(names) or any(not re.fullmatch(r'[A-Za-z0-9_.-]{1,180}', n) or n in {'.', '..'} for n in names):
            raise ValueError('Use a complete flat application payload.')
        if any(stat.S_ISLNK(i.external_attr >> 16) or i.is_dir() for i in entries):
            raise ValueError('Links and directories are not application files.')
        files = {i.filename: archive.read(i) for i in entries}
    manifest = json.loads(files['bundle_manifest.json'])
    hashes = manifest['file_sha256']
    if set(files) != set(hashes) | {'bundle_manifest.json'}:
        raise ValueError('The application inventory is incomplete.')
    if any(hashlib.sha256(files[n]).hexdigest() != sha for n, sha in hashes.items()):
        raise ValueError('An application file failed its integrity check.')
    cache = checked_directory(cache)
    target = cache / actual
    if target.is_symlink():
        raise ValueError('The application cache must not be a link.')
    if target.exists():
        if not target.is_dir() or {p.name for p in target.iterdir()} != set(files) or any((target / n).is_symlink() or not (target / n).is_file()
                                    or (target / n).read_bytes() != data for n, data in files.items()):
            raise ValueError('The cached application was changed. Choose another private cache directory.')
        return target
    temporary = Path(tempfile.mkdtemp(prefix='extract-', dir=cache))
    try:
        for name, data in files.items():
            with (temporary / name).open('xb') as stream:
                stream.write(data)
        try:
            temporary.rename(target)
        except FileExistsError:
            return unpack(raw, cache, expected)
        return target
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def worker_dispatch(root, argv):
    """Frozen executables also host only these verified internal worker scripts."""
    args = list(argv)
    while args and args[0] in {'-u', '-B', '-S'}:
        args.pop(0)
    if not args:
        raise ValueError('An internal worker script is required.')
    script = Path(args.pop(0)).resolve()
    if script.parent != root.resolve() or script.name not in WORKER_FILES:
        raise ValueError('Only bundled internal worker scripts can use this executable.')
    sys.argv = [str(script), *args]
    runpy.run_path(str(script), run_name='__main__')
    return 0


def main():
    try:
        if sys.version_info < (3, 10):
            raise ValueError('Orkestryn requires Python 3.10 or newer.')
        root = unpack(payload_bytes(), private_root() / 'code', PAYLOAD_SHA256)
        sys.path.insert(0, str(root))
        configure_runtime()
        os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
        if getattr(sys, 'frozen', False) and sys.argv[1:2] and sys.argv[1].startswith('-') and sys.argv[1] in {'-u', '-B', '-S'}:
            return worker_dispatch(root, sys.argv[1:])
        import desktop_launcher
        return desktop_launcher.main()
    except (ValueError, OSError, KeyError, zipfile.BadZipFile):
        print('Orkestryn could not open its verified application files. Use a fresh complete download and a writable private data directory.', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
