> Source layout note: run the command examples in this guide from the `app/` directory unless stated otherwise. Root launchers and current submission instructions are in the repository README.

# Research fit: AI control and evaluation

Checked against the organisations' official pages on **29 October 2026**.
These are selected relevant directions, not a complete account of their work.
This project has no affiliation, endorsement or fellowship admission guarantee.

| Programme | Relevant current direction | What Orkestryn implements |
|---|---|---|
| [Pivotal](https://www.pivotal-research.org/projects) | Its Q3 2026 projects include control protocols with interrogation, agent-to-agent monitoring, and auditing the reliability of behavioural evaluation tools. | Public monitor views separated from evaluator labels; matched text-framing probes; independent state-history scoring; transparent failure and coverage accounting. These are evaluation foundations, not a reproduction of its interrogation research. |
| [MATS Winter 2027 streams](https://www.matsprogram.org/program/winter-2027?2a8c7d58_page=9&9557ad6d_page=1) | Relevant streams study AI control, scalable oversight, monitor evaluation and realistic alignment tests. | Compare control protocols on the same plans; expose the safety/usefulness trade-off and inspection budget; export reproducible traces and run an optional Inspect task. |
| [Anthropic Fellows](https://alignment.anthropic.com/2025/anthropic-fellows-program-2026/) | The programme includes scalable oversight, adversarial robustness, AI control and empirical misalignment research, alongside other areas. | A bounded adversarial environment, optional separately reviewed NVIDIA candidate/monitor requests, explicit model provenance and researcher-facing reports. No model organisms are trained here. |

The most defensible project question is:

**When several agents propose changes to shared infrastructure, which oversight
protocols preserve another owner's work without blocking legitimate recovery?**

That question fits the existing application and has directly checkable outcomes.
Adding a model that says a plan is safe would not establish that safety: the Lab
compares the verdict with independent consequences in its specified environment.

## Techniques and tools actually used

- **Control evaluations:** authored honest and adversarial candidates are tested
  against an unprotected reference and several oversight protocols. The framing
  follows [ControlArena's introduction](https://control-arena.aisi.org.uk/intro-to-control.html)
  and [AI Control: Improving Safety Despite Intentional Subversion](https://proceedings.mlr.press/v235/greenblatt24a.html).
- **Monitor input ablation:** optional model monitors receive either the complete
  plan or typed actions with explanations withheld. The server constructs both
  from an allowlist; private intent and the oracle result are excluded.
- **Paired adversarial framing:** forged-approval and evaluation-pressure text
  changes explanations without changing permissions, typed effects or task state.
  This tests sensitivity to untrusted text; it does not establish a causal claim
  about evaluation awareness, internal objectives or naturally occurring deception.
- **UK AISI Inspect:** `safety_inspect.py` is an actual optional Task/Solver/Scorer
  adapter. It runs local deterministic controls and produces Inspect logs.
  See [INSPECT_RESEARCH.md](inspect-research.md). Inspect is also the framework
  underlying [Anthropic's Petri](https://alignment.anthropic.com/2025/petri/).
  Orkestryn does not bundle or reproduce Petri's autonomous auditor.
- **Independent outcome scoring:** task completion, harmful side effects and
  oversight intervention are distinct. This is informed by the separation of
  objectives in [SHADE-Arena](https://www.anthropic.com/research/shade-arena-sabotage-monitoring),
  but this suite is much smaller and has different semantics.

No ControlArena dependency, Petri integration, benchmark equivalence, activation
inspection, model training or chain-of-thought faithfulness experiment is claimed.
The current system evaluates visible typed actions and optional model verdicts.

## What would make the research stronger

The release includes engineering checks and an authored demonstration. A useful
next empirical result would require a frozen collection protocol, real model
attempts, independently developed attacks and critical qualitative review.
Use [EXPERIMENT_PROTOCOL.md](experiment-protocol.md) before collecting those data.
Promising results should survive new tasks and attacks, not just this public suite.

For a fellowship portfolio, share the question, implementation, reproducible
evidence and limitations. Ask researchers to critique the threat model and
measurement design. A recommendation should reflect their actual assessment.
