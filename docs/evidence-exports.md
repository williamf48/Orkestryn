> Source layout note: run the command examples in this guide from the `app/` directory unless stated otherwise. Root launchers and current submission instructions are in the repository README.

# Portable run evidence

Sign in to the Operations console, select a saved workflow, then choose
Download evidence ZIP beside View outcome report. Extract the whole ZIP
and open index.html on any laptop. Viewing the export needs no Python,
application source, account or internet connection.

This is a read-only replay of a saved run. It does not run agents or services,
accept approvals or provide a hosted working demo. It supplements the working
application and the competition submission's required access route.

## What the package contains

| File | Purpose |
|---|---|
| `index.html` | Offline overview, three-agent status and selected approval/execution timeline |
| `outcome_report.html` | Detailed existing outcome report and measurement limits |
| `outcome_report.json` | Structured outcomes and their saved-field references |
| `agent_attempts.json` | Whitelisted role status, response identifiers and reported usage |
| `activity.json` | Latest at most 200 events, with approval records separate from effect records |
| `bundle_manifest.json` | File sizes, hashes and source-summary reference |
| `README.txt` | Review instructions and verification limits |

The exporter does not include credentials, account databases, raw prompts or
replies, workload task inputs, private worker capabilities or runtime databases.
Named approval actors are included for accountability. Review the files before
sharing. A check for common credential patterns can block an export; that check
is a heuristic, not proof that arbitrary user data is non-sensitive.

The dashboard download requires a valid login and works for viewers. It is
available for stopped, expired and historical runs. Historical records retain
their original source identity and are labeled as historical; they are not
rebound or revalidated as current-source effects. No account permission, original
workflow lease, model retry or approval rule is changed by downloading evidence.

If an observer or another action changes the saved summary during export, the
download stops with EVIDENCE_CHANGED. Refresh and explicitly download again.
There is no automatic export retry, model call or workflow reconciliation.

## Create or verify from a terminal

In the project folder, replace the example ID with your actual saved run:

```powershell
py -3 operations_evidence.py create --run ".\operations_runs\EXACT_RUN_ID"
py -3 operations_evidence.py verify "C:\path\to\Orkestryn-Evidence-RUN_ID.zip"
```

Creation prints a unique folder under `evidence_exports`. It does not overwrite
a previous export or write reports into the source run. These generated folders
are excluded from Git and container builds; upload reviewed ZIPs as release or
submission evidence rather than staging generated workspace data as source.

Verification reads a bounded, fixed set of files in memory. It rejects added
files, path traversal, duplicate members, file links, oversized content, changed
hashes and reports referring to different snapshots. It neither extracts files
nor opens HTML, starts a process or contacts a provider.

INTEGRITY_CHECKED means the included files agree with the package's manifest
and cross-file references. It does not authenticate the author, prove a provider
response, establish current liveness or verify the complete original private
journal. Someone who rewrites every file and hash can create a different bundle;
these hashes are not signatures. Open archives only from sources you trust.

## Reading results accurately

- Scripted rehearsals are labeled and never pass as live-provider validation.
- A model attempt is separate from a validated reply and an authorized effect.
- Missing token usage and measurements stay unknown.
- An approval record does not prove that execution succeeded.
- Stopped runs describe saved outcomes; they are not running services.
- Capacity and ownership are local fixtures, not measured GPU quota or revenue.
