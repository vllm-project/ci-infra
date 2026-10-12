# Labelled PRs

`labels.json` holds 88 cases from the shadow trial, each a vLLM PR at a pinned
base and head with reviewers' verdicts on which test steps it needs:

- 64 from the deep review of 2026-09-30, scored against the record of build
  92249. `needed` there includes the steps that review called uncertain.
- 24 from the review of posted shadow comments on 2026-10-03, scored against
  the record each comment used, with `uncertain` itemised.

Steps nobody named count as unneeded: reviewers looked for misses among
everything the selector skipped.

```bash
uv run ci-validate score --repo /path/to/vllm --json-out after.json --baseline before.json
```

`--repo` needs every case's base and head; fetch them with
`git fetch origin pull/<N>/head` or by commit. Records are fetched into
`coverage-data/by-commit/` by commit. The number that matters is "needed steps
dropped against the baseline", which lists each one.
