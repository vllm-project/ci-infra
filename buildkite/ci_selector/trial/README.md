# CI selector shadow-trial data

Data from the shadow trial of the evidence-based CI test selector (see the
parent `ci_selector/` README). During the trial, one comment is posted on
selected vLLM PRs comparing what the selector would run with what today's CI
rules run, and updated with actual CI outcomes afterwards. Nothing in the
trial changes what CI runs.

## Files

- `prs.txt` — one line per PR covered: `<date> <PR number>`.
- `ledger.jsonl` — one JSON object per `ci-select pr` run. Fields include:
  `pr`, `title`, `base`, `head`, `files`, `run_all` (why the selector ran
  everything, when it did), `today_steps`/`today_jobs` (what today's rules
  run), `selector_steps`/`selector_jobs`, `would_skip`, `would_add`, and the
  CI outcome fields once the `--results` update lands.

New entries are appended as the trial progresses. Report issues to Kevin
(kevin@inferact.ai), who owns the selector.
