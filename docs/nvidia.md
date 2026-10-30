# NVIDIA Nemotron through Nebius

The fixed model is `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`. Direct requests go to `https://api.tokenfactory.nebius.com/v1/chat/completions`.

Main mode supports an owner-configured NEBIUS_API_KEY environment variable or privately entered browser session key. Judge-demo startup ignores inherited owner keys. In the demo, choose NVIDIA in Create a workflow and enter a privately supplied evaluation key; each role still needs a separate request review. No key is bundled, printed by the launcher or saved in exported evidence.

Planning, Governance and Recovery use the existing structured proposal contracts. Safety Lab uses strict JSON schemas for candidate tools/arguments and monitor decision/risk/reason. Schema compliance does not establish permission or safety: independent policy checks and scoring remain separate.

Failed, incomplete and unconfirmed requests stay counted. There is no automatic retry or schema fallback. Saved metadata distinguishes provider-reported token usage from local diagnostics. Keep paid candidate generation and paid monitor review separate; re-evaluating saved evidence is free.

Source entry points: app/browser_proposals.py, app/operations_proposals.py, app/safety_live.py and app/inference_gateway.py. The core direct browser-key flow uses urllib and Python's standard library. Optional legacy SDK integrations use the pinned dependency in requirements.txt.

Hosted and scoped-relay alternatives are documented in [hosting](hosting.md), [owner setup](owner-live-setup.md) and [relay setup](relay-setup.md). They need real deployment and owner-funded access configuration; documentation alone is not a live judge endpoint.
