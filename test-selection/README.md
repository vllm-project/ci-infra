# Test-selection leak corpus

`selection-leaks.json` is the canonical, versioned set of confirmed vLLM CI
selection leaks. Each row identifies an exact CI job that did not execute on a
pull request and then exposed a source regression after that pull request
merged.

The corpus is job-granular: one pull request can contribute several rows when
several exact jobs were missed. Keep the recorded Buildkite job IDs and URLs;
an aggregate green pull-request build is not evidence that a particular job
ran.

## Inclusion gate

Add a row only when all of these are true:

1. The exact job was blocked, not selected, skipped, or canceled on the
   culprit pull request. A terminal Buildkite job with no start time or agent
   is acceptable proof that it did not execute.
2. The corresponding post-merge main or daily job failed because of source
   behavior, not mutable infrastructure or an external service.
3. The cause is confirmed by a narrow fix, revert, reproduction, or direct
   code-level analysis that connects the culprit diff to the failure.

Do not add infrastructure flakes, baseline failures, external outages, or
cases where the exact job executed in pull-request CI. Keep rejected
candidates outside this positive corpus; they are useful negative examples
for model evaluation but have a different label.

## Update and validate

Keep records sorted by main build number and then job name. Use stable IDs and
never rewrite historical evidence because a job or pipeline is later renamed.

```bash
python3 test-selection/validate_selection_leaks.py
```

`selection-leaks.schema.json` documents the machine-readable contract. For
evaluation, use a frozen temporal snapshot or holdout rather than randomly
splitting rows from the same incident across train and test sets.
