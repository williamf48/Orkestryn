> Source layout note: run the command examples in this guide from the `app/` directory unless stated otherwise. Root launchers and current submission instructions are in the repository README.

# Configure NVIDIA access once as the workspace owner

Judges choose NVIDIA Nemotron through Nebius directly in Create a workflow.
They sign in with the supplied workspace accounts and never enter an API key.
The server owner provides and funds Nebius Token Factory access. A downloaded ZIP
does not include a shared key; the host must be configured before live evaluation.

## Hosted judging

1. Use the persistent Python hosting setup in [HOSTING.md](hosting.md).
2. Put your actual key in the host's private `NEBIUS_API_KEY` secret setting.
3. Set `AR_LIVE_PROPOSALS` to `1`, configure the judge accounts and restart the
   hosted service. Its entry point is `python hosted_server.py`.
4. Sign in as `judge-manager`. Create a workflow now selects the NVIDIA mode
   and says NVIDIA access configured. The free scripted option remains available.
5. Test all three separately reviewed agents, both handoff roles and evidence
   downloads through the actual HTTPS link from another laptop. Give judges the
   link and account credentials in Devpost testing instructions.

The key stays on the server. Do not publish it in GitHub, put it in JavaScript,
embed it in a ZIP, or hand it to judges. Keep the host, accounts, persistent data
and owner-funded inference available through the judging period. Configuration
means clients can be constructed; it does not verify balance, credits expiry,
provider connectivity or a completed live walkthrough.

## Your Windows laptop: use the ordinary launcher

Install the SDK from `requirements.txt` in the Python environment used by the
launcher. It chooses `.venv\Scripts\python.exe` when that file exists, otherwise
the Python launcher or `python` on PATH.

Configure your key once without putting it in a command or project file:

1. Close the running app server after stopping its owned workers.
2. In Windows Start, search Edit environment variables for your account.
3. Under User variables, add or edit `NEBIUS_API_KEY`.
4. Paste your real Nebius key as its value and save. Keep this settings screen
   private. This is a Windows user environment setting, not encrypted app storage;
   software running as that Windows user may read it.
5. Close old terminal and VS Code windows, then open a fresh session or launch
   start_operations.cmd from File Explorer so it receives the new setting.
6. Sign in and choose Create a workflow → NVIDIA Nemotron through Nebius.
   Select Balanced and 60 minutes for the full guided rehearsal.

Normal startup detects this owner-configured key. It does not prompt for a key
or make an inference request. Without a configured key, the NVIDIA option remains
visible but disabled with an owner-setup explanation; free scripted mode works.
For an isolated judge-account rehearsal, `start_live_judge_rehearsal.cmd` also
reuses the configured environment key.

The older `start_live_operations.cmd` is still available for an explicit
owner-only session. It may use one hidden terminal prompt if no environment key
exists. That prompt belongs to the owner, not a visitor of a hosted app.

## Explicit startup controls

```powershell
py -3 project.py operations serve --open --proposal-mode auto
py -3 project.py operations serve --open --proposal-mode scripted
py -3 project.py operations serve --open --proposal-mode live
```

Use `.\.venv\Scripts\python.exe` in place of `py -3` when that environment
contains the SDK. `auto` detects configured server access; `scripted` never
constructs live proposal clients; `live` explicitly requires owner configuration.
The legacy `--enable-live-proposals` flag remains supported.

Selecting NVIDIA saves a draft with zero model requests. Each Planning,
Governance and Recovery request needs its own review; service changes need their
separate exact approvals. An interrupted request is never automatically retried.
The optional support-ticket gateway remains separately enabled and labelled.

## Optional per-workflow browser key

The current Create a workflow dialog also supports an explicitly entered, masked Nebius key. This route is separate from the owner-configured instructions above. The key is held only in session memory and is not saved in runs or evidence. Use [NVIDIA_KEY_ENTRY.md](nvidia.md) for the direct no-SDK download route. The key field does not replace workspace account login.
