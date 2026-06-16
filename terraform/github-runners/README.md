# Autoscaling GitHub Actions runners on EC2

Event-driven, **ephemeral** self-hosted GitHub Actions runners on AWS, built on
[`github-aws-runners/github-runner/aws`](https://registry.terraform.io/modules/github-aws-runners/github-runner/aws/latest)
(v7.7.1; requires the AWS provider >= 6.33). A GitHub App webhook drives
autoscaling:

```
workflow_job (queued)  ->  API Gateway  ->  scale-up Lambda
   ->  one EC2 instance per job (JIT, ephemeral)  ->  runs a single job  ->  terminated
```

This is the recommended pattern over a CPU-metric Auto Scaling Group: scaling
tracks the job queue, and one job per throwaway instance is required for safely
running public-repo PR code.

> **Status: not yet applied.** This is a reviewed scaffold. It has not been
> `terraform apply`-ed (it needs the GitHub App + SSM secrets below). Run
> `terraform plan` and review before applying.

## What it creates

API Gateway + Lambdas (webhook, scale-up/down, binary syncer), SQS, IAM roles,
launch template, SSM params, and KMS — all under the `vllm-gha` prefix, in its
own Terraform state (`github-runners/terraform.tfstate`), in the account's
**default VPC** in `us-west-2`.

## Prerequisites

### 1. Create a GitHub App (org: `vllm-project`)

Org **Settings → Developer settings → GitHub Apps → New GitHub App**:

- **Webhook**: there's **nothing to fill in yet** — the URL only exists after
  `terraform apply`. Per the upstream guide, **disable the webhook** during
  creation (uncheck *Active*). You generate your own webhook **secret** (any
  random string), store it in SSM below, and the `webhook_github_app` submodule
  pushes the real URL **and** that secret onto the App at apply. (If you'd
  rather the submodule "just work" without re-enabling later, leave *Active*
  checked with a placeholder URL like `https://example.com` so the App already
  has a hook config to update.)
- **Permissions**
  - Repository → **Actions**: Read-only
  - Organization → **Self-hosted runners**: Read and write  *(needed for org runners + runner groups)*
- **Subscribe to events**: **Workflow job**
- After creating: **generate a private key** (downloads a `.pem`), note the
  **App ID**, and **Install** the App on the org (all repos, or just `vllm`).

### 2. Store the credentials in SSM (region `us-west-2`)

```bash
aws ssm put-parameter --region us-west-2 --name /github-runners/app-id \
  --type String --value "<APP_ID>"

aws ssm put-parameter --region us-west-2 --name /github-runners/key-base64 \
  --type SecureString --value "$(base64 -i path/to/app.private-key.pem)"

aws ssm put-parameter --region us-west-2 --name /github-runners/webhook-secret \
  --type SecureString --value "$(openssl rand -hex 32)"   # generate the webhook secret; the submodule pushes it to the App
```

> `key-base64` must be the **base64 of the `.pem` file**, not its contents.

### 3. Ensure the `vllm-runners` runner group exists

Org **Settings → Actions → Runner groups**. It must already exist and allow the
`vllm` repo (this matches `runs-on: { group: vllm-runners }` in workflows). The
group is managed like the others under `github/runner-groups/`.

## Deploy

```bash
cd terraform/github-runners
terraform init -backend-config=backend.hcl

# First run only: download the Lambda zips locally. The download-lambda module
# writes them at apply time, but the Lambda resources hash the files during the
# plan phase, so they must exist first. (Equivalent to the upstream guide's
# "Download lambdas" step; subsequent applies don't need this.)
terraform apply -target=module.lambdas

terraform plan      # review (~110 resources)
terraform apply
```

After apply, the `webhook_github_app` submodule has pushed the API Gateway URL
+ secret onto the App. Confirm the App's **webhook is Active** and pointed at
`terraform output -raw webhook_endpoint` (check the App's *Advanced* tab for a
delivered ping). If you skipped the submodule, enable the webhook and paste the
URL + the SSM `webhook-secret` value by hand. Then trigger a workflow targeting
the group and watch instances appear/terminate.

## Runner image (read this — it's the Python-3.12 fix)

The module's **default AMI is Amazon Linux 2023**, which reproduces the
`actions/setup-python` failure we saw on the bare `vllm-runners` box
(`version '3.12' ... not found for this operating system`) — `setup-python`
only has prebuilt CPython for **Ubuntu**.

To make `pre-commit` (and other tooling) work, point `ami` in `main.tf` at a
**custom AMI** that ships Python 3.12 + `git` + `shellcheck` (+ `wget`/`xz`),
ideally pre-seeded into `/opt/hostedtoolcache` so `setup-python` is instant.
Build it with Packer (see `packer/`) or mirror
[`actions/runner-images`](https://github.com/actions/runner-images). A boot-time
`userdata_pre_install` is sketched in `main.tf` as a stop-gap, but a baked AMI is
the reliable path. (Or evaluate [RunsOn](https://runs-on.com), which ships
GitHub-hosted-equivalent AMIs.)

## Targeting from a workflow

```yaml
jobs:
  pre-commit:
    runs-on:
      group: vllm-runners      # or: runs-on: [self-hosted, vllm-runners]
```

## Cost / scaling

- `runners_maximum_count` caps concurrent instances.
- `instance_target_capacity_type = "on-demand"` for gating-check reliability;
  switch to `"spot"` for cost (set `create_service_linked_role_spot = true`).
- Ephemeral + `scale_down_schedule_expression` reaps idle/stuck instances.

## Teardown

```bash
terraform destroy
```
Then uninstall/delete the GitHub App and remove the SSM parameters.
