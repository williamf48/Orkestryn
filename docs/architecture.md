# Architecture

| Layer | Main modules | Responsibility |
|---|---|---|
| Proposal roles | browser_proposals.py, operations_proposals.py | One reviewed Nebius request for Planning, Governance or Recovery |
| Independent coordination | operations_workspace.py, workflow_planner.py | Validate dependencies, ownership, versions, capacity and permitted changes |
| Identity and review | named_access.py, operations_server.py | Local accounts, roles, CSRF and exact-operation approval |
| Effects and observation | durable_runtime.py, native_process.py, operations_supervisor.py | Local processes, HTTP probes, durable receipts and shutdown confirmation |
| Evidence | operations_evidence.py, operations_outcomes.py | Export records, report outcomes and check consistency |
| Safety Lab | safety_lab.py, safety_environment.py, safety_live.py | Separate simulation, authored controls, optional model requests and independent scoring |
| Distribution | run.py, desktop_launcher.py, build_desktop.py | Main/demo launch, verified payload and portable Python application |

The proposal roles submit recommendations to the controller. Review fingerprints bind approval to the precise plan and current state. Effects execute only after validation and approval. Recovery is scoped to owned changes and preserves customer work until its separate release or handoff is eligible.

SQLite records attempts before model requests and effects through durable receipts. Restart reconciliation checks completed effects rather than issuing an uncertain request again. Failed or unconfirmed provider attempts remain counted and are not automatically retried.

The judge and main dashboards share the same backend. Judge mode creates separate private data. Safety Lab uses its own research journal and cannot mutate the Operations runtime. The full action guard is deterministic; it is not an LLM monitor.

See the [detailed design](system-design.md), [evidence format](evidence-exports.md), [NVIDIA setup](nvidia.md) and [safety methods](safety-lab.md).
