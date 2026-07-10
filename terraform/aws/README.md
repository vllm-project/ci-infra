## AWS CI Infra

This setup is for the Buildkite Elastic CI Stack on AWS.
Services used:
- VPC with 4 subnets (each subnet carries 16384 IP addresses)
- 4 CloudFormation stacks: `small-cpu-queue`, `cpu-queue`, `gpu-1-queue`, and `gpu-4-queue` which has corresponding Buildkite agent queue
    - `small_cpu_queue` is for running small CPU jobs like bootstrapping CI build or building documentation
    - `cpu_queue` is for heavier CPU jobs like building vLLM Docker image or running CPU tests
    - `gpu_1_queue` is for running all GPU tests that require 1 GPU
    - `gpu_4_queue` is for running all GPU tests that require 2 or 4 GPUs
- Each stack has access to write/read from ECR public repo (that stores vLLM Docker images) and AWS Secrets Manager that stores Huggingface token used for testing.

## Setup with Terraform

You need to add the 2 following credentials:
- vLLM AWS access key ID & secret access key 
- Buildkite API token (with access to vLLM org and with GraphQL API Access enabled) `export BUILDKITE_API_TOKEN=....`

To set up, start with loading the remote state:
```bash
cd remote-state
terraform init
terraform apply
```

Pass the output of applying remote state to `backend.hcl` so we can work on top of it:
```bash
terraform output > ../backend.hcl
```

Now, start with initializing using the remote state we just set up:
```bash
terraform init -backend-config=./backend.hcl
```

then validate with:
```bash
terraform validate
```

and plan:
```bash
terraform plan
```

Review the plan and if it looks good, proceed with:
```bash
terraform apply
```

Confirm the changes are made on AWS and monitor changes in the new builds on Buildkite.

The current state (`terraform.tfstate`) after applying will be updated in the remote data store (S3 bucket)

## macOS wheel builder (`macmini` queue) credentials

The Linux release builders get S3 write to `vllm-wheels` from their EC2 instance
role. The mac mini is a self-hosted agent with no instance profile, so
`macmini-wheel-uploader.tf` provisions an IAM user with the same
`vllm_wheels_bucket_read_write_access` policy and an access key.

After `terraform apply`, retrieve the key and place it on the mac mini for the
`buildkite-agent` user:

```bash
terraform output -raw macmini_wheel_uploader_access_key_id
terraform output -raw macmini_wheel_uploader_secret_access_key
```

On the agent, write `~/.aws/credentials` (or export the vars from the agent's
`hooks/environment`):

```ini
[default]
aws_access_key_id = <access_key_id>
aws_secret_access_key = <secret_access_key>
region = us-east-1
```

`upload-nightly-wheels.sh` then uploads via the `aws` CLI like the Linux
builders. To rotate, taint `aws_iam_access_key.macmini_wheel_uploader`,
re-apply, and update the agent.