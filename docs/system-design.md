> Source layout note: run the command examples in this guide from the `app/` directory unless stated otherwise. Root launchers and current submission instructions are in the repository README.

# Architecture

Orkestryn separates model recommendations, validated plans, human grants,
execution and observed outcomes. The three agents share structured context through
the coordinator; they do not receive an unrestricted shell or arbitrary tool access.

## One workflow

Scenario planning proposes bounded values in registered request slots. Governance
orders those requests. `workflow_planner.py` checks types and dependency graphs;
the coordinator independently evaluates capacity, ownership, prerequisite ordering
and the exact state transition of each selected action. Waiting requests retain an
explanation. Exhaustive search applies to at most six requests; larger graphs use
at most 96 candidate orders and have no global-optimality guarantee.

After a named manager approves the exact batch, the coordinator executes it using
durable intents and receipts. The local runtime launches dedicated workers and
serves actual HTTP probes. The observer measures health within the original lease.
A recorded incident is passed to the recovery agent; approved recovery removes
eligible work while retaining other owners' dependencies.

If a failed candidate still serves customer-owned traffic, recovery requests a
separate handoff. Both manager and customer-owner grants bind the same plan. Fresh
fallback health checks precede the route change. The customer job has a separate
exact release. None of these steps resumes the deployment agent implicitly.

## Authority boundaries

| Boundary | Enforced by |
|---|---|
| Local identity, roles, active sessions and revocation | `named_access.py` |
| Exact loopback Host/Origin and CSRF token | `operations_server.py` |
| Paid proposal review, fixed settings and one role per attempt | `operations_workspace.py`, `operations_proposals.py` |
| Typed workflow, bounded tools and dependency checks | `workflow_planner.py` |
| Exact plan, source, state, queue revision and lease | Operations broker and mission guards |
| Durable intent, receipt and owned process handling | `durable_runtime.py`, `runtime_sessions.py` |
| Saved evidence and measured outcome reports | `operations_outcomes.py` |

The first local account has all roles for development. Distinct people require
distinct accounts. A model's JSON cannot grant a role, renew a lease or approve
its own proposal. Initial policy records in the demonstration remain fixtures.

## Persistence and concurrency

SQLite stores mission phases, model attempts, actions and receipts. A separate
command lock serializes mission mutations. A live provider request runs in a
background thread while read-only summary requests remain available. One live
proposal is active per server; another server cannot bypass the mission lock or
its persisted one-attempt rule.

Public summaries use same-directory unique temporary files and atomic replacement.
Bounded Windows sharing-denial retries repeat only the file replacement, not an
approved action or network request. A persistent write failure remains visible.
Saved source fingerprints prevent new effects under changed code.

An interrupted execution reconciles the recorded intent and exact receipt before
continuing. An interrupted model call with no persisted reply is uncertain and
cannot be automatically repeated. A received reply can be validated explicitly
without calling the provider again. These properties cover local process
interruption, not distributed consensus, arbitrary disk corruption or power-loss
durability across external services.

## Résumé claims and inspectable evidence

| Claim | Implementation to inspect |
|---|---|
| Three coordinating NVIDIA/Nebius agents | Role-specific prompts, proposal reviews and persisted handoffs in `agent_team.py`, `operations_workspace.py`, `operations_proposals.py` |
| Dependency-aware planning and capacity validation | `workflow_planner.py`, `change_coordinator.py`, planner and broker tests |
| Scoped recovery preserving customer work | Operations recovery/handoff/job contracts and complete-flow tests |
| Role-based approvals and audit records | Named-access tests, exact API review tests and SQLite mission events |
| Restart reconciliation and health monitoring | Durable runtime crash tests and background observer tests |
| Authenticated dashboard | Loopback API, account/session checks, escaped reviews and UI behavior harness |

Use the dated release verification record for measured test results. Fake-provider
tests prove orchestration behavior, not model accuracy. Future production adapters
must obtain authoritative cloud identity, resource observations and durable receipts
from the target system before these local contracts can govern real resources.
