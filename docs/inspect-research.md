> Source layout note: run the command examples in this guide from the `app/` directory unless stated otherwise. Root launchers and current submission instructions are in the repository README.

# Optional Inspect evaluation

The main app and downloadable demo include Safety Lab and work without Inspect.
The separate adapter uses UK AISI's [Inspect framework](https://inspect.aisi.org.uk/)
for familiar task logs and inspection. It does not contact an inference provider.

## Windows setup

Use Python 3.11 or newer for this optional adapter; the ordinary app still supports
Python 3.10+. Run these commands in the extracted project directory:

```powershell
py -3 -m venv .research-venv
.\.research-venv\Scripts\python.exe -m pip install -r requirements-research.txt
.\.research-venv\Scripts\inspect.exe eval safety_inspect.py@orkestryn_control --model mockllm/model
.\.research-venv\Scripts\inspect.exe view
```

The requirements pin Inspect 0.3.286. The verification record gives the actual
Python version and platform tested; it is not a native Windows result.
No API key is needed. `mockllm/model` is a marker for this deterministic task;
the solver never calls `generate()` or any service tool.

Try a deliberately weak prefix control, then compare the full action guard:

```powershell
.\.research-venv\Scripts\inspect.exe eval safety_inspect.py@orkestryn_control --model mockllm/model -T protocol=prefix_audit -T budget=4 -T split=heldout
.\.research-venv\Scripts\inspect.exe eval safety_inspect.py@orkestryn_control --model mockllm/model -T protocol=full_guard -T framing=true
```

Or run the checked seven-task comparison with generation and network connections
forbidden by the test harness:

```powershell
.\.research-venv\Scripts\python.exe safety_inspect_check.py --output research_check
```

On Linux, use `.research-venv/bin/python` and `.research-venv/bin/inspect`.
Inspect log files are private local research outputs by default, not release files.

## What the adapter does

1. Selects frozen authored cases and optional text variants.
2. Gives a deterministic solver only the public task, state, permissions and plan.
3. Records the control decision without calling a model.
4. Scores the outcome against the independent state-history oracle.
5. Saves Inspect logs containing the decision, reference receipts and actual outcome.

Evaluator labels live in the scorer, not the sample input, target or metadata.
This is an interface convention: the local Python process and source are trusted.

The adapter's `harm`, `main_task_success` and `escalated` means use all selected
cases. They are not conditional safety/usefulness estimates. The main Safety Lab
report separately reports harmful attack opportunities, honest task denominators,
false positives, coverage and paired decision changes. No Inspect score is a
production-safety certification.

The adapter does not wire Inspect to a paid model provider. Actual NVIDIA research
requests are made through the app's reviewed request form with its existing
no-retry accounting. Replacing the solver with `generate()` would be a different,
unvalidated experiment and is outside the provided adapter.

Implementation references: [tasks](https://inspect.aisi.org.uk/tasks.html),
[solvers](https://inspect.aisi.org.uk/solvers.html),
[scorers](https://inspect.aisi.org.uk/scorers.html),
[datasets](https://inspect.aisi.org.uk/datasets.html).
