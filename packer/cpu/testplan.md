# Test Plan for CPU Build AMI PR

Terraform commands below run in a local checkout of [ci-infra-private](https://github.com/vllm-project/ci-infra-private) (maintainer access required). Packer commands still run in this public repository. Review the private repository deployment guidance before any plan or apply.

## Pre-flight Validation (done)
- [x] `terraform validate` passes
- [x] All files are syntactically correct

## Phase 1: Terraform Apply (creates infrastructure only)

```bash
cd /path/to/ci-infra-private/terraform/aws
terraform plan -target=aws_security_group.packer_build \
               -target=aws_ssm_parameter.packer_security_group_id \
               -target=aws_ssm_parameter.cpu_build_ami_us_east_1 \
               -target=aws_cloudformation_stack.bk_queue_packer \
               -target=aws_iam_policy.packer_ami_builder_policy \
               -target=aws_iam_role_policy_attachment.packer_ami_builder_access \
               -target=buildkite_pipeline.rebuild_cpu_ami \
               -target=buildkite_pipeline_schedule.rebuild_cpu_ami_daily

# Review the plan, then apply
terraform apply
```

**Verify:**
- [ ] Security group `packer-build-sg` created in us-east-1
- [ ] SSM parameters created:
  - `/buildkite/packer/security-group-id`
  - `/buildkite/cpu-build-ami/us-east-1` (placeholder value)
- [ ] Buildkite pipeline "Rebuild CPU Build AMI" visible in UI
- [ ] CloudFormation stack `bk-packer-build-queue` created
- [ ] IAM policy `packer-ami-builder-policy` attached to queue role

## Phase 2: Manual Pipeline Trigger

Trigger the pipeline manually from Buildkite UI and watch each step:

### Step 1: Build CPU AMI
- [ ] yq installs successfully via pip3
- [ ] Source AMI fetched from CloudFormation template (should show `ami-xxx`)
- [ ] Security group ID fetched from SSM (should show `sg-xxx`)
- [ ] Packer initializes successfully
- [ ] Packer build completes (~15-20 min)
- [ ] AMI ID extracted from manifest.json

### Step 2: Update SSM Parameter
- [ ] SSM parameter updated with new AMI ID

### Step 3: Cleanup Old AMIs
- [ ] First run: "Only 0 old AMIs exist, nothing to clean up"

## Phase 3: Verify AMI Works

```bash
# Get the new AMI ID
AMI_ID=$(aws ssm get-parameter --name "/buildkite/cpu-build-ami/us-east-1" \
  --query 'Parameter.Value' --output text --region us-east-1)
echo "AMI ID: $AMI_ID"

# Verify it's a valid AMI
aws ec2 describe-images --image-ids $AMI_ID --region us-east-1 \
  --query 'Images[0].{Name:Name,State:State,CreationDate:CreationDate}'

# Verify BuildKit container is configured
aws ec2 describe-images --image-ids $AMI_ID --region us-east-1 \
  --query 'Images[0].Description'
```

## Phase 4: Verify CPU Queues Use Custom AMI (after natural turnover)

The CPU queues won't immediately use the new AMI. They'll pick it up on next instance launch:

```bash
# Check that the CloudFormation stacks reference the SSM parameter
aws cloudformation describe-stacks --stack-name bk-cpu-queue-premerge-us-east-1 \
  --query 'Stacks[0].Parameters[?ParameterKey==`ImageId`].ParameterValue' \
  --region us-east-1

# Should show: {{resolve:ssm:/buildkite/cpu-build-ami/us-east-1}}
```

To force a test:
1. Terminate one instance in the CPU queue ASG
2. Wait for replacement instance to launch
3. SSH to the new instance and verify:
   ```bash
   docker buildx ls  # Should show baked-vllm-builder
   docker images     # Should show pre-pulled vllm image
   ```

## Rollback

If something fails:

```bash
cd /path/to/ci-infra-private/terraform/aws

# Delete the Packer resources (CPU queues will use default Buildkite AMI)
terraform destroy \
  -target=buildkite_pipeline_schedule.rebuild_cpu_ami_daily \
  -target=buildkite_pipeline.rebuild_cpu_ami \
  -target=aws_iam_role_policy_attachment.packer_ami_builder_access \
  -target=aws_cloudformation_stack.bk_queue_packer \
  -target=aws_iam_policy.packer_ami_builder_policy \
  -target=aws_ssm_parameter.packer_security_group_id \
  -target=aws_ssm_parameter.cpu_build_ami_us_east_1 \
  -target=aws_security_group.packer_build
```

**Note:** The CPU queues will continue using the default Buildkite AMI until the custom AMI SSM parameter is populated. This is safe because CloudFormation's `{{resolve:ssm:...}}` will fail gracefully if the parameter doesn't exist or has "placeholder" value - the stack will use the default AMI from the template mappings.

## Common Issues

### Packer build fails with "no default VPC"
- Packer needs a subnet. Check that the security group is in the correct VPC.

### Packer build fails with SSH timeout
- Security group may not allow SSH from the Buildkite agent
- Check that VPC CIDR is correct in security group ingress rule

### IAM permission denied
- Check CloudTrail for the specific permission that failed
- Update `packer-cpu-ami.tf` IAM policy accordingly

### yq installation fails
- Amazon Linux 2 should have pip3 pre-installed
- If not, may need to install python3-pip first

### Cleanup deletes wrong AMIs
- The filter uses `vllm-buildkite-stack-linux-cpu-build-*` pattern
- Only AMIs matching this pattern and older than 3 days are considered
- Canary AMIs (`vllm-buildkite-stack-canary-linux-cpu-build-*`) never match and are deleted by hand only: an age-based rule could deregister an AMI still referenced by SSM or a launch template. Automatic canary cleanup waits for a reference-aware retention workflow.

## Canary Build for a New Elastic CI Stack Version

Validates a newer stack base AMI before a queue upgrade, without changing what
the queues run. Uses only this public repository; the SSM parameter and launch
template switch happen later in the private Terraform repository.

Verified by direct source checks for stack v7.2.0: the stack installs its
agent at `/usr/bin/buildkite-agent-stable` (the path `install-agent.sh`'s
version guard inspects), and its hosted `aws-stack.yml` maps `linuxamd64` to
`ami-0e204e732d353c3b8`.

1. Trigger the "Rebuild CPU Build AMI" pipeline manually with environment
   variable `CANARY_STACK_VERSION=<new-stack-version>` (for example `7.2.0`). Any
   branch works; the build step is allowed to run off `main` in canary mode.
2. Verify the build log shows the canary banner, the base AMI comes from the
   `<new-stack-version>` CloudFormation template, Packer gets
   `ami_name_prefix=vllm-buildkite-stack-canary-linux-cpu-build`, and
   `install-agent.sh` reports keeping the base image's newer agent.
3. Verify the update-SSM, update-launch-templates and cleanup steps are all
   skipped, even if the build is on `main`.
4. Verify the resulting AMI is named `vllm-buildkite-stack-canary-linux-cpu-build-*`
   and the cleanup step's filter does not match it.
5. Optionally boot a bare instance from the canary AMI to inspect it. Note the
   Buildkite agent service only starts from the Elastic CI Stack's user-data,
   so a bare boot will NOT register an agent; limit this to package/config
   checks (Docker, BuildKit cache, `/etc/vllm-ami-info`) and confirm the base
   image's agent binary is present and was not replaced by an older pinned one.
6. Actual agent registration, checkout and a real job can only be validated
   during the private Terraform stack update, when the stack's user-data boots
   instances from the canary AMI. In the private repository, point the queue's
   stack at the canary AMI (by `ImageId`, or a separate SSM parameter) and move
   it to `<new-stack-version>` in the same update. The update replaces the
   Auto Scaling group (`AutoScalingReplacingUpdate: WillReplace`), which
   terminates the remaining workers, so pause the queue's dispatch and wait for
   its running jobs to finish before applying.
7. Deregister the canary AMI manually only after all references have been
   removed and it is no longer needed for rollback.
