# Building the CPU Build AMI

This directory contains Packer configuration for building a custom AMI optimized for Docker/vLLM builds.

## Features

- **Pre-pulled vLLM image**: Postmerge image pulled to cache shared layers
- **Optimized buildx**: Pre-configured with `max-parallelism=16` and `gc=false`
- **Persistent builder**: BuildKit container with `restart=always`
- **Fast storage**: gp3 volumes with 6000 IOPS and 500 MB/s throughput
- **Fully automated**: Pipeline updates SSM; new instances use latest AMI via natural turnover
- **Auto-deprecation**: Old AMIs are deprecated after 7 days

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  AMI Instance                                                   │
│                                                                 │
│  Docker daemon                                                  │
│    ├── buildx_buildkit_baked-vllm-builder (restart=always)      │
│    │     └── BuildKit with max-parallelism=16, gc=false         │
│    │                                                            │
│    └── Cached image layers from:                                │
│          public.ecr.aws/q9t5s3a7/vllm-ci-postmerge-repo:latest  │
│                                                                 │
│  /etc/buildkit/buildkitd.toml      <- BuildKit config           │
└─────────────────────────────────────────────────────────────────┘
```

## Automation Flow

The pipeline is fully automated - no manual steps required after initial setup:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  DAILY (3 AM PST / 11 AM UTC) - Buildkite Scheduled Pipeline                │
│  .buildkite/pipelines/rebuild-cpu-ami.yml                                   │
│                                                                             │
│  1. :packer: Build AMI                                                      │
│     └── Packer builds in us-east-1                                          │
│                                                                             │
│  2. :aws: Update SSM Parameter                                              │
│     └── /buildkite/cpu-build-ami/us-east-1 -> ami-xxx                       │
│                                                                             │
│  3. :broom: Cleanup Old AMIs                                                │
│     └── Keep 3 AMIs older than 3 days, delete the rest                      │
│                                                                             │
│  New instances automatically use the latest AMI via SSM dynamic reference.  │
│  (BuildkiteTerminateInstanceAfterJob=true ensures natural instance turnover)│
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Manual Build (for testing)

```bash
cd packer/cpu

# Get source AMI from Buildkite stack (must match BUILDKITE_STACK_VERSION in pipeline)
SOURCE_AMI=$(curl -s "https://s3.amazonaws.com/buildkite-aws-stack/v6.21.0/aws-stack.yml" | \
  yq '.Mappings.AWSRegion2AMI.us-east-1.linuxamd64')

# Get security group ID from SSM (created by Terraform)
SECURITY_GROUP_ID=$(aws ssm get-parameter \
  --name "/buildkite/packer/security-group-id" \
  --query 'Parameter.Value' --output text --region us-east-1)

packer init .
packer build \
  -var "source_ami=$SOURCE_AMI" \
  -var "security_group_id=$SECURITY_GROUP_ID" \
  buildkite-cpu-ami.pkr.hcl
```

## Canary Build for a New Elastic CI Stack Version

Before upgrading a queue to a newer Elastic CI Stack, build a canary AMI from
that stack's base to validate it, without touching the AMI the queues use.

Trigger the "Rebuild CPU Build AMI" pipeline manually with the environment
variable `CANARY_STACK_VERSION=<new-stack-version>` (for example `7.2.0`). The
build step then:

- uses the `<new-stack-version>` base AMI,
- keeps the base image's Buildkite agent automatically — `install-agent.sh`
  skips its install whenever the base image's stable agent is at least as new
  as its pin, so newer bases' watchdog never sees a downgrade,
- names the AMI `vllm-buildkite-stack-canary-linux-cpu-build-*`, which the
  cleanup step's `vllm-buildkite-stack-linux-cpu-build-*` filter cannot match,
  so canary AMIs are never auto-deleted.

Canary cleanup is deliberately manual: an age-based rule could deregister an
AMI that an SSM parameter or launch template still references, deleting a live
image. Automatic canary cleanup waits for a reference-aware retention
workflow; until then deregister canary AMIs by hand when done.

The publish steps (update SSM, update launch templates, cleanup) are skipped
for any non-empty `CANARY_STACK_VERSION`, even on `main`, so a canary build
never rolls anything out. `CANARY_STACK_VERSION` is never set in the
pipeline's env, so scheduled daily v6 builds are unaffected.

After the canary AMI exists, point a separate SSM parameter / launch template
at it (managed in the private Terraform repository) and validate a job on it
before switching the queue over in the same private-repo update.

## First-Time Setup

1. **Deploy Terraform first** from the [private infrastructure repository](https://github.com/vllm-project/ci-infra-private) (maintainer access required). Review its deployment guidance and plan before applying:
   ```bash
   cd /path/to/ci-infra-private/terraform/aws
   terraform apply
   ```

2. **Trigger the pipeline** to build the first AMI:
   - Go to Buildkite and trigger the "Rebuild CPU Build AMI" pipeline manually, OR
   - Wait for the scheduled run at 3 AM PST (11 AM UTC)

   Alternatively, build manually (requires Terraform to be deployed first for security group):
   ```bash
   cd packer/cpu

   # Get source AMI from Buildkite stack
   SOURCE_AMI=$(curl -s "https://s3.amazonaws.com/buildkite-aws-stack/v6.21.0/aws-stack.yml" | \
     yq '.Mappings.AWSRegion2AMI.us-east-1.linuxamd64')

   # Get security group ID from SSM
   SECURITY_GROUP_ID=$(aws ssm get-parameter \
     --name "/buildkite/packer/security-group-id" \
     --query 'Parameter.Value' --output text --region us-east-1)

   packer init .
   packer build \
     -var "source_ami=$SOURCE_AMI" \
     -var "security_group_id=$SECURITY_GROUP_ID" \
     buildkite-cpu-ami.pkr.hcl
   ```

3. **Update SSM parameter** with the AMI ID (if built manually):
   ```bash
   aws ssm put-parameter --name "/buildkite/cpu-build-ami/us-east-1" \
       --value "ami-xxx" --type String --overwrite --region us-east-1
   ```

After this, the pipeline runs automatically at 3 AM PST (11 AM UTC) daily.

## Configuration Details

### Pre-pulled Images

The postmerge vLLM image is pulled during AMI build, which caches all its shared layers:

```bash
IMAGES=(
  "public.ecr.aws/q9t5s3a7/vllm-ci-postmerge-repo:latest"
)
```

Add more images to the list in `scripts/pull-base-images.sh` as needed.

### BuildKit (`/etc/buildkit/buildkitd.toml`)

```toml
[worker.oci]
  max-parallelism = 16
  gc = false
```

### Builder Container

- Builder name: `baked-vllm-builder`
- Container name: `buildx_buildkit_baked-vllm-builder0` (auto-generated by Docker)
- Restart policy: `always`
- Driver: `docker-container`
- Network: host

### SSM Parameter

The latest AMI ID is stored in SSM Parameter Store:
- `/buildkite/cpu-build-ami/us-east-1` - us-east-1 AMI ID

CloudFormation uses `{{resolve:ssm:...}}` to dynamically fetch the AMI ID.

The pipeline rolls new AMIs out to `bk-cpu-queue-postmerge-us-east-1` only
(`bk-cpu-queue-premerge-us-east-1` runs Elastic CI Stack v7 on its own AMI). The
release queues read `/buildkite/release-cpu-build-ami/us-east-1`, which
`packer_build_queue` can read but not write, so a release AMI is always a
deliberate promotion. To promote an AMI to release, an admin runs:

```bash
aws ssm put-parameter --region us-east-1 --overwrite \
  --name /buildkite/release-cpu-build-ami/us-east-1 --value ami-...
```

and then applies Terraform in ci-infra-private: the release stacks take that
value as their `ImageId`, so the apply rolls it out. The cleanup step never
deletes the AMI the parameter points to, or any AMI an Auto Scaling group can
still launch (so the old release AMI survives until the apply).

### AMI Lifecycle

- New AMIs are created with each daily build
- AMIs are automatically deprecated after 7 days (configurable via `deprecate_days` variable)
- AMIs older than 3 days are cleaned up automatically, keeping 3 for rollback
- Snapshots are deleted along with deregistered AMIs
