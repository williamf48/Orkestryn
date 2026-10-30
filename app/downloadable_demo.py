#!/usr/bin/env python3
"""Launch local evaluation: free rehearsal, with optional NVIDIA key entry."""
from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import secrets
import sys
import webbrowser

import install_update
import judge_setup
import judge_evaluation
import operations_evidence
import operations_outcomes
import operations_workspace as ops

ROOT = Path(__file__).resolve().parent
CONTACT = 'https://github.com/williamf48'
GUIDE_PREFIX = 'DEMO_WELCOME_PRIVATE-'


def default_directory(environment=None):
    environment = os.environ if environment is None else environment
    local = environment.get('LOCALAPPDATA', '')
    if local and Path(local).is_absolute():
        return Path(local) / 'AgentRehearsal' / 'DownloadableDemo'
    return ROOT / 'judge_access_private' / 'downloadable-demo'


def package_version():
    # Verify every installed payload file before preparing local accounts.
    # Merely importing this module never reads any provider credential.
    _, version = install_update.payload(ROOT)
    return version


def welcome_html(settings, port, judge_account=None):
    judge_setup.validate(settings)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Use the running local server port.')
    url = 'http://127.0.0.1:' + str(port)
    descriptions = {
        'judge-manager': 'Planning, governance, recovery and worker shutdown',
        'judge-customer': 'Customer handoff and exact customer-job release',
        'workspace-admin': 'Optional account and permission administration',
    }
    rows = ''.join('<tr><td><strong>' + name + '</strong><small>' + descriptions[name]
                   + '</small></td><td><code>' + html.escape(settings['accounts'][name])
                   + '</code></td></tr>' for name in descriptions)
    recommended = ''
    if judge_account is not None:
        judge_evaluation.validate(judge_account)
        recommended = ('<div class="card"><span class="badge">Recommended for judges</span>'
            '<h2>One account. The complete dashboard.</h2><p>Username: <strong>judge-admin</strong><br>'
            'Password: <code>' + html.escape(judge_account['password']) + '</code></p>'
            '<p>See all three agents, approve each workflow operation, complete both handoff role reviews, '
            'release the finished customer job, inspect evidence and manage accounts without signing out.</p>'
            '<p class="note">This is a single-account evaluation. Manager and customer handoff reviews '
            'remain separate operations; their records identify the same account. '
            'Use the separate accounts below to test permission restrictions.</p></div>')
    return '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Orkestryn | Private demo welcome</title>
<style>
:root{color-scheme:dark;font:15px/1.6 'Segoe UI',Arial,sans-serif;color:#edf2fa;background:#0b1120}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top right,#18283d,transparent 60%)}
main{max-width:1040px;margin:auto;padding:44px 24px}h1{font-size:clamp(28px,5vw,44px);line-height:1.15;margin:16px 0}
h2{font-size:20px;margin:0 0 12px}p,small,li{color:#b6c3d6}.card{background:#111c2e;border:1px solid #30405a;border-radius:14px;padding:24px;margin:20px 0}
.badge{display:inline-block;color:#75ddc3;border:1px solid #2b6c60;border-radius:99px;padding:5px 12px;font-size:12px}
.button{display:inline-block;background:#206b5c;border:1px solid #3b8e7d;border-radius:8px;padding:12px 20px;color:white;text-decoration:none;font-weight:600}
a{color:#a5d5ff}table{width:100%;border-collapse:collapse}td,th{padding:14px 10px;text-align:left;border-bottom:1px solid #30405a;vertical-align:top}
td:first-child{width:40%}small{display:block}code{font:13px/1.7 Consolas,monospace;overflow-wrap:anywhere;user-select:all}
.note{border-left:3px solid #75ddc3;padding-left:16px}ol{padding-left:24px}li{padding:5px 0}.private{color:#e4be78}
@media(max-width:600px){main{padding:24px 16px}.card{padding:18px}td,th{padding:10px 6px}code{font-size:11px}}
</style><main>
<span class="badge">Orkestryn · Downloadable demo · Free scripted rehearsal</span>
<h1>Try a coordinated change.<br>Recover without losing customer work.</h1>
<p>Three scripted proposal roles work with the real local coordinator, worker processes,
HTTP probes and approval records. No Nebius request, provider SDK or API key is needed.</p>
<p class="private">Private welcome page: these passwords were generated on this computer.
Keep this file out of GitHub, videos and shared evidence.</p>
<section class="card"><h2>1. Open the Operations console</h2>
<p>Copy the recommended judge credentials below, then open the console and sign in.</p>
<p><a class="button" href="''' + url + '''" target="_blank" rel="noopener">Open Orkestryn</a></p>
<p><code>''' + url + '''</code></p>
<p>Keep the launcher console open. This address runs on this computer only.</p></section>
''' + recommended + '''
<section class="card"><h2>Separate role accounts</h2>
<table><thead><tr><th>Username and purpose</th><th>Password</th></tr></thead><tbody>''' + rows + '''</tbody></table>
<p>For role separation, start with <strong>judge-manager</strong>. Switch to <strong>judge-customer</strong>
for the customer approvals. The administrator has no automatic operational permissions.</p></section>
<section class="card"><h2>2. Rehearse the full workflow</h2><ol>
<li>Choose <strong>Create a workflow → Free scripted rehearsal → Balanced → 60 minutes</strong>.</li>
<li>Open <strong>Approvals → Review batch</strong>, inspect the selected and waiting requests, and approve the exact batch.</li>
<li>In <strong>Monitoring</strong>, start the background observer. Review and approve the <strong>development fault</strong> to stop the owned candidate worker.</li>
<li>After the incident appears, review and approve recovery, then complete the <strong>Manager handoff review</strong>.</li>
<li>With <strong>judge-admin</strong>, stay signed in: complete the separate customer handoff review, then release only the finished <strong>C-mission-job</strong>. With role accounts, switch to <strong>judge-customer</strong> for these approvals.</li>
<li>In <strong>Evidence</strong>, inspect <strong>View outcome report</strong>, stop the observer and owned workers, then download the evidence ZIP. With separate accounts, return as <strong>judge-manager</strong> for shutdown.</li>
</ol><p>The countdown belongs to this rehearsal. New runs default to 60 minutes; sessions
last 30 minutes. Signing in again preserves an unexpired run. A stopped or expired run
remains readable; create another workflow for new operations.</p></section>
<section class="card"><h2>Safety Lab: compare oversight protocols</h2>
<p>After signing in as <strong>judge-admin</strong>, choose <strong>Safety Lab</strong>
in the Operations sidebar. Run a free comparison, try paired prompt-framing probes,
and export a researcher report. These bounded simulated attacks cannot modify an
Operations workflow. No API key is needed for the authored comparison.</p>
<p>Optional candidate and monitor requests each have their own paid review. The same
research dashboard is included in the installed main app.</p></section>
<section class="card"><h2>3. Optional NVIDIA evaluation</h2>
<p>Choose NVIDIA Nemotron through Nebius in Create a workflow to enter a supplied
Nebius API key in its masked field. That direct route needs no provider SDK or
hosting setup. Startup ignores environment keys. Each live request still requires its own approval.</p>
<p><a href="''' + CONTACT + '''" target="_blank" rel="noopener noreferrer">William Fernandes · developer profile</a>
 — evaluation credentials must be supplied separately. No shared key is bundled. Keys entered in the app are held only in session memory; reconnect after signing in again or restarting.</p>
<p class="note">Capacity units and customer dependencies are local fixtures. In free mode,
model replies are scripted. These results do not claim cloud GPU provisioning or live-model quality.</p></section>
<p>Settings, accounts and saved runs stay in this computer's private demo directory.
Restarting preserves them. Reopen the <strong>Orkestryn portable launcher</strong> or <strong>START_DEMO.cmd</strong> for a fresh welcome page.</p>
</main></html>'''


def prepare_demo(directory, *, port=0):
    version = package_version()
    directory = judge_setup.checked_directory(directory)
    server = judge_setup.local_server(directory, live=False, port=port)
    try:
        settings = judge_setup.load(directory)
        judge_account = judge_evaluation.provision(directory, server.root, settings)
        server.evaluation = judge_evaluation.metadata()
        guide = directory / (GUIDE_PREFIX + secrets.token_hex(6) + '.html')
        # Exclusive private creation avoids overwriting a prior guide, credential
        # file or link. Each launch has its own current loopback URL.
        judge_setup.private_write(guide, welcome_html(settings, server.server_port, judge_account))
        return server, guide, version
    except BaseException:
        server.server_close()
        raise


def check_demo(directory):
    package_version()
    directory = judge_setup.checked_directory(directory)
    output = ops.directory_for(directory / 'download_checks')
    output.mkdir(parents=True, exist_ok=True)
    path, _ = ops.demo(output)
    summary = ops.read(path)
    audit = ops.verify(path)
    report = operations_outcomes.build(path)
    raw, _ = operations_evidence.build_bytes(path)
    exported = operations_evidence.verify_bytes(raw)
    ok = (summary['phase'] == 'RECOVERED_SERVICE_ACTIVE'
          and summary['lifecycle'] == 'STOPPED' and summary['model_calls'] == 0
          and summary['teardown']['all_workers_stopped'] and audit['status'] == 'CONSISTENT'
          and report['recovery_status'] == 'VERIFIED_RECOVERED'
          and exported['status'] == 'INTEGRITY_CHECKED')
    return {'status':'PASSED' if ok else 'FAILED', 'run':str(path), 'mode':'SCRIPTED_FIXTURE',
            'recovery':report['recovery_status'], 'owned_workers_confirmed_stopped':summary['teardown']['all_workers_stopped'],
            'archive_integrity':exported['status'], 'model_calls':0,
            'scope':'One finite scripted local process/HTTP walkthrough, not the full acceptance suite or live inference.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=('start','check'), default='start')
    parser.add_argument('--directory', type=Path, default=None)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)
    directory = args.directory if args.directory is not None else default_directory()
    if args.action == 'check':
        if args.port:parser.error('The finite check uses its own local ports.')
        try:
            result = check_demo(directory)
        except (ValueError, OSError, KeyError):
            print('The download check could not finish. Use a complete extracted package and a writable private data directory. See DOWNLOADABLE_DEMO.md.')
            return 1
        print(json.dumps(result))
        return 0 if result['status'] == 'PASSED' else 1
    try:
        server, guide, version = prepare_demo(directory, port=args.port)
    except (ValueError, OSError, KeyError):
        print('The demo could not start. Extract the complete ZIP, use Python 3.10 or newer and keep existing private settings. See DOWNLOADABLE_DEMO.md. Credentials were not printed.')
        return 1
    print('Orkestryn downloadable demo: ' + version, flush=True)
    print('Startup: free rehearsal, zero model calls. Optional NVIDIA key entry is in Create a workflow.', flush=True)
    print('Private welcome page: ' + str(guide), flush=True)
    print('Operations console: http://127.0.0.1:' + str(server.server_port), flush=True)
    print('Private data: ' + str(Path(directory).absolute()), flush=True)
    print('Keep this console open. Stop the observer and owned workers in the dashboard before closing.', flush=True)
    try:
        if not args.no_browser:
            try:
                if not webbrowser.open(guide.as_uri()):
                    print('Open the private welcome page shown above in your browser.', flush=True)
            except (OSError, webbrowser.Error):
                print('Open the private welcome page shown above in your browser.', flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':raise SystemExit(main())
