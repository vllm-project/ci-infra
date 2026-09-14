# vLLM Release Branch Cut

End-to-end workflow for cutting a new vLLM release branch, tagging release
candidates, and launching all validation builds. Use when the user asks to
"kick off vX.Y.Z release" or "cut a release branch".

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

Filter to **completed** builds only (state is `passed` or `failed`, not
`running` or `failing`). For each of the 3 most recent completed builds,
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

---

## Step 2: Create and push the release branch

```bash
git fetch origin main
git checkout -b releases/vX.Y.Z <commit_sha>
git push origin releases/vX.Y.Z
```

---

## Step 3: Create the GitHub milestone

```bash
gh api repos/vllm-project/vllm/milestones \
  -f title="vX.Y.Z cherry picks" -f state=open
```

Note the milestone number and URL for the Slack announcement.

---

## Step 4: Trigger the release-v2 build

```bash
bk build create --yes \
  --pipeline vllm/release-v2 \
  --branch releases/vX.Y.Z \
  --commit <commit_sha> \
  --message "vX.Y.ZrcN release"
```

Wait ~1–2 minutes for the pipeline to bootstrap and load all steps. Then find
the job ID for the **"Unblock to build release Docker images"** block step:

```bash
bk build view --pipeline vllm/release-v2 <build_number> --json | python3 -c "
import json, sys
b = json.load(sys.stdin)
for j in b.get('jobs', []):
    if j.get('step_key') == 'block-build-release-images':
        print(j['id'])"
```

Unblock it via the Buildkite REST API. Use your Buildkite API token
(typically stored in `~/.config/bk.yaml` by the `bk` CLI):

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

Monitor both the x86_64 CUDA 13.0 and ROCm image builds until they pass
(~30–60 min each):

```bash
while true; do
  states=$(bk build view --pipeline vllm/release-v2 <build_number> --json \
    | python3 -c "
import json, sys
b = json.load(sys.stdin)
for j in b.get('jobs', []):
    sk = j.get('step_key', '')
    if sk in ('build-release-image-x86', 'build-rocm-release-image'):
        print(f'{sk}={j.get(\"state\", \"unknown\")}')")
  echo "$(date '+%H:%M:%S') $states"
  cuda=$(echo "$states" | grep 'build-release-image-x86=' | cut -d= -f2)
  rocm=$(echo "$states" | grep 'build-rocm-release-image=' | cut -d= -f2)
  if [[ "$cuda" == "passed" || "$cuda" == "failed" ]] && \
     [[ "$rocm" == "passed" || "$rocm" == "failed" ]]; then
    break
  fi
  sleep 60
done
```

The resulting image URIs follow these patterns:

```
# x86_64 CUDA 13.0
public.ecr.aws/q9t5s3a7/vllm-release-repo:<commit_sha>-x86_64

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

- **exit_status=-1**: Infra issue (agent died, never ran). Safe to retry.
- **exit_status=1 with `NotImplementedError`**: Real code issue — check if
  the workload config is compatible with the release branch.
- **Health check timeout**: Server never came up. Could be OOM, model loading
  issue, or infra. Check logs and retry.

Retry failed jobs:

```bash
bk job retry <job_id> --yes
```

---

## Step 8: Cherry-pick PRs from the milestone

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

After cherry-picking, push and tag:

```bash
git push origin releases/vX.Y.Z
git tag vX.Y.ZrcN
git push origin vX.Y.ZrcN
```

Then repeat steps 4–7 with the new HEAD commit.

---

## Step 9: Announce in Slack

Post to the appropriate channel with:

- **Branch name** and commit SHA it was cut from
- **Link to the CI build** used as the basis (initial cut only)
- **List of failing job names** from that CI run (initial cut only)
- **Link to the milestone** for cherry-pick tracking
- **Links to all builds**: full CI, release-v2, perf-eval
- **List of cherry-picked PRs** since the previous RC

Template for initial branch cut:

```
**vX.Y.Z branch cut** :scissors:

The `releases/vX.Y.Z` branch has been cut from commit `<sha>` (based on
[full CI run #NNN](https://buildkite.com/vllm/ci/builds/NNN), the greenest
of the last 3 runs).

**Known failing jobs (N):**
• Job 1
• Job 2
...

**Milestone:** [vX.Y.Z cherry picks](https://github.com/vllm-project/vllm/milestone/NN)
— please tag PRs for cherry-picking here.

**Perf-eval:** [Build #NNN](https://buildkite.com/vllm/perf-eval/builds/NNN)
running all workloads against the release image.
```

Template for subsequent RCs:

```
**vX.Y.ZrcN** :rocket:

Release candidate `vX.Y.ZrcN` tagged on `releases/vX.Y.Z` at commit `<sha>`.

**New cherry-picks since rcN-1 (N):**
• [#NNNNN](https://github.com/vllm-project/vllm/pull/NNNNN) Title
...

**Builds:**
• Full CI: [#NNNNN](https://buildkite.com/vllm/ci/builds/NNNNN) (run_all + nightly)
• Release: [#NNNNN](https://buildkite.com/vllm/release-v2/builds/NNNNN)
• Perf-eval: [#NNNNN](https://buildkite.com/vllm/perf-eval/builds/NNNNN) (CUDA + ROCm)

**Milestone:** [vX.Y.Z cherry picks](https://github.com/vllm-project/vllm/milestone/NN)
```

---

## Gotchas

- **Branch filters on CI pipeline:** The `ci` pipeline only builds `main` and
  PR branches by default. Always pass `--ignore-branch-filters` (`-i`) when
  creating CI builds on `releases/*` branches.
- **Release version input step:** Do NOT unblock the "Provide Release version
  here" input step for release candidates. It's only for the final release
  and sets metadata used by PyPI/DockerHub publishing steps.
- **Buildkite API token:** Some operations (e.g. unblocking jobs with input
  fields) require the REST API rather than the `bk` CLI. Set
  `BUILDKITE_API_TOKEN` as an env var or retrieve it from your `bk` CLI
  config.
- **Unblocking input steps via API:** The Buildkite unblock endpoint for
  input steps expects `{"fields": {"key": "value"}}` in the POST body, not
  flat key-value pairs.
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
