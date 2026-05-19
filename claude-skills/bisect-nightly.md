# Bisect vLLM Nightly CI Failures

## Requirements

This skill requires the following MCP servers to be connected:
- **Buildkite MCP** — for listing builds, getting build details, reading/searching logs, and unblocking jobs
- **Slack MCP** — for posting findings to #sig-ci (channel ID: `C07R5PAL2L9`) via canvas and messages

It also uses:
- **GitHub CLI (`gh`)** — for listing commits and reading diffs from `vllm-project/vllm`
- **CI Dashboard API** — web endpoint at `vllm-ci-dashboard.vercel.app` for job pass/fail history across builds

---

## Reference: CI Dashboard Builds Summary API

Base URL: `https://vllm-ci-dashboard.vercel.app/api/builds/summary`

Use `curl` or `WebFetch` to call this endpoint. It returns a compact plain-text summary (by default) showing build number, state, commit, author, duration, PR number, commit message, test group statuses, and individual failed job names.

### Parameters

| Param | Example | Description |
|-------|---------|-------------|
| pipeline | CI | Pipeline name (e.g. CI, CI%20(AMD)) |
| branch | main | Git branch name |
| state | failed | Build state: passed, failed, running, canceled |
| startDate | 2026-05-15 | Start of date range (YYYY-MM-DD) |
| endDate | 2026-05-19 | End of date range (YYYY-MM-DD) |
| jobNames | Kernels%20(B200) | Comma-separated job names (URL-encoded). Filters to builds containing these jobs. |
| jobGroups | Kernels,Distributed | Comma-separated test group names. Groups: Attention, Basic Correctness, Benchmarks, Compile, CUDA, Distributed, E2E Integration, Engine, Entrypoints, Expert Parallelism, Hardware, Hardware - AMD, Kernels, LM Eval, LoRA, Miscellaneous, Model Executor, Model Runner V2, Models - Basic, Models - Distributed, Models - Language, Models - Multimodal, Plugins, PyTorch, Quantization, Ray Compatibility, Samplers, Spec Decode, Weight Loading |
| per_page | 10 | Results per page (1-30, default 10) |
| page | 0 | Page number (0-indexed) |
| format | text | text (default, compact) or json |
| jobs | false | Set false to hide per-group failure details |

### Output format

When `jobNames` is specified, the endpoint returns **per-job PASS/FAIL/RUNNING state directly** for each build — one line per job per build. This is the easiest case: just look at the explicit state for the specific job you care about.

When `jobNames` is NOT specified, each build shows:
- Build number, state, date, commit, author, duration, PR number
- Commit message
- Test group statuses in brackets: `ok` = all passed, `1F/4` = 1 failure out of 4 jobs, `--` = not run, `..` = still running
- List of individual failed job names per group

### Example calls

```
# Recent failed builds on main
curl "https://vllm-ci-dashboard.vercel.app/api/builds/summary?pipeline=CI&branch=main&state=failed&per_page=10"

# Builds where a specific job ran
curl "https://vllm-ci-dashboard.vercel.app/api/builds/summary?pipeline=CI&branch=main&jobNames=Kernels%20(B200)&per_page=10"

# Builds from a specific date range
curl "https://vllm-ci-dashboard.vercel.app/api/builds/summary?pipeline=CI&branch=main&startDate=2026-05-15&endDate=2026-05-19"
```

---

## Step 1: Find the latest full CI run

List recent builds on the main CI pipeline (up to 100) and find the first one whose message contains "Full CI run":

```
list_builds(org_slug: "vllm", pipeline_slug: "ci", branch: "main", per_page: 100)
```

Scan the results for a build with `"Full CI run"` in the message. There are two variants:
- `Full CI run - nightly` (runs at ~06:00 UTC daily)
- `Full CI run - daily` (runs at ~21:00 UTC daily)

Note the build's `number`, `commit`, and `state`.

## Step 2: List non-optional failing jobs

Get the build details filtered to failed/broken jobs:

```
get_build(org_slug: "vllm", pipeline_slug: "ci", build_number: "<number>", job_state: "failed,broken", detail_level: "full")
```

Filter out jobs where `soft_failed: true` — those are optional (e.g. "Torch Nightly", "Intel HPU Test", "Transformers Backward Compatibility"). The remaining jobs are the real failures to investigate.

## Important: Repeat steps 3-5 for every non-optional failing job

Steps 3 through 5 must be performed independently for **each** non-optional failing job from step 2. Each job may have a different suspicious commit range, different suspect commits, and different root causes. Do not assume that because one job's failure was caused by commit X, all other jobs failed for the same reason.

For each failing job:
1. Find its suspicious commit range (step 3)
2. Analyze the commits in that range against that job's specific failure logs (step 4)
3. Unblock and test on suspect commits (step 5)

Work through all failing jobs systematically.

---

## Step 3: Find the suspicious commit range for each failing job

For each non-optional failing job, use the CI dashboard builds summary API with the **exact** job name in `jobNames`:

```
curl "https://vllm-ci-dashboard.vercel.app/api/builds/summary?pipeline=CI&branch=main&jobNames=<URL-encoded job name>&per_page=30"
```

When `jobNames` is specified, the endpoint returns the **per-job state directly** — each build will have a line like `Kernels (B200): PASS` or `Kernels (B200): FAIL` or `Kernels (B200): RUNNING`. Just read that.

**How to find the suspicious range:**
1. Fetch enough results (use `per_page=30`, page through with `page=0`, `page=1`, etc.)
2. Walk backwards from the most recent build until you find the first PASS — that's the **last pass commit**
3. The build right after that with FAIL is the **first fail commit**
4. The range is `<last_pass_commit>..<first_fail_commit>`

**Watch for intermittent failures.** A job may fail once, then pass again for several builds, then start failing consistently. Focus on the **sustained** failure streak — find the last pass before the current unbroken streak of failures. Isolated earlier failures are likely flakes and a separate issue.

**Watch for duplicate runs on the same build.** If you see a job listed twice for the same build (e.g. one PASS and one FAIL), it means the job was retried or unblocked multiple times. Treat that build as flaky and look at neighboring builds for a clearer signal.

**Do NOT infer from group status.** If the endpoint is returning group-level status (`Kernels:ok` / `Kernels:1F/22`), it's the OLD format — request the endpoint with `jobNames=<job>` to get direct per-job state. The group `ok` does NOT mean a specific job passed (could mean it didn't run); group `1F/N` doesn't tell you WHICH job failed.

**Beware of substring matches.** If you ever need to grep failure details by job name, use anchored patterns. For example, "Spec Decode Draft Model" is a substring of "Spec Decode Draft Model Nightly B200" — use a regex like `Spec Decode Draft Model(?! Nightly)` to avoid false positives.

**Get full SHAs before running `gh api compare`.** The dashboard prints short 7-char SHAs but GitHub's compare API often needs full 40-char SHAs. Run `gh api repos/vllm-project/vllm/commits/<short_sha> --jq '.sha'` first to get the full SHA, then use those in the compare call.

The suspicious commit range is `<last_pass_commit>..<first_fail_commit>`. All commits that landed on main between these two builds are candidates for the regression.

## Step 4: Analyze commits in the suspicious range against failure logs

### 4a. List all commits in the range

Use the GitHub CLI to list all commits between the last passing and first failing commits:

```
gh api repos/vllm-project/vllm/compare/<last_pass_commit>...<first_fail_commit> --jq '.commits[] | "\(.sha[:9]) \(.commit.message | split("\n")[0])"'
```

This gives a compact list of every commit that landed on main in the suspicious window.

### 4b. Read the diff of each commit

For each commit in the range, fetch its diff to understand what changed:

```
gh api repos/vllm-project/vllm/commits/<sha> --jq '.files[] | "\(.filename) (\(.status), +\(.additions)/-\(.deletions))"'
```

Or for the full patch:

```
gh api repos/vllm-project/vllm/commits/<sha> -H "Accept: application/vnd.github.v3.diff"
```

### 4c. Get the failure logs from the failing build

Use the Buildkite tools to read the logs from the failing job:

```
tail_logs(org_slug: "vllm", pipeline_slug: "ci", build_number: "<first_fail_build>", job_id: "<job_id>")
```

Or search for error patterns:

```
search_logs(org_slug: "vllm", pipeline_slug: "ci", build_number: "<first_fail_build>", job_id: "<job_id>", pattern: "error|FAILED|AssertionError", limit: 20)
```

### 4d. Cross-reference diffs with failure logs

Compare what each commit changed against what the failure logs show:
- Which files/modules did the failing test exercise?
- Which commits touched those same files or related code paths?
- Do the error messages (e.g. import errors, assertion failures, missing attributes) point to specific changes?

Rank the commits by suspicion level. The most suspicious commit is the one whose diff most directly overlaps with the code paths in the failure.

## Step 5: Unblock the failing job on suspect commit builds

Each per-commit CI build on vLLM has most jobs behind block steps (manual gates). To test whether a suspect commit introduced the regression, find the **existing** CI build for that commit and unblock the failing job on it.

**NEVER create a new build.** Every commit merged to main already has a CI build triggered by webhook. Find that existing build and unblock the specific job you need to test.

### 5a. Find the CI build for each suspect commit

```
list_builds(org_slug: "vllm", pipeline_slug: "ci", commit: "<full_sha>")
```

### 5b. Find the block step for the failing job

Get all blocked jobs from the build:

```
get_build(org_slug: "vllm", pipeline_slug: "ci", build_number: "<number>", job_state: "blocked", detail_level: "full")
```

Search for the block step (type: "manual", name: "") whose `step_key` matches the pattern `block-<job-name-kebab-case>`. For example, "Kernels (B200)" has step_key `block-kernels-b200`.

Note: not every build has every job — the pipeline definition may vary across commits. If the job doesn't exist in a build, skip it and try the next suspect.

### 5c. Unblock the job

```
unblock_job(org_slug: "vllm", pipeline_slug: "ci", build_number: "<number>", job_id: "<block_step_id>")
```

This triggers the blocked job to run. Wait for it to complete, then check if it passed or failed.

### 5d. Confirm by running the commit right before

When the job **fails** on a suspect commit, always unblock and run the same job on the commit **immediately before it in the commit list**. This confirms the bisect — if the previous commit passes and the suspect fails, the suspect is the culprit.

**Critical:** "The commit right before" means the **adjacent** commit in the `git log` order from step 4a — the line directly above the suspect. NOT just any earlier commit.

Why this matters: if there are multiple suspect commits in the range, running a build several commits earlier and seeing it pass only narrows the culprit to "somewhere between that earlier commit and the failing suspect" — it does NOT confirm the specific suspect. To confirm one specific commit, you need its immediate predecessor.

Do NOT mark a bisect as "CONFIRMED" until the truly adjacent commit has been tested. If you only have data from a non-adjacent earlier commit, the status is "narrowed down" — not confirmed.

How to find the adjacent commit:
1. Get the full commit list from `gh api repos/vllm-project/vllm/compare/<last_pass>...<first_fail> --jq '.commits[] | "\(.sha[:9]) \(.commit.message | split("\n")[0])"'`
2. Find the line containing your suspect SHA
3. The commit on the line **directly above** is the adjacent one — use that.

### 5e. Narrow down

- If the job **passes** on a suspect commit → the regression is in a later commit
- If the job **fails** on a suspect commit → the regression is in this commit or an earlier one; confirm by running the commit right before it
- Use binary search across the suspect commits to narrow down efficiently

## Step 6: Post findings to Slack and keep updated

### 6a. Create a canvas with findings and post to #sig-ci

Create a Slack canvas with the bisect findings. This allows you to update it in place as results come in.

```
slack_create_canvas(title: "Nightly CI Bisect — <date>", content: "<findings>")
```

Then post the canvas link to `#sig-ci` (channel ID: `C07R5PAL2L9`):

```
slack_send_message(channel_id: "C07R5PAL2L9", message: "<short summary with canvas link>")
```

The canvas should include for each non-optional failing job:
- Job name
- Error summary (1-2 lines)
- Top suspect commit with PR number and reason
- Buildkite link to the unblocked bisect job
- Status: ⏳ waiting / ✅ passed / ❌ failed

Keep it simple — one section per failing job.

### 6b. Monitor unblocked jobs and update the canvas

Frequently check the status of jobs you unblocked:

```
get_build(org_slug: "vllm", pipeline_slug: "ci", build_number: "<number>", detail_level: "detailed")
```

When there are updates (job finished, bisect narrowed down, root cause identified), **update the canvas in place**:

```
slack_update_canvas(canvas_id: "<id>", action: "replace", section_id: "<section>", content: "<updated content>")
```

Keep updating until all failing jobs have been investigated and root causes identified or narrowed down.
