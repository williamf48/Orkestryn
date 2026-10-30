> Source layout note: run hosting commands from the repository root unless stated otherwise. Root launchers and current submission instructions are in the repository README.

# Host Orkestryn

The hosted entry point runs the same Python coordinator, SQLite journals, local
worker processes and three proposal roles as the desktop workspace. Visitors
need only a browser and the evaluation credentials. Their laptops do not need
Python or the source. The runtime still uses fixture capacity and ownership;
hosting does not turn it into a cloud GPU provisioning product.

The persistent `/var/data/agent-rehearsal` path is retained for existing
installations; the new product name does not require a data migration.

## Requirements

- One Python 3.10+ process on a Linux host that permits child processes and local TCP.
- One persistent writable directory outside the source checkout.
- An HTTPS reverse proxy that preserves the public `Host` header.
- A funded owner-provided Nebius Token Factory key for live evaluation.

Run a single instance with its own volume. A static host, GitHub Pages, and a
Cloudflare Worker cannot run this application's Python/process runtime. The
desktop `start_operations.cmd` remains local-only; this entry point is separate.

## Environment settings

Password login is permitted by the competition. Keep the existing three-account
flow. JUDGE_SETUP.md generates private credentials on your laptop
and exports host settings with the same passwords after you obtain an HTTPS
origin. Never publish its private files.

| Name | Value |
|---|---|
| `AR_PUBLIC_ORIGIN` | Exact assigned HTTPS origin, for example `https://your-service.example`; no trailing slash/path |
| `PORT` | Platform HTTP listener port; defaults to `10000` |
| `AR_STATE_ROOT` | Persistent directory; defaults to `/var/data/agent-rehearsal` |
| `AR_ADMIN_PASSWORD` | Unique 12-256 character password for `workspace-admin` |
| `AR_MANAGER_PASSWORD` | Different 12-256 character password for `judge-manager` |
| `AR_CUSTOMER_PASSWORD` | Different 12-256 character password for `judge-customer` |
| `AR_LIVE_PROPOSALS` | `1` enables separately approved live proposals; `0` starts free scripted mode |
| `NEBIUS_API_KEY` | Host secret, required when live proposals are enabled |
| `AR_MAX_OUTPUT_TOKENS` | Per-response limit, `1024`-`10000`; defaults to `8192` |

Set passwords and the API key in the host's secret settings. Do not put actual
values in Git, a public README, a video, or a command-line argument. Account
passwords are stored as salted scrypt hashes. The provider key stays in server
memory. Each paid role request still requires an explicit manager review.

Startup creates three accounts before listening. The administrator manages
accounts; the manager coordinates changes; the customer owner independently
approves the customer route and exact job release. Existing users, password
hashes, revoked roles and sessions are preserved on restart. Environment values
do not reset an existing password. Account administration remains authenticated.
There is no public registration or remotely usable first-account bootstrap.

## Judge-facing NVIDIA selection

With `AR_LIVE_PROPOSALS=1` and the owner key configured as a host secret, the
creation menu selects NVIDIA Nemotron through Nebius. Judges enter only their
workspace account credentials. The menu never collects, stores or sends an API
key. It shows an owner-setup explanation when live access is unavailable. See
[OWNER_LIVE_SETUP.md](owner-live-setup.md). Keep the existing hosted flag explicit;
the desktop launcher's automatic environment detection does not override it.

## Optional Render deployment

The included `render.yaml` uses a paid Starter web service and a 1 GB persistent
disk. Review Render's displayed price before creating it. Preparing this file
does not purchase or deploy a service. Its native Python entry point avoids
requiring Docker on your Windows laptop.

1. Publish the complete clean source under your own GitHub account.
2. Create the web service from that repository. Review the paid service/disk
   selection. Use `render.yaml` as the configuration reference or Blueprint.
3. Fill in the secret settings above and use the exact HTTPS origin Render
   assigns. No URL has been deployed or reserved by this package.
4. Keep the volume mounted at `/var/data/agent-rehearsal` and use one instance.
5. Start `python app/hosted_server.py`. `/healthz` must return `{"status":"ok"}`.
6. Test the public URL from a second laptop/private browser. Follow
   [JUDGE_TESTING.md](judge-guide.md) with both judge accounts and a fresh live run.
7. Keep access and owner-funded model inference available through judging.

Free Render web services lose local files on restart/sleep and cannot attach a
persistent disk. That would lose this app's SQLite accounts and history, so it
does not provide equivalent hosting for the current design. A downloadable
test build is a competition-supported alternative to buying hosting.

Render free-service and Blueprint references rechecked 27 October 2026:
[web services](https://render.com/docs/web-services),
[persistent disks](https://render.com/docs/disks),
[free-service limits](https://render.com/docs/free),
[Blueprint schema](https://render.com/docs/blueprint-spec).

## Container on another host

`Dockerfile` is an alternative deployment recipe. Build it from a clean release:

```sh
docker build -t orkestryn .
docker volume create rehearsal-state
docker run --name orkestryn --init --restart unless-stopped \
  --env-file /private/path/rehearsal.env \
  -v rehearsal-state:/var/data/agent-rehearsal \
  -p 127.0.0.1:10000:10000 orkestryn
```

The environment file is private and must contain the settings above. The
container runs as UID `10001`; that user must be able to write the volume. A
bind-mounted directory needs matching ownership. Configure your HTTPS proxy to
forward to the local port and preserve the public `Host`. Forwarded host/protocol
headers do not bypass the app's exact origin checks. Never expose this internal
HTTP listener directly as the public application URL.

The current environment can test real HTTP and account behavior but cannot build
Docker images, validate a Render deployment, or attest an actual TLS connection.
Those checks remain deployment acceptance steps, not passed results.

## Judging availability

Provide the actual deployed URL and all three evaluation account passwords in
Devpost's testing instructions. Judges must not need an invitation, your manual
approval to sign in, a payment, or their own Nebius account/key. The API key is
configured by the deployment owner and is never handed to a browser visitor.

Login sessions last thirty minutes. New workflows default to sixty minutes;
judges can choose fifteen, thirty or sixty minutes before creation. Signing back
in preserves an unexpired workflow and never renews its duration. These limits
are not deadlines on the judge accounts. Keep the host, accounts, disk and
inference access available until 24 December 2026, 1:30 AM IST (15 December
2026, noon Pacific). Freeze the submitted version before the submission deadline;
do not silently replace the judged implementation after the deadline.
