# Develop profile

`Develop` compiles one to three explicit rounds into the shared `Harness` goal.
Each round records PLAN, RESEARCH, BUILD, DEBUG, VERIFY and HEALTH task receipts.
Hosts supply five registered phase adapters; HEALTH is a controller check. The
fixed round count is chosen before admission, with per-task attempts, per-goal
units and shared tenant/global allowances retained across restarts. No hidden
retry or model-selected permission escalation is supplied. DEBUG observes and
prepares the next BUILD; only BUILD has candidate-write capability.

`CandidateWorkspace` is a bounded file tool with an explicit canonical path
allowlist, no links/hardlinks, exclusive temporary writes and atomic replacement.
Protected checks live outside the candidate; their digest, objective, allowed
paths and phase capabilities bind the goal. Hashes are checked around callbacks.
Non-BUILD candidate mutations, changed checks and model-forged verdicts produce
an uncertain attempt and human wait. This detects contract changes by trusted
callbacks; it is not an OS boundary against malicious host code or filesystem
races. Original example criteria are maker-owned, not independently protected.

Only the host VERIFY adapter can return a `VerifiedResult` for the bound goal.
The last round's VERIFY and HEALTH must both pass. Earlier failures remain in
the journal and retain their unit charges. HEALTH confirms the candidate still
matches its verification. `handoff` rechecks candidate and protected-check hashes;
editing after verification invalidates handoff and terminal resume. Handoff
authorizes no publication or deployment. Continuity is a controller projection
of receipts and status, with no full callback output/source included.

Run `python examples/develop_data.py` after installing the package. It transforms
original data through a candidate file, pauses after BUILD, reconstructs the host
controller, verifies and exports handoff. It runs trusted Python callbacks and
executes no candidate code. For untrusted check programs, compose VERIFY with
`DockerSandbox` and a trusted semantic verifier. Docker exit zero or a printed
PASS is insufficient criterion evidence. Five actual Linux boundary cases have
passed on the selected engine and native Linux installed-wheel CI. Each release
requires fresh candidate-bound results; see [release status](release-status.md).
