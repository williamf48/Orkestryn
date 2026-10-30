"""One full-permission local evaluation account; existing role accounts stay intact.

This helper is used only by the downloaded evaluation launchers. It never grants
permissions to an existing account, resets a password, or makes a model call.
"""
from __future__ import annotations

import json
from pathlib import Path
import secrets

import judge_setup
import named_access

NAME = 'judge-admin'
VERSION = 'agent-rehearsal-single-judge-v1'
PRIVATE_DIRECTORY = 'judge_evaluation_private'


def validate(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'name', 'password'}
            or value['version'] != VERSION or value['name'] != NAME):
        raise ValueError('Keep the original private judge administrator settings.')
    password = value['password']
    if (not isinstance(password, str) or not 12 <= len(password) <= 256
            or any(c in password for c in '\r\n\0')):
        raise ValueError('The judge administrator password is invalid.')
    return value


def load(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError('The private judge administrator settings are unavailable.')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate judge administrator setting.')
            result[key] = value
        return result
    return validate(json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs))


def provision(directory, root, settings):
    """Add judge-admin once to this private demo, using its existing admin.

    The original three-account credential file is never changed. A separate
    generated private file allows interrupted creation to finish on the next
    launch. Revoked permissions, disabled accounts and passwords are preserved.
    """
    judge_setup.validate(settings)
    directory = judge_setup.checked_directory(directory)
    private = judge_setup.checked_directory(directory / PRIVATE_DIRECTORY)
    path = private / 'credentials.json'
    auth = named_access.Access(root)
    try:
        existing = auth.db.execute('SELECT id FROM users WHERE name=?', (NAME,)).fetchone()
        if path.exists() or path.is_symlink():
            value = load(path)
            if existing:
                return value
        elif existing:
            raise ValueError('An existing judge-admin account has no generated settings. Keep it unchanged and use the existing role accounts.')
        else:
            value = {'version': VERSION, 'name': NAME, 'password': 'arp_' + secrets.token_urlsafe(32)}

        # A valid existing workspace administrator must authorize account creation.
        token, _, admin = auth.login('workspace-admin', settings['accounts']['workspace-admin'])
        try:
            auth.require(admin, 'admin')
        finally:
            auth.logout(token)
        if not path.exists():
            private.mkdir(mode=0o700, parents=False, exist_ok=True)
            private.chmod(0o700)
            judge_setup.private_write(path, json.dumps(value, indent=2) + '\n')
        auth.create(NAME, value['password'], sorted(named_access.ROLES), admin)
        return value
    finally:
        auth.close()


def metadata():
    # Public descriptions only. The server still enforces every normal role,
    # session, exact-review, source, workflow and paid-attempt check.
    return {'account': NAME, 'single_account_available': True,
            'scope': 'Dedicated local evaluation workspace; separate role accounts remain available.'}
