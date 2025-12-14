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
│  DAILY (6 AM UTC) - Buildkite Scheduled Pipeline                            │
│  .buildkite/pipelines/rebuild-cpu-ami.yml                                   │
│                                                                             │
│  1. :packer: Build AMI                                                      │
│     └── Packer builds in us-east-1                                          │
│                                                                             │
│  2. :aws: Update SSM Parameter                                              │
│     └── /buildkite/cpu-build-ami/us-east-1 -> ami-xxx                       │
│                                                                             │
│  New instances automatically use the latest AMI via SSM dynamic reference.  │
│  (BuildkiteTerminateInstanceAfterJob=true ensures natural instance turnover)│
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Manual Build (for testing)

```bash
cd packer/cpu
packer init .
packer build buildkite-cpu-ami.pkr.hcl
```

## First-Time Setup

1. **Build first AMI manually**:
   ```bash
   cd packer/cpu
   packer init .
   packer build buildkite-cpu-ami.pkr.hcl
   ```

2. **Update SSM parameter** with the AMI ID from step 1:
   ```bash
   aws ssm put-parameter --name "/buildkite/cpu-build-ami/us-east-1" \
       --value "ami-xxx" --type String --overwrite --region us-east-1
   ```

3. **Deploy Terraform** (creates SSM parameter, packer queue, pipeline, and schedule):
   ```bash
   cd terraform/aws
   terraform apply
   ```

After this, the pipeline runs automatically at 6 AM UTC daily (schedule is managed via Terraform).

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

- Name: `baked-vllm-builder`
- Restart policy: `always`
- Driver: `docker-container`
- Network: host

### SSM Parameter

The latest AMI ID is stored in SSM Parameter Store:
- `/buildkite/cpu-build-ami/us-east-1` - us-east-1 AMI ID

CloudFormation uses `{{resolve:ssm:...}}` to dynamically fetch the AMI ID.

### AMI Lifecycle

- New AMIs are created with each daily build
- AMIs are automatically deprecated after 7 days (configurable via `deprecate_days` variable)
- Deprecated AMIs can still be used but won't appear in default listings
- Old deprecated AMIs should be manually deregistered periodically (TBD)

