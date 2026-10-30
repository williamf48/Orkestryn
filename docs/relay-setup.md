> Source layout note: run the command examples in this guide from the `app/` directory unless stated otherwise. Root launchers and current submission instructions are in the repository README.

# Live judge access without distributing the Nebius key

This release adds an **owner-hosted inference relay** for the downloadable app.
The Windows application, local workers, approvals and saved runs stay on the
judge's computer. Only the three separately approved proposal requests go to
the relay, which calls NVIDIA Nemotron through Nebius Token Factory.

**Status: prepared code, not a deployed service.** A host account, private host
secrets and an actual live acceptance walkthrough are still required. Offline
tests use mock provider responses and do not prove Nebius connectivity, credits,
free-tier capacity or availability through judging.

## Owner: deploy once

Cloudflare Workers and D1 currently have free tiers. Nebius inference consumes
your Token Factory balance separately. Choose the Free plan; no paid plan is
required by this implementation. Check your account's limits before deployment.

1. Sign in to your own Cloudflare account at <https://dash.cloudflare.com/>.
   Under **Workers & Pages**, create a Worker named `orkestryn-relay`.
   Use its assigned `https://...workers.dev` address. No custom domain or PC
   tunnel is needed. Leave this service disabled until the steps below are done.
2. Open the Worker editor. Replace its example module with the complete contents
   of `judge_relay_worker.mjs`. This file contains the fixed registered prompts;
   it needs no npm dependencies or imports. Save/deploy the module.
3. Under **Storage & databases → D1**, create a dedicated database named
   `orkestryn-relay`. In its SQL console, execute the complete contents of
   `judge_relay_schema.sql` once. In your Worker **Bindings**, add that D1 database
   with the binding name **`DB`**. Keep this ledger for the judging period.
4. On your Windows computer, open a terminal in the extracted project. Generate
   a new private owner setup, substituting your actual assigned URL:

   ```powershell
   py -3 relay_demo.py prepare-owner --origin https://YOUR-WORKER.YOUR-SUBDOMAIN.workers.dev --directory "$env:LOCALAPPDATA\AgentRehearsal\RelayOwnerSetup"
   ```

   Open the printed `RELAY_OWNER_PRIVATE.txt` locally. It contains a generated
   evaluation password. This file and `relay_connection_private.json` are
   private; neither belongs in the repository or public release ZIP. Re-running
   setup never replaces them. Use the originals if they already exist.
5. In Worker **Settings → Variables and Secrets**, add three values of type
   **Secret**:

   | Name | Private value |
   |---|---|
   | `NEBIUS_API_KEY` | Your real Nebius Token Factory API key |
   | `RELAY_USERNAME` | The evaluation username from the private setup |
   | `RELAY_PASSWORD` | The evaluation password from the private setup |

   Never paste the provider key into source, a terminal command or ChatGPT.
6. Add these ordinary Worker variables:

   | Name | Value |
   |---|---|
   | `RELAY_ENABLED` | `1` after all other settings are ready |
   | `MAX_ATTEMPTS` | `1200` initially; owner-adjustable global attempt ceiling |
   | `ACCESS_UNTIL` | `2026-12-17T00:00:00Z` |

   Deploy the updated settings. `/health` reports whether configuration exists;
   it makes zero provider calls and does not verify your balance or API key.

The global attempt ceiling counts successful, failed and uncertain starts.
Monitor it and maintain enough allowance and inference credit for legitimate
judging. Increase it when needed; do not delete or reset the ledger. A fixed
output-token limit is not a monetary spending cap. The access date above extends
beyond the published judging end, but the owner must keep the host and credentials
available and check that credits remain usable.

Cloudflare dashboard labels can change. Primary documentation:
[Workers dashboard setup](https://developers.cloudflare.com/workers/get-started/dashboard/),
[secrets](https://developers.cloudflare.com/workers/configuration/secrets/),
[D1 setup](https://developers.cloudflare.com/d1/get-started/),
[Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/),
[D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/).

## Judge: download and enter the supplied evaluation login

The owner supplies three items privately in Devpost testing instructions:
the actual relay URL, the evaluation username, and the evaluation password.
These are scoped relay credentials, **not a Nebius API key**. The username is
`judge` unless the deployment owner intentionally changes it.

1. Install Python 3.10 or newer if necessary. Download and **Extract All** files.
2. Double-click **`START_JUDGE_LIVE.cmd`**. On first use, enter the provided relay
   URL and evaluation password in its hidden terminal prompt. No SDK or pip
   installation is needed. Settings are saved privately for later launches.
3. The welcome page opens with three generated **local application** logins.
   These are separate from the shared relay access password. Sign in as
   `judge-admin` (or the separate manager account) and create **NVIDIA Nemotron through Nebius → Balanced →
   60 minutes**. Creating a workflow sends no model request.
4. Follow the welcome page: separately review Planning and Governance, approve
   the exact batch, start observation, approve the development fault, review
   Recovery, approve recovery effects, and complete the two handoff roles and
   exact finished-customer-job release.
5. Inspect the outcome report, stop the observer and owned workers, and download
   the evidence ZIP. Live attempts and reported tokens are recorded separately
   from scripted fixtures. An invalid/refused/deferred result can stop the flow;
   the app never silently substitutes a scripted response.

The live workspace is a separate `relay_workspace` directory beneath the private
downloadable-demo data directory. Existing William accounts and free-demo runs
are preserved. Relaunching preserves local passwords and saved live runs.
The console must remain open while this local app is being used. The owner's PC
does not need to remain on for the hosted relay to work.

`START_DEMO.cmd` remains the explicitly free scripted route. The original
`start_operations.cmd` supports your existing direct owner-key configuration.
This relay route does not enable the optional support-ticket inference gateway.

## Owner: actual acceptance before submission

Test the published HTTPS relay from another computer, using the same release
and credentials judges will receive. Complete all three real proposals, the
role-separated effects and the evidence download. Check Token Factory usage.
Use the real integration in the public video; keep passwords and API keys out
of its recording. Describe fixture capacity values honestly.

A prepared relay ZIP, a green `/health`, and offline mocks are not a completed
live deployment. The complete app must remain freely testable through judging.
Do not describe contact-only access as organizer-approved. The official rules
allow a test build and allow private website credentials; they do not explicitly
approve a scripted-only replacement for live NVIDIA functionality.

## Request and privacy boundaries

- HTTPS only. No redirects, proxy-environment credential forwarding or automatic
  retries. Each request is bound to its workflow, role, exact review and context.
- The relay fixes the NVIDIA model, Nebius endpoint, system prompts and 8192
  maximum output tokens. Clients cannot choose a destination, tools or a model.
- D1 reserves a unique attempt before contacting Nebius. Concurrent duplicates
  cannot make another paid request. Exact completed duplicates return the saved
  receipt; uncertain attempts remain blocked. The app itself never retries them.
- Factory, Governance and Recovery remain ordered, with the original workflow
  deadline preserved. Each request needs separate local manager consent. The
  relay does not grant any local service-change or customer-owner permission.
- The relay stores request fingerprints, statuses, model replies and reported
  usage. It does not intentionally persist request contexts or passwords. The
  contexts necessarily travel to the relay and Nebius; use synthetic demo data.
- The evaluation password authorizes bounded model requests and consumes the
  owner's credit. Keep it private, monitor the global attempt allowance, and
  rotate it if necessary while updating judges' testing instructions. It does
  not expose the provider key or permit local process control.

## Alternative without relay hosting

The latest app also supports direct per-workflow key entry. If an evaluation key is supplied privately, judges can choose NVIDIA in Create a workflow and paste it into the masked field. That direct route needs neither a Cloudflare deployment nor a provider SDK. The relay instructions above describe the optional route that keeps the provider key solely on the owner host. See [NVIDIA_KEY_ENTRY.md](nvidia.md).

The live relay launcher also generates **judge-admin** for one-login evaluation. Both handoff reviews remain separate; outcome evidence identifies a single account when it approves both. See JUDGE_DASHBOARD.md.
