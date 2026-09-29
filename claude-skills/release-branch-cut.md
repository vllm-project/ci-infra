# vLLM Release Branch Cut

End-to-end workflow for cutting a new vLLM release branch, tagging release
candidates, and launching all validation builds. Use when the user asks to
"kick off vX.Y.Z release" or "cut a release branch".

**RC numbering:** the branch-cut commit is tagged `vX.Y.Zrc0`. Each later
cherry-pick round is `rc1`, `rc2`, …. Below, `rcN` means the RC you are
building right now, so it is `rc0` for the first cut.

---

## Prerequisites

- **Buildkite CLI (`bk`)** — authenticated to the `vllm` org.
- **Buildkite API token** — set as `BUILDKITE_API_TOKEN` env var for REST API
  calls (e.g. unblocking jobs with input fields).
- **GitHub CLI (`gh`)** — authenticated with repo access to `vllm-project/vllm`.
- Local clone of `vllm-project/vllm` with `origin` remote.

---

## Step 1: Find the greenest full CI run

List recent "Full CI run" builds on `main`:

```bash
bk build list --pipeline vllm/ci --branch main --message "Full CI run" \
  --limit 10 --summary --json
```

Skip "Full CI run torch nightly" builds. The message filter matches them
too, but they test against torch nightly, not the torch the release ships
with. Filter to **completed** builds only (state is `passed` or `failed`,
not `running` or `failing`). For each of the 3 most recent completed builds,
count failed jobs:

```bash
bk build view --pipeline vllm/ci <build_number> --json | python3 -c "
import json, sys
b = json.load(sys.stdin)
jobs = b.get('jobs', [])
failed = sum(1 for j in jobs if j.get('state') == 'failed')
passed = sum(1 for j in jobs if j.get('state') == 'passed')
print(f'Build #{b[\"number\"]}: {failed} failed / {passed} passed, commit={b[\"commit\"]}')"
```

Pick the build with the **fewest failed jobs**. If tied, pick the more recent
one. Note the full commit SHA — this is the branch cut point.

A newer build that is still running can be picked if the jobs it has
finished are already greener than the completed builds. If you pick one,
list its still-pending jobs in the announcement.

---

## Step 2: Create and push the release branch, tag rc0

Push the branch, then tag the cut commit `vX.Y.Zrc0` and push the tag right
away:

```bash
git fetch origin main
git checkout -b releases/vX.Y.Z <commit_sha>
git push origin releases/vX.Y.Z
git tag vX.Y.Zrc0 <commit_sha>
git push origin vX.Y.Zrc0
```

Pushing the branch starts the release-v2 build (see step 4). Pushing the
tag does not start anything.

---

## Step 3: Create the GitHub milestone

```bash
gh api repos/vllm-project/vllm/milestones \
  -f title="vX.Y.Z cherry picks" -f state=open
```

Note the milestone number and URL for the Slack announcement.

---

## Step 4: Find the release-v2 build and unblock it

A push of a `releases/*` branch, or of new commits to it, starts a
release-v2 build of the branch HEAD on its own. The build's message is the
commit title. Do NOT `bk build create` another one: the duplicate just
queues behind the first on `small_cpu_queue_release`. Find the build:

```bash
bk build list --pipeline vllm/release-v2 --branch releases/vX.Y.Z \
  --limit 5 --json | python3 -c "
import json, sys
for b in json.load(sys.stdin):
    print(b['number'], b['state'], b['commit'][:12], b.get('message', '').splitlines()[0])"
```

If no build shows up for the HEAD commit (e.g. the webhook missed the
push), create one by hand as a last resort:

```bash
bk build create --yes \
  --pipeline vllm/release-v2 \
  --branch releases/vX.Y.Z \
  --commit <commit_sha> \
  --message "vX.Y.ZrcN release"
```

The block step appears only after the Bootstrap job uploads the pipeline.
Bootstrap can wait on the queue for a while. Once it has run, find the job
ID for the **"Unblock to build release Docker images"** block step:

```bash
bk build view --pipeline vllm/release-v2 <build_number> --json | python3 -c "
import json, sys
b = json.load(sys.stdin)
for j in b.get('jobs', []):
    if j.get('step_key') == 'block-build-release-images':
        print(j['id'])"
```

Unblock it with the CLI. It takes only the job ID, with no `--pipeline` flag:

```bash
bk job unblock <job_id>
```

If the CLI fails, use the REST API with your Buildkite API token (the `bk`
CLI usually stores it in `~/.config/bk.yaml`). For an input step, the PUT
body is `{"fields": {"key": "value"}}`:

```bash
curl -s -X PUT \
  "https://api.buildkite.com/v2/organizations/vllm/pipelines/release-v2/builds/<build_number>/jobs/<job_id>/unblock" \
  -H "Authorization: Bearer $BUILDKITE_API_TOKEN" \
  -H "Content-Type: application/json" -d '{}'
```

> **Important:** Do NOT unblock the "Provide Release version here" input step
> for release candidates. That step sets the final version string used for
> PyPI uploads and DockerHub tags — it's only for the final release.

---

## Step 5: Wait for the release images

Monitor the x86_64 CUDA 13.0 and ROCm image builds until they pass
(~30–60 min each). Also watch the CUDA 12.9 x86_64 images
(`build-release-image-x86-cuda-12-9` and its `-ubuntu2404` variant). These
jobs stay blocked on main's release-v2 builds, so a build break that only
affects 12.9 first shows up at the cut. In v0.31.0 the snapshot runtime
helper needed CUDA 13 headers. vllm-project/vllm#59118 fixed it and was
cherry-picked.

macOS has no `timeout` command, so the loop wraps each API call in a Python
subprocess timeout. That way a hung call can't stall the monitor:

```bash
python3 - <<'EOF'
import json, subprocess, time
BUILD = "<build_number>"
KEYS = ["build-release-image-x86", "build-release-image-x86-cuda-12-9",
        "build-release-image-x86-cuda-12-9-ubuntu2404", "build-rocm-release-image"]
DONE = {"passed", "failed", "canceled", "timed_out", "broken", "skipped"}
while True:
    try:
        out = subprocess.run(
            ["bk", "build", "view", "--pipeline", "vllm/release-v2", BUILD, "--json"],
            capture_output=True, text=True, timeout=60).stdout
        jobs = {j.get("step_key"): j.get("state")
                for j in json.loads(out).get("jobs", []) if not j.get("retried")}
    except (subprocess.TimeoutExpired, ValueError) as e:
        print(time.strftime("%H:%M:%S"), "bk call failed:", type(e).__name__, flush=True)
        time.sleep(60)
        continue
    states = {k: jobs.get(k, "missing") for k in KEYS}
    print(time.strftime("%H:%M:%S"), " ".join(f"{k}={v}" for k, v in states.items()), flush=True)
    if all(v in DONE for v in states.values()):
        break
    time.sleep(60)
EOF
```

The resulting image URIs follow these patterns:

```
# x86_64 CUDA 13.0
public.ecr.aws/q9t5s3a7/vllm-release-repo:<commit_sha>-x86_64

# x86_64 CUDA 12.9 (Ubuntu 24.04 variant: ...-x86_64-cu129-ubuntu2404)
public.ecr.aws/q9t5s3a7/vllm-release-repo:<commit_sha>-x86_64-cu129

# ROCm
public.ecr.aws/q9t5s3a7/vllm-release-repo:<commit_sha>-rocm
```

---

## Step 6: Launch full CI

Trigger a full CI run on the release branch with `RUN_ALL=1` and `NIGHTLY=1`.
The `--ignore-branch-filters` flag is required because the CI pipeline's
branch filter rules don't include `releases/*` by default:

```bash
bk build create --yes --ignore-branch-filters \
  --pipeline vllm/ci \
  --branch releases/vX.Y.Z \
  --commit <commit_sha> \
  --message "Full CI run - vX.Y.ZrcN" \
  --env "RUN_ALL=1" \
  --env "NIGHTLY=1"
```

---

## Step 7: Launch perf-eval

Once both images pass, launch perf-eval with **all workloads** (omit
`WORKLOADS` to run the full `nightly: true` set). Use the per-platform
image env vars so both CUDA and ROCm workloads get the right image:

```bash
# Get the latest perf-eval main HEAD
PERF_EVAL_SHA=$(gh api repos/vllm-project/perf-eval/branches/main --jq '.commit.sha')

bk build create --yes \
  --pipeline vllm/perf-eval \
  --commit "$PERF_EVAL_SHA" \
  --branch main \
  --message "vX.Y.ZrcN candidate" \
  --env "VLLM_COMMIT=<commit_sha>" \
  --env "VLLM_IMAGE_CUDA=public.ecr.aws/q9t5s3a7/vllm-release-repo:<commit_sha>-x86_64" \
  --env "VLLM_IMAGE_ROCM=public.ecr.aws/q9t5s3a7/vllm-release-repo:<commit_sha>-rocm"
```

> **Note:** The `--commit` and `--branch` here refer to the **perf-eval
> repo**, not the vLLM repo. The vLLM commit and images are passed via
> environment variables. Use `VLLM_IMAGE_CUDA` and `VLLM_IMAGE_ROCM` (not
> `VLLM_IMAGE`) so each platform gets its own image. If only `VLLM_IMAGE`
> is set, ROCm workloads may get the wrong image or be skipped.

### Prioritize key workloads

After launching, reprioritize critical jobs to the top of their queue. For
example, to prioritize a specific model that a cherry-picked PR is meant to
fix:

```bash
# List all jobs to find IDs
bk build view --pipeline vllm/perf-eval <build_number> --json | python3 -c "
import json, sys
b = json.load(sys.stdin)
for j in b.get('jobs', []):
    print(f'{j[\"id\"]} {j.get(\"state\",\"?\"):12s} {j.get(\"name\",\"?\")}')"

# Reprioritize a specific job to the top (higher number = sooner)
bk job reprioritize <job_id> 100 --yes

# Reprioritize all H200 jobs above default
bk job reprioritize <job_id> 50 --yes
```

### Monitor and retry failures

Watch the build until all jobs complete. Common failure patterns:

- **exit_status=-1** (agent lost, never ran): infra. Safe to retry.
- **pip/network `ReadTimeoutError`** during setup: infra. Safe to retry.
- **exit_status=1 with `NotImplementedError`**: Real code issue — check if
  the workload config is compatible with the release branch.
- **Health check timeout**: Server never came up. Could be OOM, model loading
  issue, or infra. Check logs and retry.
- **`timed_out` after hitting `timeout_in_minutes`**: not infra. Before you
  retry, look at the same job in earlier nightly/RC builds. A job that
  always runs past its limit will just time out again. It needs a higher
  timeout in perf-eval. For example, `glm_5_3-h200` hit its 120 min limit
  on every run through v0.31.0rc1.

Fetch a job's log (the flag is `--build-number`, not `--build`). Logs are
large, so save to a file, strip ANSI codes, and grep:

```bash
bk job log <job_id> --pipeline vllm/perf-eval --build-number <build_number> \
  | sed 's/\x1b\[[0-9;]*m//g' > job.log
```

Retry failed jobs:

```bash
bk job retry <job_id> --yes
```

---

## Step 8: Compare perf with the previous release

Once perf-eval finishes, compare the release candidate against the last
release's final RC using the CI dashboard compare API.

Find the previous release's perf-eval build (look for the latest
`v<prev>.Y.ZrcN candidate` message):

```bash
bk build list --pipeline vllm/perf-eval --branch main --limit 30 --summary --json \
  | python3 -c "
import json, sys
for b in json.load(sys.stdin):
    print(b['number'], b['state'], b.get('message', ''))"
```

Sometimes the final release tag had no perf-eval run of its own. If it
differs from its last RC only by build-only commits, use that RC's images
as the baseline. For example, v0.30.0 final (`ced6857a`) had no run, so
rc2 (`fa6ff060`) was used.

Pull the release image URIs from that build's env (`VLLM_IMAGE_CUDA` /
`VLLM_IMAGE_ROCM`), then query the compare API once per platform — the
`baseline` is the previous release image, `candidate` is the new one. Use
curl. Python `urllib` fails SSL verification on macOS:

```bash
curl -s "https://ci.vllm.ai/api/compare?baseline=<prev_image>&candidate=<new_image>" > compare.json
```

The JSON contains a `summary` (regression/improvement/noisy/unchanged
counts), `worstRegressions` (sorted by severity), and `eval.deltas`
(accuracy scores). Thresholds: perf 2%, eval 2σ (`thresholds` field).

Things to watch for when reading results:

- **Accuracy first:** any `eval.deltas` entry with status `regression` is
  a release blocker. `noisy` means within 2σ — not actionable.
- **Single-run noise:** perf numbers come from one run each. Large
  regressions (>10%) on a workload whose job was retried (e.g. after an
  infra flake) may be agent-specific — sanity-check against a rerun before
  filing.
- **Check the baseline before bisecting code:** a flagged regression can be
  a *lucky baseline* rather than a slow candidate. Before digging through
  commits, compare recent nightly builds against the same baseline image.
  Find them with `bk build list --pipeline vllm/perf-eval --branch main`.
  Their message is "Nightly run YYYY-MM-DD: commit …", and they run CUDA
  only. The per-day candidate values form a day-by-day series: a free
  bisection that also shows the metric's steady-state band. If the baseline
  sits outside that band, the regression is an artifact. Definitive check:
  rerun the workload with `VLLM_IMAGE_CUDA` pointing at the *baseline* image
  and see if it reproduces the baseline number.
- **Agent identity matters:** group results by the agent each job ran on
  (job detail API). For example, `h200-ci-1` and `mithril-h200-*` differ a
  lot. Only same-host comparisons are trustworthy. Some perf hosts are
  intermittently slow, so a bad run on an unverified host is noise until
  it reproduces on a known-good one.
- **Explain trade-offs from server logs:** grep each run's job log for
  `GPU KV cache size` and `Using … attention backend`. A changed KV cache
  size or attention backend usually explains a throughput-vs-latency shift.
- **Workload coverage:** check `summary.missingBaseline` /
  `missingCandidate` — renamed or newly added workloads won't have a
  comparison. ROCm comparisons often match few workloads across releases;
  that's expected, not an error.
- Share the browser version of the link in the Slack announcement:
  `https://ci.vllm.ai/compare?baseline=<prev_image>&candidate=<new_image>`

To confirm a regression with a targeted rerun, launch a new perf-eval
build with `WORKLOADS` (comma-separated workload stems from the
perf-eval repo's `workloads/` dir, e.g. `deepseek_v4_pro_5_h200`) and
`BENCH_ONLY=1`, which runs only the vllm bench configs and skips the
lm_eval/BFCL accuracy tasks — much faster when accuracy data is already
in hand:

```bash
bk build create --yes --pipeline vllm/perf-eval --branch main \
  --commit <perf_eval_sha> --message "vX.Y.ZrcN regression rerun" \
  --env "BENCH_ONLY=1" \
  --env "WORKLOADS=<stem1>,<stem2>" \
  --env "VLLM_COMMIT=<commit_sha>" \
  --env "VLLM_IMAGE_CUDA=public.ecr.aws/q9t5s3a7/vllm-release-repo:<commit_sha>-x86_64"
```

A passed job cannot be retried in place, so a fresh targeted build is the
way to get a clean-agent data point. Rerun results upload under the same
image URI and supersede the earlier runs in the compare API.

Include significant regressions in the Slack announcement and flag them to
the release manager as cherry-pick candidates.

---

## Step 9: Cherry-pick PRs from the milestone

When new PRs are added to the milestone for the next RC:

```bash
# List all PRs in the milestone
gh pr list --repo vllm-project/vllm \
  --search "milestone:\"vX.Y.Z cherry picks\"" \
  --state all --json number,title,state --limit 50
```

**Only cherry-pick PRs that are already merged to main** (state `MERGED`).
Skip open or closed-unmerged PRs — they haven't landed yet and may still
change. If a PR in the milestone is still open, flag it to the release
manager and move on. If the release manager explicitly asks to cherry-pick
an open PR, fetch the head commit SHA from the PR branch instead of the
merge commit:

```bash
# For merged PRs — get merge commit SHA
gh pr view <PR_NUMBER> --repo vllm-project/vllm \
  --json mergeCommit --jq '.mergeCommit.oid'

# For open PRs (only when explicitly instructed) — get head commit SHA
gh pr view <PR_NUMBER> --repo vllm-project/vllm \
  --json commits --jq '.commits[-1].oid'
```

Before cherry-picking, check if a PR is already on the branch (e.g. from an
earlier RC):

```bash
git log --oneline releases/vX.Y.Z | grep <PR_NUMBER>
```

Cherry-pick in ascending PR-number order (oldest first) to minimize
conflicts:

```bash
git checkout releases/vX.Y.Z
git pull origin releases/vX.Y.Z   # pick up any commits pushed by others
git fetch origin <commit_sha>
git cherry-pick <commit_sha>
```

If pre-commit hooks fail due to local environment issues (e.g. SSL errors
downloading shellcheck), bypass them:

```bash
git -c core.hooksPath=/dev/null cherry-pick <commit_sha>
# Or to continue after resolving a conflict:
git -c core.hooksPath=/dev/null cherry-pick --continue --no-edit
```

After cherry-picking, push and tag the next RC. The first cherry-pick round
after the rc0 cut is `rc1`:

```bash
git push origin releases/vX.Y.Z
git tag vX.Y.ZrcN
git push origin vX.Y.ZrcN
```

The branch push starts the release-v2 build of the new HEAD on its own.
Then repeat steps 4–8 with the new HEAD commit, starting from finding that
build instead of creating one.

---

## Step 10: Announce in Slack

Post to the appropriate channel with:

- **Branch name** and commit SHA it was cut from
- **Link to the CI build** used as the basis (initial cut only)
- **List of failing job names** from that CI run, plus any jobs still
  pending if it was still running (initial cut only)
- **Link to the milestone** for cherry-pick tracking
- **Links to all builds**: full CI, release-v2, perf-eval
- **List of cherry-picked PRs** since the previous RC

> **Formatting:** the human usually pastes the message into Slack's
> composer. The composer does not render mrkdwn: `<url|text>` and
> `*bold*` show up as literal characters, and so do Markdown's `**bold**`
> and `[text](url)`. So give them **rendered rich text** that keeps bold
> and links when copied. For example, write an HTML preview (`<b>`,
> `<a href>`, `<ul>`) they open, select all, and copy, or send a rendered
> Markdown message. Use the mrkdwn templates below only when posting
> through the Slack API or a bot. In mrkdwn, `*single asterisks*` make
> bold, backticks make inline code (branches, tags, SHAs), and
> `<https://full-url|link text>` makes a link.

Template for the initial branch cut (rc0):

```
*vX.Y.Z branch cut* :scissors:

The `releases/vX.Y.Z` branch has been cut from commit `<sha>` and tagged
`vX.Y.Zrc0` (based on <https://buildkite.com/vllm/ci/builds/NNNNN|full CI run #NNNNN>,
the greenest of the last 3 runs).

*Known failing jobs (N):*
• Job 1
• Job 2
...
(Still running at cut time: Job X, Job Y)

*Milestone:* <https://github.com/vllm-project/vllm/milestone/NN|vX.Y.Z cherry picks> — please tag PRs for cherry-picking here.

*Builds:* <https://buildkite.com/vllm/ci/builds/NNNNN|Full CI #NNNNN> (run_all + nightly) · <https://buildkite.com/vllm/release-v2/builds/NNNNN|Release #NNNNN> · <https://buildkite.com/vllm/perf-eval/builds/NNNN|Perf-eval #NNNN> (all workloads, CUDA + ROCm)
```

Template for subsequent RCs:

```
*vX.Y.ZrcN* :rocket:

Release candidate `vX.Y.ZrcN` tagged on `releases/vX.Y.Z` at commit `<full_sha>`.

*New cherry-picks since rcN-1 (N):*
• <https://github.com/vllm-project/vllm/pull/NNNNN|#NNNNN> Title
...

*Builds:* <https://buildkite.com/vllm/ci/builds/NNNNN|Full CI #NNNNN> (run_all + nightly) · <https://buildkite.com/vllm/release-v2/builds/NNNNN|Release #NNNNN> · <https://buildkite.com/vllm/perf-eval/builds/NNNN|Perf-eval #NNNN> (all workloads, CUDA + ROCm)

*Milestone:* <https://github.com/vllm-project/vllm/milestone/NN|vX.Y.Z cherry picks>
```

---

## Step 11: Smoke test the release artifacts (final release only)

For the **final** release (not RCs), once the release-v2 build has produced
wheels and images, smoke test what users will actually install before the
announcement goes out. Keep it shallow: install/boot + one request, not
benchmarks.

**What to test:**

- **Wheel:** once `vllm==X.Y.Z` appears on PyPI (`upload-release-wheels`
  job; poll `https://pypi.org/pypi/vllm/json`): on a clean linux x86_64
  machine, `python3.12 -m venv /tmp/vllm-smoke && pip install vllm==X.Y.Z`,
  then `python -c "import vllm; print(vllm.__version__)"` and
  `from vllm import LLM, SamplingParams` import check. A short offline
  generation with a tiny cached model is a good bonus but optional.
- **Image:** the release-repo ECR images are the exact content that the
  publish steps later push to DockerHub:
  `public.ecr.aws/q9t5s3a7/vllm-release-repo:<full_sha>-x86_64` (and
  `-aarch64` for Grace/ARM). `docker run` the image, `vllm serve` a model,
  wait for `/health`, hit `/v1/models`, send one chat completion
  ("What is 2+2?", max_tokens 64), then tear down.

**Model × hardware matrix:** cover the currently popular models on each GPU
generation (e.g. GLM-5.3-Flash, MiniMax-M3, Qwen3.8-Flash-Next,
DeepSeek-V4.x-Flash across H200 / B200 / GB200). Use the exact serve command
from the model's recipe — `https://recipes.vllm.ai/models.json` is
machine-readable; each recipe has per-hardware `command`/`env` blocks.
Prefer each model's recipe-recommended hardware.

**Machines available for smoke testing:**

- H200: `ssh h200-ci-1` (8×H200; HF cache at `/mnt/vllm-ci`, set
  `HF_HOME=/mnt/vllm-ci` and mount it into the container)
- GB200: `gcloud compute ssh gb200-rack1-07 --zone us-central1-b
  --ssh-key-file ~/.ssh/id_ed25519` (also `gb200-rack1-08`; 4×GB200 each;
  model cache on Lustre at `/mnt/lustre/hf-models`)
- B200: `ssh dgxb200-15` / `ssh dgxb200-16` (8×B200)

**Practical notes:**

- These are shared CI machines — wait for a CI-free window before starting
  (no containers in `docker ps`, no processes in
  `nvidia-smi --query-compute-apps=pid`). Never run a TP8 server alongside
  a CI job; you'd poison both.
- The release image's entrypoint is `["vllm", "serve"]` — so
  `docker run <img> <model> <serve args...>` works directly (do NOT add
  `serve` yourself; `docker run <img> serve <model>` becomes
  `vllm serve serve <model>` and fails with
  `unrecognized arguments: <model>`). For `bench` or anything else, override
  the entrypoint: `--entrypoint vllm` (bench) or `--entrypoint python3`.
- Always pass `--ipc=host` — DP/TP servers need >64 MiB of /dev/shm
  (docker's default) and die with
  `Insufficient space in /dev/shm: ... required, 64 MiB free`.
- Pulling release-repo images from a fresh host: anonymous ECR Public pulls
  hit "Data limit exceeded" quickly at 30 GB/image. Log in first:
  `aws ecr-public get-login-password --region us-east-1 | ssh <host> 'sudo docker login --username AWS --password-stdin public.ecr.aws'`.
- With `VLLM_USE_RUST_FRONTEND=1`, the frontend gives up after 600s if the
  engine is still downloading/loading a model — set
  `VLLM_ENGINE_READY_TIMEOUT_S=3600` for first-time (uncached) models.
- Use `--network host`, and mount the HF cache dir with
  `-e HF_HOME=<path> -v <path>:<path>`.
- Big models take 5–60 min to load even from cache; wait on `/health` up to
  90 min (GB200 + Lustre can be slow) and bail early if the container exits.
- Docker needs `sudo` on the mithril/GB200 hosts.
- If a release-pipeline step fails on infra (e.g. the triton-cpu sleef
  submodule flake in `build-cpu-release-image-x86`), retry it once; if it
  repeats, it's the known `--shallow-submodules --filter=blob:none` issue —
  see vllm#57871.

---

## Gotchas

- **Branch filters on CI pipeline:** The `ci` pipeline only builds `main` and
  PR branches by default. Always pass `--ignore-branch-filters` (`-i`) when
  creating CI builds on `releases/*` branches.
- **Release version input step:** Do NOT unblock the "Provide Release version
  here" input step for release candidates. It's only for the final release
  and sets metadata used by PyPI/DockerHub publishing steps. When you DO
  unblock it (final release), the value must include the leading `v`
  (e.g. `v0.30.0`, not `0.30.0`): `upload-release-wheels-pypi.sh` compares
  it literally against `git describe --tags` output and hard-fails on
  mismatch. A wrong value cannot be fixed by retry — you must create a new
  release build at the same commit and unblock the input correctly.
- **Buildkite API token:** Some operations (e.g. unblocking jobs with input
  fields) require the REST API rather than the `bk` CLI. Set
  `BUILDKITE_API_TOKEN` as an env var or retrieve it from your `bk` CLI
  config.
- **Unblocking input steps via API:** The endpoint is
  `PUT .../jobs/<id>/unblock`. For input steps it expects
  `{"fields": {"key": "value"}}` in the PUT body, not flat key-value pairs.
  For a plain block step, `bk job unblock <job_id>` is enough.
- **Release builds start on push:** a push of `releases/*` (or of new
  commits to it) starts a release-v2 build of the HEAD, with the commit
  title as its message. A manual `bk build create` only adds a duplicate
  that queues behind it on `small_cpu_queue_release`. Tag pushes don't
  start builds.
- **CUDA 12.9 images build only at the cut:** they stay blocked on main's
  release-v2 builds. So a 12.9-only compile break first surfaces on the
  release branch. In v0.31.0 it was vllm-project/vllm#59118, and it had to
  be cherry-picked.
- **`bk job log` flags:** `bk job log <job_id> --pipeline vllm/<p>
  --build-number <N>`. The flag is `--build-number`, not `--build`.
- **Image naming:** The x86_64 CUDA 13.0 release image is
  `public.ecr.aws/q9t5s3a7/vllm-release-repo:<full_commit_sha>-x86_64`.
  The ROCm image is `...:<full_commit_sha>-rocm`. The commit SHA is the
  HEAD of the release branch at build time.
- **Perf-eval image env vars:** Use `VLLM_IMAGE_CUDA` and `VLLM_IMAGE_ROCM`
  (not `VLLM_IMAGE`) when both CUDA and ROCm workloads should run. Setting
  only `VLLM_IMAGE` causes ROCm workloads to get the CUDA image or be
  skipped entirely.
- **Perf-eval commit vs vLLM commit:** The perf-eval build's `--commit` and
  `--branch` refer to the perf-eval repo (which Buildkite clones). The vLLM
  commit and Docker images go in `--env` vars.
- **Job reprioritization:** Use `bk job reprioritize <job_id> <number>` to
  move jobs up the queue. Higher numbers run sooner. This is useful for
  prioritizing workloads that validate specific cherry-picked fixes.
- **Perf-eval exit_status=-1:** Means the job never actually ran (agent died
  or was killed). Safe to retry. If retries keep failing with -1 on the same
  platform (e.g. MI300X), it's likely a persistent infra issue with that
  agent pool — flag it but don't keep retrying indefinitely.
- **Cherry-pick conflicts:** Conflicts in `.buildkite/test_areas/*.yaml` are
  common when CI config has diverged between the branch cut point and main.
  Take the incoming (PR) version unless there's a clear reason otherwise.
- **Pre-commit hook failures:** Local SSL or network issues can cause
  pre-commit hooks (e.g. shellcheck download) to fail during cherry-pick.
  Use `git -c core.hooksPath=/dev/null` to bypass hooks for release
  cherry-picks — CI will validate the code anyway.
- **Duplicate cherry-picks:** Always check `git log --oneline` on the release
  branch before cherry-picking — a PR from an earlier RC may already be
  there. The cherry-pick will succeed but create a duplicate commit with a
  different SHA.
- **Others may push to the branch:** The release manager or other maintainers
  may merge PRs directly into the release branch. Always `git pull` before
  cherry-picking to avoid non-fast-forward push rejections.
