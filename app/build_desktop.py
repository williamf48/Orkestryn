"""Build a cross-platform Python zipapp, or a native binary on its target OS."""
from __future__ import annotations
import argparse
import ast
import hashlib
import io
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipapp
import zipfile

import install_update
import release_check


def build(source, output, native=False):
    if Path(output).is_symlink():
        raise ValueError('Choose a new regular output path.')
    source, output = Path(source).resolve(), Path(output).resolve()
    files, version = install_update.payload(source)
    if release_check.check_entries(release_check.package_entries(source)):
        raise ValueError('Review the complete public package before building.')
    if output.exists() or output.is_symlink() or source == output or source in output.parents:
        raise ValueError('Choose a new output path outside the application source.')
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, raw in sorted(files.items()):
            archive.writestr(name, raw)
    raw = payload.getvalue()
    digest = hashlib.sha256(raw).hexdigest()
    entry = files['desktop_entry.py'].decode('utf-8').replace("'__ORK_PAYLOAD_SHA256__'", repr(digest))
    with tempfile.TemporaryDirectory(prefix='Orkestryn build ') as temporary:
        staging = Path(temporary)
        (staging / '__main__.py').write_text(entry, encoding='utf-8')
        (staging / 'payload.zip').write_bytes(raw)
        if native:
            # PyInstaller bundles this interpreter. Build separately on each OS.
            modules = set()
            for name, data in files.items():
                if not name.endswith('.py'):
                    continue
                for node in ast.walk(ast.parse(data.decode('utf-8'))):
                    names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and not node.level else []
                    for module in names:
                        if module and module.split('.')[0] in sys.stdlib_module_names:
                            try:
                                if importlib.util.find_spec(module) is not None:
                                    modules.add(module)
                            except (ModuleNotFoundError, ValueError):
                                pass
            hidden = [arg for module in sorted(modules) for arg in ('--hidden-import', module)]
            result = subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile',
                '--name', output.stem, '--distpath', str(staging / 'dist'), '--workpath', str(staging / 'work'),
                '--specpath', str(staging), '--add-data', str(staging / 'payload.zip') + ':.',
                '--hidden-import', 'tkinter', '--hidden-import', 'tkinter.ttk', '--hidden-import', 'tkinter.messagebox',
                '--collect-submodules', 'http', '--collect-submodules', 'sqlite3', '--collect-submodules', 'email',
                '--hidden-import', 'ssl', '--hidden-import', 'webbrowser', '--hidden-import', 'argparse',
                '--hidden-import', 'uuid', '--hidden-import', 'secrets', '--hidden-import', 'unittest.mock',
                *hidden, str(staging / '__main__.py')], cwd=staging, check=False)
            if result.returncode:
                raise ValueError('The native build failed. Install the documented build requirements on this target OS.')
            name = output.stem + ('.exe' if sys.platform == 'win32' else '')
            (staging / 'dist' / name).replace(output)
        else:
            zipapp.create_archive(staging, output, interpreter='/usr/bin/env python3', compressed=True)
    return {'file': output.name, 'format': 'native' if native else 'python-zipapp', 'package_version': version,
            'sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'bytes': output.stat().st_size,
            'requires_python': not native, 'built_on': sys.platform, 'startup_model_calls': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output, args.native)))


if __name__ == '__main__':
    main()
