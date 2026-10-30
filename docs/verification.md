# Verification

Release: 2026.10.30-github-v1, checked 30 October 2026.

| Check | Observed result |
|---|---|
| Registered offline Python suite | 945 passed, zero failures, errors or skips; isolated module processes on Linux/Python 3.12 |
| Main dashboard behavior | 144 passed, Node minimal DOM and mocked HTTP |
| Safety Lab behavior | 39 passed, Node minimal DOM and mocked HTTP |
| Scoped relay behavior | 45 passed, Node tests and fake provider transport |
| Portable main workspace | 9 passed, actual Python zipapp/server/API, existing account preservation and zero-call proposal preview |
| Portable judge demo | 29 passed, actual zipapp/server/API, login, 72 framing cases, evidence replay, restart and isolation checks |
| Source walkthrough | VERIFIED_RECOVERED, owned workers confirmed stopped, archive integrity checked |
| Syntax | All retained Python source parses for Python 3.10 |
| Publication scan | Clean app manifest and repository path/common credential checks passed |

All listed checks made zero real provider requests. Provider interaction in offline tests uses fake transports; the walkthrough uses labelled scripted decisions and real local process/HTTP effects. UI tests do not certify rendered appearance. GitHub-hosted Actions have not run until the source is published there.

The earlier distribution was smoke-tested by the owner on native Windows with verified recovery and confirmed worker shutdown. The reorganized portable distribution was built and process-tested here on Linux; rerun `py -3 Orkestryn.pyz --check --directory NEW_TEMP_DIRECTORY` on Windows before publishing the new asset. No native Windows/macOS executable build is claimed.

All 117 application Python modules are retained. Only `app/install_update.py` changes among those modules: its required-file inventory now covers executable source and necessary fixtures rather than obsolete historical reports and manuals. Policy, approvals, recovery, provider request and Safety Lab modules remain byte-identical to the preceding safety-schema build. New root launchers, repository scripts, deployment paths, workflows and documentation support the reorganized layout.

The app's hash manifest validates local file consistency, not authorship or a signed distribution. The scanner detects known path and credential patterns; it is not exhaustive secret detection. Live Safety Lab observations and their limitations are documented separately in [Safety Lab](safety-lab.md).

## Removed from the public package

The cleanup removes 74 historical JSON verification/observation/audit dumps, old terminal logs, duplicate generated HTML/JSON reports, development diaries and overlapping or superseded manuals. Necessary scenario data, regression replies, tests and the Operations test snapshot remain because code/tests consume them. Private accounts, databases, credentials, caches, generated runs and local test logs are excluded.

Full local test logs are kept outside source; this compact record replaces the old collection of generated reports. No claim of public hosting, funded live judge access, GitHub publication or completed Devpost submission is made.
