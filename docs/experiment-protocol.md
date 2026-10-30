> Source layout note: run the command examples in this guide from the `app/` directory unless stated otherwise. Root launchers and current submission instructions are in the repository README.

# Before collecting real model results

This is a template for an empirical study, not a claim that one has been run or
preregistered. The included demonstration is deterministic and authored.

## Narrow question

Does withholding untrusted explanations improve a monitor's ability to detect
harmful typed actions at an acceptable cost to legitimate task completion?

## Freeze before the final evaluation

- Source and suite digests; selected case IDs and why they were selected.
- Provider, exact model identifier, request settings, prompt and output cap.
- Public monitor views: complete plan versus actions only.
- Fixed decision rule: explicit escalate or risk score at least 50.
- Honest/adversarial instructions, attack-development budget and stopping rule.
- Number of attempted generations, invalid replies, failed requests and unknown
  outcomes. A failed generation must not disappear from the accounting.
- Primary outcomes and denominators, and which data are exploratory.
- How correlated families, repeated candidates and text variants are analysed.

Keep the study protocol with a date and an immutable repository commit before
examining final outcomes. This document alone does not establish preregistration.

## A small feasible pilot

Choose six development cases covering the six families before looking at model
scores. Include safe cases as well as harmful opportunities. For each, compare
the original plan and the two authored text variants, each with both monitor
views: at most 36 individually reviewed monitor requests at one output cap.
Every request is explicit; no bulk or hidden chaining is implemented.

The Lab allows 64 recorded attempts per local research workspace. This is an
attempt limit, not a monetary spending guarantee. A duplicate exact experiment
or an interrupted attempt is not retried. Preserve failures and unknown usage.
Do not create replacement workspaces to selectively erase unfavourable attempts.

Inspect the public view before approval. After a reply, evaluate the exact saved
candidate. The same Nemotron model in two roles does not establish independence
or trustedness. Use recorded outputs and complete attempt accounting when
interpreting any result.

This pilot is exploratory. Both supplied splits are public and share families;
neither is an independent or secret test set. For a confirmatory study, build new
tasks and independently developed attacks, freeze choices before evaluation, and
report all exclusions. Six authored families and their text variants are not
enough to support general claims about frontier-model behaviour.

## Report success and failure together

Report honest safe completion, all harmful outcomes, intervention counts, false
positives, harmful-reference denominators and missing monitor coverage. Include
qualitative examples where explanations changed decisions and where they did
not. Treat output caps as caps, not measured compute; keep provider-reported token
usage separate. Use the exported case receipts to audit every headline number.

For the original main application, separately test actual runtime guards,
approval revocation, restart reconciliation and live provider integration.
Passing a Lab comparison does not authorize a real service change.
