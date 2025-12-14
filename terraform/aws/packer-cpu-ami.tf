# =============================================================================
# CPU Build AMI Infrastructure (us-east-1)
# =============================================================================
#
# This file contains all resources for building and managing custom CPU build
# AMIs with pre-warmed Docker cache for faster CI builds.
#
# See packer/cpu/README.md for full documentation.
# =============================================================================

# -----------------------------------------------------------------------------
# Buildkite Pipeline and Schedule
# -----------------------------------------------------------------------------

resource "buildkite_pipeline" "rebuild_cpu_ami" {
  name        = "Rebuild CPU Build AMI"
  repository  = "https://github.com/vllm-project/ci-infra.git"
  description = "Daily rebuild of CPU build AMI with pre-warmed Docker cache"

  default_branch = "main"

  steps = <<-YAML
    steps:
      - label: ":pipeline: Upload Pipeline"
        command: buildkite-agent pipeline upload .buildkite/pipelines/rebuild-cpu-ami.yml
        agents:
          queue: packer_build_queue
  YAML

  cluster_id = "Q2x1c3Rlci0tLTljZWNjNmIxLTk0Y2QtNDNkMS1hMjU2LWFiNDM4MDgzZjRmNQ=="
}

# Schedule for daily AMI rebuild at 6 AM UTC
resource "buildkite_pipeline_schedule" "rebuild_cpu_ami_daily" {
  pipeline_id = buildkite_pipeline.rebuild_cpu_ami.id
  label       = "Daily AMI Rebuild"
  cronline    = "0 6 * * *"
  branch      = "main"
  message     = "Daily scheduled rebuild of CPU build AMI"
  enabled     = true
}

# -----------------------------------------------------------------------------
# SSM Parameter for AMI ID
# -----------------------------------------------------------------------------

resource "aws_ssm_parameter" "cpu_build_ami_us_east_1" {
  name        = "/buildkite/cpu-build-ami/us-east-1"
  type        = "String"
  value       = "placeholder" # Updated by Packer pipeline
  description = "Latest vLLM CPU build AMI ID for us-east-1"
  provider    = aws.us_east_1

  lifecycle {
    ignore_changes = [value]
  }
}

# -----------------------------------------------------------------------------
# Packer Build Queue
# -----------------------------------------------------------------------------

locals {
  queues_parameters_packer = {
    packer-build-queue = {
      BuildkiteAgentTokenParameterStorePath = aws_ssm_parameter.bk_agent_token_cluster_ci.name
      BuildkiteQueue                        = "packer_build_queue"
      InstanceTypes                         = "r6in.large"
      MaxSize                               = 2
      ECRAccessPolicy                       = "readonly"
      InstanceOperatingSystem               = "linux"
      OnDemandPercentage                    = 100
      EnableInstanceStorage                 = "true"
      BuildkiteTerminateInstanceAfterJob    = true
    }
  }

  merged_parameters_packer = {
    for name, params in local.queues_parameters_packer :
    name => merge(local.default_parameters, params)
  }

  # Custom CPU build AMI configuration using SSM dynamic reference
  cpu_build_ami_config_us_east_1 = {
    ImageId = "{{resolve:ssm:/buildkite/cpu-build-ami/us-east-1}}"
  }
}

resource "aws_cloudformation_stack" "bk_queue_packer" {
  for_each   = local.merged_parameters_packer
  name       = "bk-${each.key}"
  parameters = { for k, v in each.value : k => v if k != "elastic_ci_stack_version" }

  template_url = "https://s3.amazonaws.com/buildkite-aws-stack/v${each.value["elastic_ci_stack_version"]}/aws-stack.yml"
  capabilities = ["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"]

  lifecycle {
    ignore_changes = [
      tags["AppManagerCFNStackKey"],
      tags_all["AppManagerCFNStackKey"],
    ]
  }
}

# -----------------------------------------------------------------------------
# IAM Policy for Packer
# -----------------------------------------------------------------------------

resource "aws_iam_policy" "packer_ami_builder_policy" {
  name        = "packer-ami-builder-policy"
  description = "Policy to allow Packer to build AMIs and update SSM"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ec2:RunInstances",
          "ec2:TerminateInstances",
          "ec2:StopInstances",
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceStatus",
          "ec2:CreateImage",
          "ec2:RegisterImage",
          "ec2:DeregisterImage",
          "ec2:DescribeImages",
          "ec2:ModifyImageAttribute",
          "ec2:CreateSnapshot",
          "ec2:DeleteSnapshot",
          "ec2:DescribeSnapshots",
          "ec2:CreateVolume",
          "ec2:DeleteVolume",
          "ec2:AttachVolume",
          "ec2:DetachVolume",
          "ec2:DescribeVolumes",
          "ec2:CreateKeyPair",
          "ec2:DeleteKeyPair",
          "ec2:DescribeKeyPairs",
          "ec2:CreateSecurityGroup",
          "ec2:DeleteSecurityGroup",
          "ec2:AuthorizeSecurityGroupIngress",
          "ec2:DescribeSecurityGroups",
          "ec2:CreateNetworkInterface",
          "ec2:DeleteNetworkInterface",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DescribeVpcs",
          "ec2:DescribeSubnets",
          "ec2:DescribeRegions",
          "ec2:CreateTags",
          "ec2:DescribeTags",
          "iam:PassRole"
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:PutParameter", "ssm:GetParameter"]
        Resource = ["arn:aws:ssm:us-east-1:*:parameter/buildkite/cpu-build-ami/*"]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "packer_ami_builder_access" {
  for_each   = aws_cloudformation_stack.bk_queue_packer
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.packer_ami_builder_policy.arn
}
