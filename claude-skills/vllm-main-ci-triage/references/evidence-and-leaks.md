# Evidence and selection-leak contract

## Minimum incident evidence

- exact Buildkite build number, job ID, job key, URL, and terminal state;
- first causal error plus the test summary or proof that tests never started;
- retry and recent exact-job history;
- relevant commit, pull request, diff, and last-good/first-bad boundary;
- exact state of the same job on the suspected culprit pull request;
- a narrow fix, revert, reproduction, or direct code path that confirms cause.

Blocked is a valid non-running state only on a terminal PR build when the job
has no start time or assigned agent. Absence from a PR build is `not_selected`,
not success.

## Positive corpus rule

Add one record per `(culprit PR, exact job, main failure job)` only when:

1. `pr_ci.ran` is false;
2. the main failure is a source regression caused by that PR; and
3. causality has evidence stronger than chronological proximity.

Exclude infrastructure failures, external outages, known baselines, flaky
tests without a source regression, and exact jobs that ran in PR CI. A test
inside a passing job being omitted by an inner shard is a related but distinct
selection problem and must not be mislabeled as an exact-job leak.

## Data hygiene

Use full immutable merge SHAs and exact Buildkite job anchors. Keep the concise
failure signature semantic rather than copying volatile timestamps or ANSI
logs. Preserve historical names. Sort records by main build and job name, run
the validator, and use frozen temporal snapshots for evaluation.
