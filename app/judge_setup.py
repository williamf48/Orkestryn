#!/usr/bin/env python3
"""Prepare password-protected evaluation accounts and rehearse them locally.

Credentials are generated on the owner's computer and kept in an excluded private
directory. No cloud account, deployment, public signup or model request is created
by setup. Live proposals remain separately approved in the existing dashboard.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from getpass import getpass, GetPassWarning
import json
import os
from pathlib import Path
import secrets
import warnings
import webbrowser

import hosted_server
import operations_server
import operations_workspace as ops
import terminal_guard

ROOT = Path(__file__).resolve().parent
DEFAULT = ROOT / 'judge_access_private'
VERSION = 'agent-rehearsal-judge-settings-v1'


def checked_directory(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise ValueError('Use a private directory without links.')
    return path


def validate(value):
    if not isinstance(value, dict) or set(value) != {'version','accounts'} or value['version'] != VERSION:
        raise ValueError('Use the generated private credentials file.')
    accounts = value['accounts']
    if not isinstance(accounts, dict) or set(accounts) != set(hosted_server.ACCOUNTS):
        raise ValueError('The private file must contain the three evaluation accounts.')
    passwords = list(accounts.values())
    if (any(not isinstance(p, str) or not 12 <= len(p) <= 256 or any(c in p for c in '\r\n\0') for p in passwords)
            or len(set(passwords)) != len(passwords)):
        raise ValueError('Each evaluation account needs a distinct valid password.')
    return value


def load(directory):
    directory = checked_directory(directory)
    path = directory / 'credentials.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
        raise ValueError('Private settings are unavailable. Keep the original credentials file.')
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:raise ValueError('Duplicate private setting.')
            result[key] = item
        return result
    return validate(json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs))


def environment(settings):
    return {hosted_server.ACCOUNTS[name][0]: password for name, password in validate(settings)['accounts'].items()}


def private_write(path, content):
    # Exclusive creation: re-running setup must never silently replace credentials.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(content)


def instructions(settings, origin=None):
    return ('ORKESTRYN — PRIVATE EVALUATION CREDENTIALS\n'
            'Do not upload this file to GitHub or show it in the public video.\n\n'
            'Hosted URL: ' + (origin or 'NOT DEPLOYED — use the local URL printed by the launcher for rehearsal.') + '\n\n'
            + '\n'.join('Username: ' + name + '\nPassword: ' + password + '\n' for name, password in settings['accounts'].items())
            + '\nUse judge-manager for planning, governance, recovery and teardown.\n'
            'Use judge-customer for customer handoff and exact job release.\n'
            'workspace-admin is only for testing account administration in this dedicated evaluation workspace.\n'
            'These are separate demo accounts, not proof of separate people.\n'
            'Follow JUDGE_TESTING.md. Sign out before switching roles, or use separate browser profiles.\n'
            'Sessions last 30 minutes; sign in again as needed. New workflows default to 60 minutes; choose 15, 30 or 60 minutes before creation.\n'
            'The deployment owner configures and funds Nebius; judges need no personal API key.\n'
            'Provide the actual deployed URL and these credentials in Devpost testing instructions.\n'
            'Keep access available through December 15, 2026 at noon Pacific.\n')


def prepare(directory):
    directory = checked_directory(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    settings = {'version': VERSION, 'accounts': {name: 'arp_' + secrets.token_urlsafe(32) for name in hosted_server.ACCOUNTS}}
    validate(settings)
    private_write(directory/'credentials.json', json.dumps(settings, indent=2)+'\n')
    private_write(directory/'JUDGE_CREDENTIALS_PRIVATE.txt', instructions(settings))
    return settings


def export_hosting(directory, origin):
    origin = operations_server.public_origin(origin)
    directory = checked_directory(directory);settings = load(directory)
    target = directory / ('hosting-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(4))
    target.mkdir(mode=0o700)
    values = {'AR_PUBLIC_ORIGIN': origin, **environment(settings), 'AR_LIVE_PROPOSALS': '1', 'AR_MAX_OUTPUT_TOKENS':'8192'}
    # JSON settings are for this helper; env values must be safe single-line literals.
    # Generated URL-safe passwords satisfy this. Reject manually edited env syntax.
    if any(not all(c.isalnum() or c in '_-.:/' for c in v) for v in values.values()):
        raise ValueError('Use the original generated passwords for an environment export.')
    private_write(target/'hosting.env', '\n'.join(k+'='+v for k,v in values.items())+'\n')
    private_write(target/'DEVPOST_TESTING_PRIVATE.txt', instructions(settings, origin))
    private_write(target/'README_PRIVATE.txt', 'Copy hosting.env values into the host secret settings. Add NEBIUS_API_KEY there separately. '
        'Never give that API key to judges. Use a dedicated persistent state directory. These values create missing accounts only; '
        'they do not reset existing passwords. A prepared export is not a deployment. Follow HOSTING.md.\n')
    return target


def local_server(directory, *, live=False, port=0, output_tokens=8192, key=None):
    if type(port) is not int or not 0 <= port <= 65535 or type(output_tokens) is not int or not 1024 <= output_tokens <= 10000:
        raise ValueError('Use port 0-65535 and output tokens 1024-10000.')
    directory = checked_directory(directory)
    settings = load(directory) if directory.exists() else prepare(directory)
    providers = None
    if live:
        key = (key or os.environ.get('NEBIUS_API_KEY') or '').strip()
        if not key:
            if not terminal_guard.interactive_input():
                raise ValueError('Use an interactive terminal for the hidden Nebius key prompt, or set NEBIUS_API_KEY locally.')
            with warnings.catch_warnings():
                warnings.simplefilter('error', GetPassWarning)
                key = getpass('Nebius key locally (hidden): ').strip()
        if not key:raise ValueError('A Nebius key is required for the selected live mode.')
        # Construction alone never requests the provider. Three separate browser
        # approvals are still necessary, one for each persisted role attempt.
        providers = {role: ops.RoleClient(role, key, output_tokens, True) for role in ops.ROLES}
    run_root = ops.directory_for(directory/'operations_runs');run_root.mkdir(parents=True, exist_ok=True)
    hosted_server.provision(run_root, environment(settings))
    hosted_server.forget_account_secrets()
    return operations_server.Server(run_root, port=port, proposal_providers=providers)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare','rehearse','hosting'))
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--origin', help='Actual assigned HTTPS origin for a hosting export.')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--max-output-tokens', type=int, default=8192)
    args = parser.parse_args(argv)
    if args.action!='rehearse' and (args.live or args.open or args.port or args.max_output_tokens!=8192):
        parser.error('Live mode, browser and listener settings apply only to rehearse.')
    if (args.action=='hosting') != bool(args.origin):parser.error('--origin is required only for hosting.')
    try:
        if args.action=='prepare':
            prepare(args.directory)
            print('Private evaluation credentials created: '+str(Path(args.directory).absolute()/'JUDGE_CREDENTIALS_PRIVATE.txt'))
            print('No server, deployment or model request was started. Existing credentials were not replaced.')
            return 0
        if args.action=='hosting':
            target=export_hosting(args.directory,args.origin)
            print('Private host settings and Devpost testing instructions: '+str(target))
            print('Add the Nebius key separately in the host secret settings. No deployment or model call occurred.')
            return 0
        server=local_server(args.directory,live=args.live,port=args.port,output_tokens=args.max_output_tokens)
    except (ValueError,OSError,ImportError,GetPassWarning,EOFError):
        print('Judge setup could not start. Check Python/SDK setup, private settings and the local key prompt. See JUDGE_SETUP.md. Credentials were not printed.')
        return 1
    url='http://127.0.0.1:'+str(server.server_port)
    print('Local judge rehearsal: '+url,flush=True)
    print('Usernames and passwords: '+str(Path(args.directory).absolute()/'JUDGE_CREDENTIALS_PRIVATE.txt'),flush=True)
    print('Live proposals enabled; each attempt needs a separate review.' if args.live else 'Free scripted mode; zero model calls.',flush=True)
    print('This URL is local to this computer. Keep the console open; stop owned workers in the dashboard before closing.',flush=True)
    try:
        if args.open:webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
    return 0


if __name__=='__main__':raise SystemExit(main())
