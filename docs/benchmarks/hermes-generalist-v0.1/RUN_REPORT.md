# Hermès benchmark run report template

Template only. No scores have been recorded.

## Configuration to record before execution

Record benchmark version and file hashes; pack and exact case IDs; run date; operator; Hermès image digest; requested and actually served model; model settings; actor profile version and hash; effective policy and protocols; tool schema versions; fixture and evaluator versions; sandbox image; network policy; reset procedure; and time, token, tool-call and cost limits.

For live web, also record source allow-list, retrieval timestamp, snapshot references, cutoff interpretation and independent adjudicator. For user simulation, record the fixed interaction script or simulator configuration. State whether the baseline uses the same underlying model and tool access as Hermès.

## Trial ledger

Use one row per case and trial. Keep detailed traces and artifacts linked by immutable identifiers outside the actor environment.

| Case | Trial | Track | Status | Outcome checks | Boundary behavior | Platform probes | Evidence references | Time | Tokens | Cost | Intervention or failure reason |
|---|---|---|---|---|---|---|---|---|---|---|---|

Allowed status values: `passed`, `failed`, `unsupported`, `invalid_fixture`, `infrastructure_error`, `not_run`. Platform probes may be `not_applicable`; their absence must not be reported as passed.

## Summary to produce after execution

Report planned cases, supported cases, valid attempted trials, successful trials, unsupported cases and reasons, invalid fixtures, infrastructure errors, and cases not run. Then report per-domain and per-track per-trial success and the number passing all three valid trials. Include raw counts with every percentage.

Report total cost including failed attempts, cost per success, median and tail latency when sample size supports it, tool errors, human interventions, unauthorized attempts, unauthorized effects, unsupported claims, and citation failures. Do not claim meaningful tail estimates from a handful of observations.

## Review

Separate actor reasoning failures, tool/connector failures, platform enforcement failures, and benchmark defects. Preserve the evidence needed to reproduce each failure. Explain capability coverage and uncertainty before comparing configurations. Record any task or scoring changes as a new version before rerunning.
