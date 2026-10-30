#!/usr/bin/env python3
"""Check a release payload or the exact Git index for common publication mistakes.

This is a local, heuristic check, not a guarantee that a repository contains no
sensitive data. It never reads environment variables or prints matching values.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
PRIVATE_PARTS = {'.env', '.venv', 'venv', '.control_access', 'update_backups',
                 '__pycache__', '.pytest_cache', '.git', 'node_modules', 'evidence_exports', 'judge_access_private',
                 'credentials.json', 'judge_credentials_private.txt', 'devpost_testing_private.txt',
                 'hosting.env', 'readme_private.txt', 'relay_connection_private.json', 'relay_owner_private.txt',
                 'judge_evaluation_private', '.safety_lab', '.research-venv', 'logs', 'research_check'}
PRIVATE_SUFFIXES = ('.sqlite3', '.sqlite3-wal', '.sqlite3-shm', '.sqlite', '.db',
                    '.db-wal', '.db-shm', '.log', '.zip', '.key', '.pem', '.p12', '.pfx')
TOKEN_PATTERNS = (
    re.compile(r'(?<![A-Za-z0-9])arp_[A-Za-z0-9_-]{43}'),
    re.compile(r'(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{24,}'),
    re.compile(r'(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{30,}'),
    re.compile(r'(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{30,}'),
    re.compile(r'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----'),
)
KEY_LITERAL = re.compile(r'''["']?(?:NEBIUS_API_KEY|OPENAI_API_KEY|api_key)["']?\s*[:=]\s*(["'])([^"'\r\n]{32,})\1''', re.I)


def path_problem(name):
    if not isinstance(name, str) or not name or '\\' in name or any(ord(c) < 32 for c in name):
        return 'Invalid repository path.'
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or ':' in name or str(path) != name:
        return 'Invalid repository path.'
    parts = [p.lower() for p in path.parts]
    if any(p in PRIVATE_PARTS or p.startswith('.env.') or p.endswith('_runs') for p in parts):
        return 'Local credentials, accounts, generated runs or dependencies must not be published.'
    if parts[-1].startswith('demo_welcome_private-'):
        return 'The private local welcome page contains generated evaluation credentials.'
    if parts[-1].endswith(PRIVATE_SUFFIXES):
        return 'Private state, key, log or archive file must not be published.'
    return None


def content_problem(data):
    text = data.decode('utf-8', errors='replace')
    if any(pattern.search(text) for pattern in TOKEN_PATTERNS):
        return 'Possible credential or private key; inspect locally. Matching content is withheld.'
    if KEY_LITERAL.search(text):
        return 'Long literal API key assignment; inspect locally. Matching content is withheld.'
    return None


def check_entries(entries):
    findings, seen = [], set()
    for name, data, mode in entries:
        reason = path_problem(name)
        if name.casefold() in seen:
            reason = 'Duplicate path under Windows case-insensitive naming.'
        seen.add(name.casefold())
        if mode not in {'100644', '100755'}:
            reason = 'Only regular files can be published by this check; links and submodules need review.'
        reason = reason or content_problem(data)
        if reason:
            findings.append({'file': name, 'reason': reason})
    return findings


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise ValueError('Git index could not be read. Run this command in the intended repository after git add.')
    return result.stdout


def staged_entries(root):
    entries = []
    for record in git(root, 'ls-files', '--stage', '-z').split(b'\0'):
        if not record:
            continue
        header, raw_name = record.split(b'\t', 1)
        mode, object_id, stage = header.decode('ascii').split()
        if stage != '0':
            raise ValueError('Resolve merge conflicts before checking the Git index.')
        name = raw_name.decode('utf-8', errors='strict')
        # Read the staged blob, not a potentially different working-tree file.
        data = git(root, 'cat-file', 'blob', object_id) if mode in {'100644', '100755'} else b''
        entries.append((name, data, mode))
    if not entries:
        raise ValueError('The Git index is empty. Add the intended source files first.')
    return entries


def package_entries(root):
    root = Path(root).resolve()
    path = root / 'bundle_manifest.json'
    if path.is_symlink():
        raise ValueError('The manifest must be a regular file.')
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if not isinstance(manifest, dict):
        raise ValueError('The release manifest must be an object.')
    files = manifest.get('file_sha256')
    extras = manifest.get('repository_files_sha256', {})
    if not isinstance(files, dict) or not files or not isinstance(extras, dict):
        raise ValueError('Missing or invalid release file hashes.')
    if set(files) & set(extras) or 'bundle_manifest.json' in files or 'bundle_manifest.json' in extras:
        raise ValueError('Duplicate or self-referencing release file hashes.')
    entries = [('bundle_manifest.json', raw, '100644')]
    for name, expected in {**files, **extras}.items():
        reason = path_problem(name)
        if reason:
            raise ValueError('Unsafe release path: ' + str(name))
        path = root / name
        if any(p.is_symlink() for p in (path, *path.parents) if p == root or root in p.parents):
            raise ValueError('Release files must not use symbolic links: ' + name)
        data = path.read_bytes()
        if not isinstance(expected, str) or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('Release hash mismatch: ' + name)
        entries.append((name, data, '100644'))
    return entries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--package', action='store_true', help='Verify and scan only manifest-declared release files.')
    mode.add_argument('--staged', action='store_true', help='Scan every tracked blob in the exact Git index.')
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        entries = package_entries(args.root) if args.package else staged_entries(args.root)
        findings = check_entries(entries)
        result = {'status': 'BLOCKED' if findings else 'PASSED', 'files_checked': len(entries),
                  'mode': 'release payload' if args.package else 'Git index', 'findings': findings,
                  'scope': 'Local path, hash and common credential checks. Review changes before publishing; no exhaustive secret-detection claim.'}
        print(json.dumps(result, indent=2))
        return 1 if findings else 0
    except (OSError, ValueError, UnicodeError) as exc:
        # File errors are generic so an unexpected path/exception cannot echo contents.
        message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, (UnicodeError, json.JSONDecodeError)) else 'Release files or Git index could not be read.'
        print(json.dumps({'status': 'BLOCKED', 'error': message}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
