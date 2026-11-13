# Labelled PRs

`labels.json` holds 88 cases from the shadow trial, each a vLLM PR at a pinned
base and head with a verdict on which test steps it needs. 64 cases come from
the deep review of 2026-09-30 and 24 from the review of posted shadow comments
on 2026-10-03; `record` is the coverage record each was scored against.

The verdicts follow the lenient standard (2026-10-12). Every runnable PR-time
step at the head commit was judged from the diff and the step definitions
alone, without seeing what the selector picked. A step is `needed` when its
tests run the changed code with the change active and a bug in the change
could make it fail, for one of four reasons:

- direct: its command collects a test file the PR changed or added, and the
  file is not deselected by marker or skipped on the step's hardware;
- exercises: its tests run the changed behaviour with its gate open (config,
  request parameter, model, quantization, backend, hardware, TP/DP shape) and
  assert on it, including accuracy and parity runs through the change;
- witness: for an always-on change no step above covers, one cheap GPU step
  that goes through it;
- gate: build, lint and yaml checks the change triggers, and any step whose
  own yaml block the PR adds or edits.

Every step that qualifies counts, not only the cheapest. Two rules from a vLLM
maintainer's review narrow it: a CPU step is not needed when it only runs
shared Python a GPU step already runs and the PR touches no CPU-specific code,
and a step that only passes through a plumbing change (an assert, a config
pass-through) while testing something else is not needed. ROCm-only paths
count through the NVIDIA parent step of their AMD mirror. The 88 cases have
361 needed steps and 2 uncertain; `unneeded` lists every other step judged.

`missed_by_review_run` and `reviewed_selection` record what the original
reviews saw of the selector's run at the time; they are history, not labels.

```bash
uv run ci-validate score --repo /path/to/vllm --json-out after.json --baseline before.json
```

`--repo` needs every case's base and head; fetch them with
`git fetch origin pull/<N>/head` or by commit. Records are fetched into
`coverage-data/by-commit/` by commit. The number that matters is "needed steps
dropped against the baseline", which lists each one.
