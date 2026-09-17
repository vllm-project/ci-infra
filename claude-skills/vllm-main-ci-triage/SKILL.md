---
name: vllm-main-ci-triage
description: Diagnose hard vLLM main-CI failures, open narrow source-fix PRs when authorized, audit the culprit PR's exact job coverage, and record confirmed test-selection leaks. Use when monitoring the vLLM main-CI dashboard, investigating a failed Buildkite main or daily job, deciding whether a failure is source-related or environmental, or updating the ci-infra selection-leak corpus.
---

# vLLM Main CI Triage

Triage hard failures from this filtered view:

`https://vllm-ci-dashboard.vercel.app/alerts?tab=main-ci&window=1d&hide=softfail%2Coptional%2Camd`

The dashboard is an index, not evidence. Open the exact Buildkite job and read
the first causal error, relevant test summary, agent identity, retry history,
and teardown output before classifying it.

## Investigate

1. Deduplicate against open dashboard alerts, recent Slack/Raft incidents,
   GitHub issues, and fix or revert PRs.
2. Compare the exact job with recent main and daily executions. Separate the
   first causal error from cleanup, worker-loss, timeout, and cancellation
   cascades.
3. Classify the failure as source regression, test defect, infrastructure,
   external dependency, known baseline, or unknown. State confidence and link
   exact evidence.
4. For a suspected source regression, establish the last-good/first-bad
   boundary and inspect only relevant commits and diffs. Do not assign a
   culprit from timing alone.
5. Inspect the culprit PR's exact job state. Overall PR green is irrelevant:
   record whether that precise job passed, failed, was blocked, was skipped,
   or was not selected.

Retry once only when evidence supports a transient failure. Do not use
retries to hide a repeated signature or a deterministic source failure.

## Fix and report

When the current request authorizes external writes, search for duplicate
work, create the smallest source or harness fix, run focused local validation,
and open a PR with exact failure evidence. Never merge, revert, deploy, cancel
jobs, change runner state, or resolve an incident unless that action is also
authorized. Include a plain disclosure when AI materially assisted the patch.

Post a brief lifecycle update on the authorized team surface only when there
is actionable new evidence. Include the exact main job, normalized signature,
classification, cause, fix PR, and validation state. Keep credentials and raw
environment files out of chat and commits.

## Record selection leaks

Read [references/evidence-and-leaks.md](references/evidence-and-leaks.md) before
adding a row. Update `test-selection/selection-leaks.json` only when the exact
PR job did not run, the post-merge failure is source-related, and causality is
confirmed. Then run:

```bash
python3 test-selection/validate_selection_leaks.py
```

Treat each affected job as a separate record. Do not record cases where the
exact job ran in PR CI, even when its internal test sharding missed the failing
case; those require a different corpus and label.
