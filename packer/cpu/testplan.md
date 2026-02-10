# Test Plan for CPU Build AMI PR

## Pre-flight Validation (done)
- [x] `terraform validate` passes
- [x] All files are syntactically correct

## Phase 1: Terraform Apply (creates infrastructure only)

```bash
cd terraform/aws
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
cd terraform/aws

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
