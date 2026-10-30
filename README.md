# Orkestryn

Agents in sync. Humans in control.

Orkestryn (formerly Agent Rehearsal) coordinates AI-proposed infrastructure changes and scoped recovery while preserving customer-owned work. NVIDIA Nemotron runs through Nebius Token Factory; an independent controller validates proposals, and people approve exact operations before effects occur.

Python 3.10+ · Windows / macOS / Linux · MIT

## What you can do

- Review Planning, Governance and Recovery proposals in one operations dashboard.
- Validate dependencies, capacity, ownership, versions and exact approval scope.
- Execute local worker and HTTP effects with durable SQLite receipts and restart reconciliation.
- Recover an owned deployment while preserving customer routes, allocations and running-job dependencies.
- Review manager and customer handoff separately, then release only an approved finished job.
- Compare Safety Lab controls and live model monitors against independent simulated outcomes.
- Export evidence with provider-reported usage and deterministic replay checks.

## Run locally

Main app:

No package installation is needed for the core app or free scripted demo.

```sh
python run.py --main --console
```

On Windows use `py -3` instead of `python`, or double-click START_APP.cmd. The launcher prints the local address. Windows main mode reuses existing accounts in `~/AgentRehearsal/operations_runs`; new installations follow the account setup shown in the browser. Keep the console running.

Judge demo:

```sh
python run.py --demo --console
```

On Windows, double-click START_DEMO.cmd. A private welcome page supplies generated judge-admin credentials for an isolated workspace. The demo includes the complete dashboard, approvals, monitoring, evidence and Safety Lab. The free walkthrough uses labelled scripted proposals with real local process/HTTP effects.

Live NVIDIA requests are optional and reviewed one at a time. Enter a Nebius key privately in the app, or configure owner-funded access. Keys are excluded from saved evidence. No shared provider key or password is bundled. Startup makes zero model calls. See [judge instructions](docs/judge-guide.md) and [NVIDIA setup](docs/nvidia.md).

## NVIDIA and Nebius integration

The app calls `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` through `https://api.tokenfactory.nebius.com/v1/`. Three proposal roles prepare the scenario, order changes and recommend recovery. Safety Lab can separately request candidate plans and monitor verdicts. Structured JSON responses still pass independent validation; model output grants no execution authority.

Nebius hosts inference, so the local computer needs no NVIDIA GPU. The application server, SQLite, worker processes and probes run locally unless you deploy the supplied hosting configuration. SDK-based command-line integrations are optional: `python -m pip install -r requirements.txt`.

Typical workflow:

1. Create a workflow and review the Planning and Governance stages.
2. Inspect and approve the exact batch; start the background observer.
3. Review the controlled development fault and Recovery proposal.
4. Complete the separate handoff reviews and approved finished-job release.
5. Review the outcome, stop the observer and owned workers, and export evidence.

A single judge-admin holds all demonstration roles; its two handoff reviews do not establish approval by two different people. Each paid request has its own review and no automatic retry.

## Safety Lab

Compare controls on 48 authored cases across six families, inspect a selected candidate, or run a separately approved NVIDIA candidate/monitor request. Scoring distinguishes harm prevention, honest safe completion, false positives and coverage. Local monitor budgets count actions; model output caps and reported usage are separate.

Two live monitor tests recorded one detected authored attack and one false positive on a safe generated plan. These small experiments demonstrate the evaluation flow, not general monitoring reliability. See [methods and results](docs/safety-lab.md).

## Verify and build

```sh
python scripts/check_repository.py
python scripts/test.py
python app/operations_ui_checks.py
python app/safety_ui_checks.py --output /tmp/safety-ui-checks.json
python run.py --demo --check --directory /tmp/orkestryn-smoke
python scripts/build_release.py --output dist
```

UI checks need Node.js. On Windows, use a new path under `$env:TEMP` for test outputs instead of `/tmp`. The build creates a source ZIP and Orkestryn.pyz for GitHub Release assets; generated binaries and archives are not committed to source. See [testing](docs/testing.md) and [upload instructions](docs/github-upload.md).

## Repository layout

| Path | Contents |
|---|---|
| `run.py`, `START_APP.cmd`, `START_DEMO.cmd` | Main and judge-demo entry points |
| `app/` | Application modules, regression tests and necessary test datasets |
| `docs/` | Architecture, setup, judging, safety research and submission guides |
| `scripts/` | Test runner, repository checks, release builder and optional Git push helper |
| `.github/workflows/` | Offline CI, manual release publishing and optional native builds |
| `Dockerfile`, `render.yaml` | Optional persistent server deployment recipes |

Tests remain beside the modules they exercise to preserve existing fixture paths and direct command-line tools. Old verification dumps, terminal logs, duplicate reports, development diaries, caches, account files and generated run folders are excluded.

Evidence limits:

Capacity units and customer resources are local fixtures, not provider GPU reservations. HTTP probes are local measurements, not a cloud SLA. Built-in attacks are authored; the same Nemotron model in different roles does not establish independent trusted oversight. The project makes no production-safety, alignment-certification, revenue or incident-prevention claim.

See [architecture](docs/architecture.md), [current verification](docs/verification.md), [competition checklist](docs/competition.md), [contributing](CONTRIBUTING.md), [security](SECURITY.md) and [MIT licence](LICENSE).
