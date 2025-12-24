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
# SSM Parameters
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

resource "aws_ssm_parameter" "packer_security_group_id" {
  name        = "/buildkite/packer/security-group-id"
  type        = "String"
  value       = aws_security_group.packer_build.id
  description = "Security group ID for Packer build instances"
  provider    = aws.us_east_1
}

resource "aws_ssm_parameter" "packer_subnet_id" {
  name        = "/buildkite/packer/subnet-id"
  type        = "String"
  value       = module.vpc_us_east_1.public_subnets[0]
  description = "Subnet ID for Packer build instances"
  provider    = aws.us_east_1
}

# -----------------------------------------------------------------------------
# Packer Build Security Group
# -----------------------------------------------------------------------------

resource "aws_security_group" "packer_build" {
  name        = "packer-build-sg"
  description = "Security group for Packer AMI build instances"
  vpc_id      = module.vpc_us_east_1.vpc_id
  provider    = aws.us_east_1

  # SSH access from within VPC (Buildkite agent -> Packer build instance)
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [module.vpc_us_east_1.vpc_cidr_block]
    description = "SSH from VPC for Packer builds"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow all outbound"
  }

  tags = {
    Name = "packer-build-sg"
  }
}

# -----------------------------------------------------------------------------
# Packer Build Queue
# -----------------------------------------------------------------------------

locals {
  queues_parameters_packer = {
    packer-build-queue = {
      BuildkiteAgentTokenParameterStorePath = aws_ssm_parameter.bk_agent_token_cluster_ci_us_east_1.name
      BuildkiteQueue                        = "packer_build_queue"
      InstanceTypes                         = "r6in.large"
      MaxSize                               = 2
      ECRAccessPolicy                       = "readonly"
      InstanceOperatingSystem               = "linux"
      OnDemandPercentage                    = 100
      EnableInstanceStorage                 = "true"
      BuildkiteTerminateInstanceAfterJob    = true
      VpcId                                 = module.vpc_us_east_1.vpc_id
      SecurityGroupIds                      = module.vpc_us_east_1.default_security_group_id
      Subnets                               = join(",", module.vpc_us_east_1.public_subnets)
    }
  }

  merged_parameters_packer = {
    for name, params in local.queues_parameters_packer :
    name => merge(local.default_parameters, params)
  }

  # Custom CPU build AMI configuration using SSM dynamic reference
  # Only applied to specific x86_64 CPU build queues in us-east-1
  cpu_build_ami_queues = toset([
    "cpu-queue-premerge-us-east-1",
    "cpu-queue-postmerge-us-east-1",
  ])

}

resource "aws_cloudformation_stack" "bk_queue_packer" {
  for_each   = local.merged_parameters_packer
  name       = "bk-${each.key}"
  parameters = { for k, v in each.value : k => v if k != "elastic_ci_stack_version" }

  template_url = "https://s3.amazonaws.com/buildkite-aws-stack/v${each.value["elastic_ci_stack_version"]}/aws-stack.yml"
  capabilities = ["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"]

  provider = aws.us_east_1

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
  provider    = aws.us_east_1

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Read-only describe actions (required for discovery)
      {
        Sid    = "DescribeResources"
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceStatus",
          "ec2:DescribeImages",
          "ec2:DescribeSnapshots",
          "ec2:DescribeVolumes",
          "ec2:DescribeKeyPairs",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DescribeVpcs",
          "ec2:DescribeSubnets",
          "ec2:DescribeRegions",
          "ec2:DescribeTags"
        ]
        Resource = "*"
      },
      # Instance management - scoped to us-east-1
      {
        Sid    = "ManageInstances"
        Effect = "Allow"
        Action = [
          "ec2:RunInstances",
          "ec2:TerminateInstances",
          "ec2:StopInstances"
        ]
        Resource = [
          "arn:aws:ec2:us-east-1:*:instance/*",
          "arn:aws:ec2:us-east-1:*:volume/*",
          "arn:aws:ec2:us-east-1:*:network-interface/*",
          "arn:aws:ec2:us-east-1:*:security-group/*",
          "arn:aws:ec2:us-east-1:*:subnet/*",
          "arn:aws:ec2:us-east-1:*:key-pair/*",
          "arn:aws:ec2:us-east-1:*:image/*"
        ]
      },
      # AMI management - scoped to us-east-1
      # CreateImage requires permissions on instance, image, and snapshot resources
      {
        Sid    = "ManageAMIs"
        Effect = "Allow"
        Action = [
          "ec2:CreateImage",
          "ec2:RegisterImage",
          "ec2:DeregisterImage",
          "ec2:EnableImageDeprecation",
          "ec2:ModifyImageAttribute"
        ]
        Resource = [
          "arn:aws:ec2:us-east-1:*:image/*",
          "arn:aws:ec2:us-east-1:*:instance/*",
          "arn:aws:ec2:us-east-1:*:snapshot/*"
        ]
      },
      # Snapshot management - scoped to us-east-1
      {
        Sid    = "ManageSnapshots"
        Effect = "Allow"
        Action = [
          "ec2:CreateSnapshot",
          "ec2:DeleteSnapshot"
        ]
        Resource = "arn:aws:ec2:us-east-1:*:snapshot/*"
      },
      # Volume management - scoped to us-east-1
      {
        Sid    = "ManageVolumes"
        Effect = "Allow"
        Action = [
          "ec2:CreateVolume",
          "ec2:DeleteVolume"
        ]
        Resource = "arn:aws:ec2:us-east-1:*:volume/*"
      },
      # Temporary key pair for SSH access
      {
        Sid    = "ManageKeyPairs"
        Effect = "Allow"
        Action = [
          "ec2:CreateKeyPair",
          "ec2:DeleteKeyPair"
        ]
        Resource = "arn:aws:ec2:us-east-1:*:key-pair/packer_*"
      },
      # Network interface management
      {
        Sid    = "ManageNetworkInterfaces"
        Effect = "Allow"
        Action = [
          "ec2:CreateNetworkInterface",
          "ec2:DeleteNetworkInterface"
        ]
        Resource = "arn:aws:ec2:us-east-1:*:network-interface/*"
      },
      # Tagging - only for resources created by allowed actions
      {
        Sid    = "CreateTags"
        Effect = "Allow"
        Action = "ec2:CreateTags"
        Resource = [
          "arn:aws:ec2:us-east-1:*:instance/*",
          "arn:aws:ec2:us-east-1:*:volume/*",
          "arn:aws:ec2:us-east-1:*:network-interface/*",
          "arn:aws:ec2:us-east-1:*:image/*",
          "arn:aws:ec2:us-east-1:*:snapshot/*"
        ]
        Condition = {
          StringEquals = {
            "ec2:CreateAction" = [
              "RunInstances",
              "CreateImage",
              "CreateSnapshot",
              "CreateVolume"
            ]
          }
        }
      },
      # SSM parameter access - read security group ID, read/write AMI ID
      {
        Sid      = "UpdateSSMParameter"
        Effect   = "Allow"
        Action   = ["ssm:PutParameter", "ssm:GetParameter"]
        Resource = [
          "arn:aws:ssm:us-east-1:*:parameter/buildkite/cpu-build-ami/*",
          "arn:aws:ssm:us-east-1:*:parameter/buildkite/packer/*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "packer_ami_builder_access" {
  for_each   = aws_cloudformation_stack.bk_queue_packer
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.packer_ami_builder_policy.arn
}
