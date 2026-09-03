# vLLM Release Branch Cut

End-to-end workflow for cutting a new vLLM release branch, tagging release
candidates, and launching all validation builds. Use when the user asks to
"kick off vX.Y.Z release" or "cut a release branch".

---

## Prerequisites

- **Buildkite CLI (`bk`)** — authenticated to the `vllm` org. Config at
  `~/.config/bk.yaml` contains the API token under `organizations.vllm.api_token`.
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
  --message "vX.Y.Z release"
```

Wait ~1 minute for the pipeline to bootstrap and load all steps. Then find
the job ID for the **"Unblock to build release Docker images"** block step:

```bash
bk build view --pipeline vllm/release-v2 <build_number> --json | python3 -c "
import json, sys
b = json.load(sys.stdin)
for j in b.get('jobs', []):
    if j.get('step_key') == 'block-build-release-images':
        print(j['id'])"
```

Unblock it via the Buildkite REST API. The API token is in `~/.config/bk.yaml`
under `organizations.vllm.api_token`:

```bash
BK_TOKEN=$(python3 -c "
import yaml; c = yaml.safe_load(open('$HOME/.config/bk.yaml'))
print(c['organizations']['vllm']['api_token'])")

curl -s -X PUT \
  "https://api.buildkite.com/v2/organizations/vllm/pipelines/release-v2/builds/<build_number>/jobs/<job_id>/unblock" \
  -H "Authorization: Bearer $BK_TOKEN" \
  -H "Content-Type: application/json" -d '{}'
```

> **Important:** Do NOT unblock the "Provide Release version here" input step
> for release candidates. That step sets the final version string used for
> PyPI uploads and DockerHub tags — it's only for the final release.

---

## Step 5: Wait for the x86_64 CUDA 13.0 image

Monitor the `build-release-image-x86` step until it passes (~30–60 min):

```bash
while true; do
  state=$(bk build view --pipeline vllm/release-v2 <build_number> --json \
    | python3 -c "
import json, sys
b = json.load(sys.stdin)
for j in b.get('jobs', []):
    if j.get('step_key') == 'build-release-image-x86':
        print(j.get('state', 'unknown')); break")
  echo "$(date '+%H:%M:%S') build-release-image-x86: $state"
  [[ "$state" == "passed" || "$state" == "failed" ]] && break
  sleep 60
done
```

The resulting image URI follows this pattern:

```
public.ecr.aws/q9t5s3a7/vllm-release-repo:<commit_sha>-x86_64
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

Once the x86_64 CUDA 13.0 image build passes, launch perf-eval with **all
workloads** (omit `WORKLOADS` to run the full `nightly: true` set):

```bash
# Get the latest perf-eval main HEAD
PERF_EVAL_SHA=$(gh api repos/vllm-project/perf-eval/branches/main --jq '.commit.sha')

bk build create --yes \
  --pipeline vllm/perf-eval \
  --commit "$PERF_EVAL_SHA" \
  --branch main \
  --message "vX.Y.ZrcN candidate" \
  --env "VLLM_COMMIT=<commit_sha>" \
  --env "VLLM_IMAGE=public.ecr.aws/q9t5s3a7/vllm-release-repo:<commit_sha>-x86_64"
```

> **Note:** The `--commit` and `--branch` here refer to the **perf-eval
> repo**, not the vLLM repo. The vLLM commit and image are passed via
> environment variables.

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
manager and move on.

```bash
# Get merge commit SHAs (only for MERGED PRs)
gh pr view <PR_NUMBER> --repo vllm-project/vllm \
  --json mergeCommit --jq '.mergeCommit.oid'
```

Cherry-pick in ascending PR-number order (oldest first) to minimize
conflicts:

```bash
git checkout releases/vX.Y.Z
git fetch origin <merge_commit_sha>
git cherry-pick <merge_commit_sha>
```

If pre-commit hooks fail due to local environment issues (e.g. SSL errors
downloading shellcheck), bypass them:

```bash
git -c core.hooksPath=/dev/null cherry-pick <merge_commit_sha>
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
- **Link to the CI build** used as the basis
- **List of failing job names** from that CI run
- **Link to the milestone** for cherry-pick tracking
- **Links to all builds**: full CI, release-v2, perf-eval
- **List of cherry-picked PRs** (for RC2+)

Template:

```
**vX.Y.ZrcN** :rocket:

Release candidate `vX.Y.ZrcN` tagged on `releases/vX.Y.Z` at commit `<sha>`.

**Cherry-picked PRs (N):**
• [#NNNNN](https://github.com/vllm-project/vllm/pull/NNNNN) Title
...

**Builds:**
• Full CI: [#NNNNN](https://buildkite.com/vllm/ci/builds/NNNNN) (run_all + nightly)
• Release: [#NNNNN](https://buildkite.com/vllm/release-v2/builds/NNNNN)
• Perf-eval: [#NNNNN](https://buildkite.com/vllm/perf-eval/builds/NNNNN)

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
- **Buildkite API token:** The `bk` CLI stores its token in
  `~/.config/bk.yaml`. For REST API calls (e.g. unblocking jobs with input
  fields), extract it from there rather than looking for an env var.
- **Unblocking input steps via API:** The Buildkite unblock endpoint for
  input steps expects `{"fields": {"key": "value"}}` in the POST body, not
  flat key-value pairs.
- **Image naming:** The x86_64 CUDA 13.0 release image is always
  `public.ecr.aws/q9t5s3a7/vllm-release-repo:<full_commit_sha>-x86_64`.
  The commit SHA is the HEAD of the release branch at build time.
- **Perf-eval commit vs vLLM commit:** The perf-eval build's `--commit` and
  `--branch` refer to the perf-eval repo (which Buildkite clones). The vLLM
  commit and Docker image go in `--env` vars (`VLLM_COMMIT`, `VLLM_IMAGE`).
- **Cherry-pick conflicts:** Conflicts in `.buildkite/test_areas/*.yaml` are
  common when CI config has diverged between the branch cut point and main.
  Take the incoming (PR) version unless there's a clear reason otherwise.
- **Pre-commit hook failures:** Local SSL or network issues can cause
  pre-commit hooks (e.g. shellcheck download) to fail during cherry-pick.
  Use `git -c core.hooksPath=/dev/null` to bypass hooks for release
  cherry-picks — CI will validate the code anyway.
