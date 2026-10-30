#!/usr/bin/env python3
"""Host the same Python workspace behind an HTTPS reverse proxy.

One process, one persistent volume, no public signup. Configure secrets through
the host's environment. Merely starting this server makes no model request.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import sys

import named_access
import operations_server
import operations_workspace as ops

ACCOUNTS = {
    'workspace-admin': ('AR_ADMIN_PASSWORD', ['admin', 'viewer']),
    'judge-manager': ('AR_MANAGER_PASSWORD', ['manager', 'deployer', 'cost', 'traffic', 'consumer', 'viewer']),
    'judge-customer': ('AR_CUSTOMER_PASSWORD', ['owner_C', 'viewer']),
}


def forget_account_secrets():
    """Accounts have been provisioned; child workers do not need these passwords."""
    private = {fields[0] for fields in ACCOUNTS.values()}
    for name in list(os.environ):
        if name.upper() in private:
            os.environ.pop(name, None)


def provision(root, environment):
    """Add missing hosted accounts; never reset passwords or restore revoked roles.

    Each account creation uses Access's own transaction/authorization. If startup
    is interrupted between accounts, only the existing admin's password can
    authorize completing that setup on restart.
    """
    auth = named_access.Access(root)
    try:
        existing = {name: uid for uid, name in auth.db.execute('SELECT id,name FROM users')}
        missing = [name for name in ACCOUNTS if name not in existing]
        if not missing:
            return {'status': 'EXISTING_ACCOUNTS_PRESERVED', 'accounts': list(ACCOUNTS)}
        if existing and 'workspace-admin' not in existing:
            raise ValueError('Use a dedicated hosted state directory; existing accounts are preserved.')
        required = set(missing) | {'workspace-admin'}
        passwords = {name: environment.get(ACCOUNTS[name][0], '') for name in required}
        if any(not isinstance(pw, str) or not 12 <= len(pw) <= 256 for pw in passwords.values()):
            raise ValueError('Set each required hosted account password to 12-256 characters in the host secret settings.')
        if len(set(passwords.values())) != len(passwords):
            raise ValueError('Use a different password for each hosted account.')
        if 'workspace-admin' not in existing:
            principal = auth.create('workspace-admin', passwords['workspace-admin'], ACCOUNTS['workspace-admin'][1])
        else:
            token, _, principal = auth.login('workspace-admin', passwords['workspace-admin'])
            auth.logout(token)
            auth.require(principal, 'admin')
        for name in missing:
            if name == 'workspace-admin':
                continue
            auth.create(name, passwords[name], ACCOUNTS[name][1], principal)
        return {'status': 'ACCOUNTS_READY', 'accounts': list(ACCOUNTS)}
    finally:
        auth.close()


def configured_server(environment=None):
    environment = os.environ if environment is None else environment
    origin = operations_server.public_origin(environment.get('AR_PUBLIC_ORIGIN', ''))
    try:
        port = int(environment.get('PORT', '10000'))
        output_tokens = int(environment.get('AR_MAX_OUTPUT_TOKENS', '8192'))
    except (TypeError, ValueError):
        raise ValueError('PORT and AR_MAX_OUTPUT_TOKENS must be integers.') from None
    if not 1 <= port <= 65535 or not 1024 <= output_tokens <= 10000:
        raise ValueError('Use PORT 1-65535 and AR_MAX_OUTPUT_TOKENS 1024-10000.')
    live = environment.get('AR_LIVE_PROPOSALS', '0')
    if live not in {'0', '1'}:
        raise ValueError('AR_LIVE_PROPOSALS must be 0 or 1.')
    providers = None
    if live == '1':
        key = environment.get('NEBIUS_API_KEY', '').strip()
        if not key:
            raise ValueError('Live proposals need NEBIUS_API_KEY in the host secret settings.')
        # Client construction sends no request. Every attempt still needs its own
        # authenticated manager review in the browser.
        providers = {role: ops.RoleClient(role, key, output_tokens, True) for role in ops.ROLES}
    root = Path(environment.get('AR_STATE_ROOT', '/var/data/agent-rehearsal')).absolute()
    source = Path(__file__).resolve().parent
    if root == source or source in root.parents:
        raise ValueError('Keep hosted accounts and runs on a persistent volume outside the source tree.')
    root = ops.directory_for(root)
    root.mkdir(parents=True, exist_ok=True)
    provision(root, environment)
    forget_account_secrets()
    return operations_server.Server(root, port=port, proposal_providers=providers, https_origin=origin, bind_host='0.0.0.0')


def main():
    try:
        server = configured_server()
    except (ValueError, OSError, ImportError, named_access.AccessDenied):
        # Never echo environment values or provider exception text into platform logs.
        print('Hosted startup needs valid AR_PUBLIC_ORIGIN, persistent storage, account secrets and optional live-provider setup. See HOSTING.md.', file=sys.stderr)
        return 1
    print('Orkestryn: ' + server.https_origin, flush=True)
    print('Hosted accounts configured. No model request is made at startup.', flush=True)
    previous = signal.getsignal(signal.SIGTERM)
    def terminate(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous)
        server.server_close()
    # Existing leased workers remain governed by their original durable runtime
    # and expiry, exactly as on a local server restart. No effect is inferred here.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
