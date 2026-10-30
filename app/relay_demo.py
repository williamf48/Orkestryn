#!/usr/bin/env python3
"""Live judge launcher using supplied evaluation credentials, never a Nebius key."""
from __future__ import annotations

import argparse
from getpass import getpass, GetPassWarning
import html
import json
from pathlib import Path
import secrets
import sys
import warnings
import webbrowser

import downloadable_demo
import hosted_server
import judge_relay as relay
import judge_setup
import judge_evaluation
import operations_server
import operations_workspace as ops
import terminal_guard


def setup(directory, url, username='judge', password=None):
    url = relay.origin(url)
    if password is None:
        if not terminal_guard.interactive_input():raise ValueError('Use the supplied evaluation login in a private interactive terminal.')
        with warnings.catch_warnings():
            warnings.simplefilter('error', GetPassWarning)
            password = getpass('Evaluation password from the testing instructions (hidden; not a Nebius key): ')
    value = {'version':relay.VERSION, 'origin':url, 'username':username, 'password':password}
    relay.save(directory, value)
    return value


def prepare_owner(directory, url):
    value = setup(directory, url, 'judge', 'arp_' + secrets.token_urlsafe(32))
    text = ('ORKESTRYN — PRIVATE HOSTED RELAY SETUP\n'
            'Never publish this file. This contains an evaluation password, not a Nebius API key.\n\n'
            'In Cloudflare Worker Variables and Secrets add these two Secret values:\n'
            'RELAY_USERNAME: ' + value['username'] + '\nRELAY_PASSWORD: ' + value['password'] + '\n\n'
            'Add your NEBIUS_API_KEY separately as a Cloudflare Secret.\n'
            'Public relay origin: ' + value['origin'] + '\n\n'
            'Give judges the origin, evaluation username and evaluation password in private Devpost testing instructions.\n'
            'They run START_JUDGE_LIVE.cmd and enter these once. No provider key is distributed.\n'
            'The app separately generates its local manager, customer-owner and admin logins.\n'
            'Follow JUDGE_RELAY_SETUP.md. This file does not deploy or verify a live service.\n')
    judge_setup.private_write(Path(directory) / 'RELAY_OWNER_PRIVATE.txt', text)
    return Path(directory) / 'RELAY_OWNER_PRIVATE.txt'


def welcome(settings, port, url, judge_account=None):
    judge_setup.validate(settings)
    if type(port) is not int or not 1 <= port <= 65535:raise ValueError('Use a running local port.')
    url = relay.origin(url)
    rows = ''.join('<tr><td>' + name + '</td><td><code>' + html.escape(password) + '</code></td></tr>'
                   for name, password in settings['accounts'].items())
    if judge_account is not None:
        judge_evaluation.validate(judge_account)
        rows = '<tr><td><strong>judge-admin · complete evaluation</strong></td><td><code>' + html.escape(judge_account['password']) + '</code></td></tr>' + rows
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Orkestryn | Live evaluation</title>
<style>:root{color-scheme:dark;font:15px/1.65 'Segoe UI',sans-serif;color:#edf2fa;background:#0b1120}
main{max-width:980px;margin:auto;padding:36px 24px}section{background:#111c2e;border:1px solid #30405a;border-radius:12px;padding:24px;margin:24px 0}
h1{font-size:36px;line-height:1.2}h2{font-size:20px}p,li{color:#bac9da}td,th{padding:12px;text-align:left;border-bottom:1px solid #30405a}
table{width:100%}code{overflow-wrap:anywhere;user-select:all}a{color:#9ad9ff}.button{display:inline-block;padding:12px 20px;background:#206b5c;color:white;border-radius:8px;text-decoration:none}
li{padding:6px 0}.private{color:#e6bf82}</style><main><p>OWNER-FUNDED LIVE EVALUATION</p>
<h1>Orkestryn · Three agents. Real NVIDIA inference.<br>Separate approvals for every change.</h1>
<p>NVIDIA Nemotron requests go through the owner's hosted relay to Nebius Token Factory.
The provider key remains on that host. The coordinator, worker processes and saved runs stay on this computer.</p>
<p>Relay: <code>''' + html.escape(url) + '''</code>. Configuration alone does not verify its availability or credit balance.</p>
<section><h2>Open the Operations console</h2><p><a class="button" target="_blank" rel="noopener" href="http://127.0.0.1:''' + str(port) + '''">Open Orkestryn</a></p>
<p class="private">Private local passwords. Keep this page out of videos, GitHub and shared evidence.</p>
<table><tr><th>Local account</th><th>Password</th></tr>''' + rows + '''</table></section>
<section><h2>Complete the live walkthrough</h2><ol>
<li>Sign in as <strong>judge-admin</strong> for the complete dashboard, or use the separate manager and customer accounts. Create a workflow with <strong>NVIDIA Nemotron through Nebius → Balanced → 60 minutes</strong>.</li>
<li>Review and authorize the paid <strong>Scenario planning</strong> proposal. Wait for its saved result.</li>
<li>Review and authorize the paid <strong>Governance</strong> proposal. Review its result, then separately approve the exact batch.</li>
<li>Start the observer. Review and approve the controlled development fault. Wait for two failed health windows and the incident.</li>
<li>Review and authorize the paid <strong>Recovery</strong> proposal. Inspect the result. If valid, separately approve recovery and the manager handoff.</li>
<li>Stay signed in as <strong>judge-admin</strong> to separately review customer handoff and release only the finished <strong>C-mission-job</strong>. For role separation, switch to <strong>judge-customer</strong> instead.</li>
<li>Inspect the outcome, stop the observer and owned workers, and download the evidence ZIP. With separate accounts, return as manager for shutdown.</li>
</ol><p>Each model request needs its own consent. Failed, refused, invalid or deferred model output may stop the flow for review.
No automatic retry or scripted replacement is made. Service changes require their separate exact approvals.
The judge-admin walkthrough records one account acting in both handoff roles; it does not demonstrate approval by two different people.</p>
<p>All capacity and customer workload values are local fixtures. Live proposals do not provision cloud GPUs.</p></section>
<p>Keep the console open during use. Runs default to 60 minutes; local login sessions last 30 minutes.
Stopping your own app does not stop the owner's hosted relay. The free START_DEMO.cmd launcher remains available.</p></main></html>'''


def prepare(directory, *, port=0):
    version = downloadable_demo.package_version()
    directory = judge_setup.checked_directory(directory)
    connection = relay.load(directory)
    workspace = directory / 'relay_workspace'
    settings = judge_setup.load(workspace) if workspace.exists() else judge_setup.prepare(workspace)
    root = ops.directory_for(workspace / 'operations_runs');root.mkdir(parents=True, exist_ok=True)
    hosted_server.provision(root, judge_setup.environment(settings))
    hosted_server.forget_account_secrets()
    judge_account = judge_evaluation.provision(workspace, root, settings)
    server = operations_server.Server(root, port=port, proposal_providers=relay.providers(connection))
    server.evaluation = judge_evaluation.metadata()
    try:
        guide = directory / ('DEMO_WELCOME_PRIVATE-' + secrets.token_hex(6) + '.html')
        judge_setup.private_write(guide, welcome(settings, server.server_port, connection['origin'], judge_account))
        return server, guide, version
    except BaseException:
        server.server_close();raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start','setup','prepare-owner'), nargs='?', default='start')
    parser.add_argument('--directory', type=Path, default=None)
    parser.add_argument('--origin')
    parser.add_argument('--username', default='judge')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)
    directory = args.directory or downloadable_demo.default_directory()
    try:
        downloadable_demo.package_version()
        if args.action == 'prepare-owner':
            if not args.origin:parser.error('prepare-owner requires the actual --origin.')
            # Owner preparation uses a new private directory, so account setup
            # and downloaded-demo data are never replaced by a connection file.
            if directory.exists():raise ValueError('Use a new private owner-setup directory.')
            path = prepare_owner(directory, args.origin)
            print('Private relay host values: ' + str(path))
            print('No deployment or provider request was made. Add your Nebius key privately on the host.')
            return 0
        if args.action == 'setup' or not (directory / relay.CONNECTION).exists():
            url = args.origin
            if not url:
                if not terminal_guard.interactive_input():raise ValueError('Use the supplied testing URL in a private terminal.')
                url = input('Hosted evaluation URL from the testing instructions: ').strip()
            setup(directory, url, args.username)
            print('Evaluation connection saved privately. No model request was sent.')
            if args.action == 'setup':return 0
        server, guide, version = prepare(directory)
    except (ValueError, OSError, ImportError, GetPassWarning, EOFError):
        print('Live evaluation could not start. Use the full ZIP, Python 3.10+ and the supplied relay URL/login. See JUDGE_RELAY_SETUP.md. No credential was printed.')
        return 1
    print('Orkestryn live evaluation: ' + version, flush=True)
    print('Private welcome page: ' + str(guide), flush=True)
    print('Operations console: http://127.0.0.1:' + str(server.server_port), flush=True)
    print('Startup made zero model calls. Each paid role still needs its own browser review.', flush=True)
    print('Stop the observer and owned workers before closing this console.', flush=True)
    try:
        if not args.no_browser:
            try:
                if not webbrowser.open(guide.as_uri()):print('Open the private welcome page shown above.', flush=True)
            except (OSError, webbrowser.Error):print('Open the private welcome page shown above.', flush=True)
        server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
    return 0


if __name__ == '__main__':raise SystemExit(main())
