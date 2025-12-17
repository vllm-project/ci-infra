terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.3"
    }
    buildkite = {
      source  = "buildkite/buildkite"
      version = "1.10.1"
    }
  }
}

provider "aws" {
  region = "us-west-2"
}

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}

provider "buildkite" {
  organization = "vllm"
}

# Provide tokens as variables instead of creating/polling resources
variable "bk_agent_token" {
  description = "Buildkite agent token for the build fleet"
  type        = string
  sensitive   = true
}

variable "bk_agent_token_cluster_perf_benchmark" {
  description = "Buildkite cluster agent token for perf benchmark fleet"
  type        = string
  sensitive   = true
}

variable "bk_agent_token_cluster_ci" {
  description = "Buildkite cluster agent token for CI AWS fleet"
  type        = string
  sensitive   = true
}

# Write these tokens into SSM Parameter Store for use by instances
resource "aws_ssm_parameter" "bk_agent_token" {
  name  = "/bk_agent_token"
  type  = "String"
  value = var.bk_agent_token
}

resource "aws_ssm_parameter" "bk_agent_token_cluster_perf_benchmark" {
  name  = "/bk_agent_token_cluster_perf_benchmark"
  type  = "String"
  value = var.bk_agent_token_cluster_perf_benchmark
}

resource "aws_ssm_parameter" "bk_agent_token_cluster_ci" {
  name  = "/bk_agent_token_cluster_ci"
  type  = "String"
  value = var.bk_agent_token_cluster_ci
}

resource "aws_ssm_parameter" "bk_agent_token_cluster_ci_us_east_1" {
  name     = "/bk_agent_token_cluster_ci_us_east_1"
  type     = "String"
  value    = var.bk_agent_token_cluster_ci
  provider = aws.us_east_1
}


module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "5.0.0"

  name = "vllm-ci-vpc"
  cidr = "10.0.0.0/16"

  azs            = ["us-west-2a", "us-west-2b", "us-west-2c", "us-west-2d"]
  public_subnets = ["10.0.0.0/18", "10.0.64.0/18", "10.0.128.0/18", "10.0.192.0/18"]

  enable_dns_hostnames          = true
  map_public_ip_on_launch       = true
  manage_default_network_acl    = false
  manage_default_route_table    = false
  manage_default_security_group = false

  tags = {
    Name = "vLLM CI VPC"
  }
}

module "vpc_us_east_1" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "5.0.0"

  name = "vllm-ci-vpc-us-east-1"
  cidr = "10.0.0.0/16"

  azs            = ["us-east-1a", "us-east-1b", "us-east-1c", "us-east-1d"]
  public_subnets = ["10.0.0.0/18", "10.0.64.0/18", "10.0.128.0/18", "10.0.192.0/18"]

  enable_dns_hostnames          = true
  map_public_ip_on_launch       = true
  manage_default_network_acl    = false
  manage_default_route_table    = false
  manage_default_security_group = false

  tags = {
    Name = "vLLM CI VPC us-east-1"
  }

  providers = {
    aws = aws.us_east_1
  }
}

locals {
  ecr_cache_lifecycle_policy = jsonencode({
    rules = [{
      rulePriority = 1
      selection = {
        tagStatus   = "any"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 14
      }
      action = {
        type = "expire"
      }
    }]
  })

  default_parameters = {
    elastic_ci_stack_version              = var.elastic_ci_stack_version
    BuildkiteAgentTokenParameterStorePath = aws_ssm_parameter.bk_agent_token.name
    MinSize                               = 0
    EnableECRPlugin                       = "true"
    VpcId                                 = module.vpc.vpc_id
    SecurityGroupIds                      = module.vpc.default_security_group_id
    Subnets                               = join(",", module.vpc.public_subnets)
    RootVolumeSize                        = 512   # Gb
    EnableDockerUserNamespaceRemap        = false # Turn off remap so we can run dind
    BuildkiteAgentTimestampLines          = true
    BuildkiteTerminateInstanceAfterJob    = true
  }

  queues_parameters_premerge = {
    small-cpu-queue-premerge = {
      BuildkiteAgentTokenParameterStorePath = aws_ssm_parameter.bk_agent_token_cluster_ci.name
      BuildkiteQueue                       = "small_cpu_queue_premerge"
      InstanceTypes                        = "r6in.large" # Intel Ice Lake with AVX-512 for vLLM CPU backend
      MaxSize                              = 40
      ECRAccessPolicy                      = "readonly"
      InstanceOperatingSystem              = "linux"
      OnDemandPercentage                   = 100
      EnableInstanceStorage                = "true"
    }

    cpu-queue-premerge = {
      BuildkiteAgentTokenParameterStorePath = aws_ssm_parameter.bk_agent_token_cluster_ci.name
      BuildkiteQueue                       = "cpu_queue_premerge"
      InstanceTypes                        = "r6in.16xlarge" # 512GB memory for CUDA kernel compilation
      MaxSize                              = 10
      ECRAccessPolicy                      = "readonly"
      InstanceOperatingSystem              = "linux"
      OnDemandPercentage                   = 100
      EnableInstanceStorage                = "true"
    }

    arm64-cpu-queue-premerge = {
      BuildkiteAgentTokenParameterStorePath = aws_ssm_parameter.bk_agent_token_cluster_ci.name
      BuildkiteQueue                       = "arm64_cpu_queue_premerge"
      InstanceTypes                        = "r7g.16xlarge" # 512GB memory for CUDA kernel compilation
      MaxSize                              = 10
      ECRAccessPolicy                      = "readonly"
      InstanceOperatingSystem              = "linux"
      OnDemandPercentage                   = 100
      EnableInstanceStorage                = "true"
    }
  }

  queues_parameters_premerge_us_east_1 = {
    cpu-queue-premerge-us-east-1 = {
      BuildkiteAgentTokenParameterStorePath = aws_ssm_parameter.bk_agent_token_cluster_ci_us_east_1.name
      BuildkiteQueue                       = "cpu_queue_premerge_us_east_1"
      InstanceTypes                        = "r6in.16xlarge" # 512GB memory for CUDA kernel compilation
      MaxSize                              = 40
      ECRAccessPolicy                      = "readonly"
      InstanceOperatingSystem              = "linux"
      OnDemandPercentage                   = 100
      EnableInstanceStorage                = "true"
      VpcId                                = module.vpc_us_east_1.vpc_id
      SecurityGroupIds                     = module.vpc_us_east_1.default_security_group_id
      Subnets                              = join(",", module.vpc_us_east_1.public_subnets)
    }
  }

  queues_parameters_postmerge = {
    small-cpu-queue-postmerge = {
      BuildkiteAgentTokenParameterStorePath = aws_ssm_parameter.bk_agent_token_cluster_ci.name
      BuildkiteQueue                       = "small_cpu_queue_postmerge"
      InstanceTypes                        = "r6in.large" # Intel Ice Lake with AVX-512 for vLLM CPU backend
      MaxSize                              = 10
      ECRAccessPolicy                      = "poweruser"
      InstanceOperatingSystem              = "linux"
      OnDemandPercentage                   = 100
      EnableInstanceStorage                = "true"
      BuildkiteTerminateInstanceAfterJob   = true
    }

    cpu-queue-postmerge = {
      BuildkiteAgentTokenParameterStorePath = aws_ssm_parameter.bk_agent_token_cluster_ci.name
      BuildkiteQueue                       = "cpu_queue_postmerge"
      InstanceTypes                        = "r6in.16xlarge" # 512GB memory for CUDA kernel compilation
      MaxSize                              = 10
      ECRAccessPolicy                      = "poweruser"
      InstanceOperatingSystem              = "linux"
      OnDemandPercentage                   = 100
      EnableInstanceStorage                = "true"
      BuildkiteTerminateInstanceAfterJob   = true
    }

    arm64-cpu-queue-postmerge = {
      BuildkiteAgentTokenParameterStorePath = aws_ssm_parameter.bk_agent_token_cluster_ci.name
      BuildkiteQueue                       = "arm64_cpu_queue_postmerge"
      InstanceTypes                        = "r7g.16xlarge" # 512GB memory for CUDA kernel compilation
      MaxSize                              = 10
      ECRAccessPolicy                      = "poweruser"
      InstanceOperatingSystem              = "linux"
      OnDemandPercentage                   = 100
      EnableInstanceStorage                = "true"
      BuildkiteTerminateInstanceAfterJob   = true
    }
  }

  queues_parameters_postmerge_us_east_1 = {
    cpu-queue-postmerge-us-east-1 = {
      BuildkiteAgentTokenParameterStorePath = aws_ssm_parameter.bk_agent_token_cluster_ci_us_east_1.name
      BuildkiteQueue                       = "cpu_queue_postmerge_us_east_1"
      InstanceTypes                        = "r6in.16xlarge" # 512GB memory for CUDA kernel compilation
      MaxSize                              = 10
      ECRAccessPolicy                      = "poweruser"
      InstanceOperatingSystem              = "linux"
      OnDemandPercentage                   = 100
      EnableInstanceStorage                = "true"
      BuildkiteTerminateInstanceAfterJob   = true
      VpcId                                = module.vpc_us_east_1.vpc_id
      SecurityGroupIds                     = module.vpc_us_east_1.default_security_group_id
      Subnets                              = join(",", module.vpc_us_east_1.public_subnets)
    }
  }

  ci_gpu_queues_parameters = {
    gpu-1-queue-ci = {
      BuildkiteAgentTokenParameterStorePath = aws_ssm_parameter.bk_agent_token_cluster_ci.name
      BuildkiteQueue                       = "gpu_1_queue"
      InstanceTypes                        = "g6.4xlarge"  # 1 Nvidia L4 GPU, 64GB memory
      MaxSize                              = 208
      ECRAccessPolicy                      = "readonly"
      InstanceOperatingSystem              = "linux"
      OnDemandPercentage                   = 100
      ImageId                              = "ami-040f1b73b7a7c7453" # Custom AMI with Nvidia driver 570.133.20
      BootstrapScriptUrl                   = "https://vllm-ci.s3.us-west-2.amazonaws.com/bootstrap.sh"
      BuildkiteTerminateInstanceAfterJob   = true
    }

    gpu-4-queue-ci = {
      BuildkiteAgentTokenParameterStorePath = aws_ssm_parameter.bk_agent_token_cluster_ci.name
      BuildkiteQueue                       = "gpu_4_queue"
      InstanceTypes                        = "g6.12xlarge" # 4 Nvidia L4 GPUs, 192GB memory
      MaxSize                              = 64
      ECRAccessPolicy                      = "readonly"
      InstanceOperatingSystem              = "linux"
      OnDemandPercentage                   = 100
      ImageId                              = "ami-040f1b73b7a7c7453" # Custom AMI with Nvidia driver 570.133.20
      BootstrapScriptUrl                   = "https://vllm-ci.s3.us-west-2.amazonaws.com/bootstrap.sh"
      BuildkiteTerminateInstanceAfterJob   = true
    }
  }

  queues_parameters = {
    bootstrap = {
      BuildkiteAgentTokenParameterStorePath = aws_ssm_parameter.bk_agent_token_cluster_perf_benchmark.name
      BuildkiteQueue                       = "bootstrap"
      InstanceTypes                        = "r6in.large" # Intel Ice Lake with AVX-512 for vLLM CPU backend
      MaxSize                              = 10
      ECRAccessPolicy                      = "poweruser"
      InstanceOperatingSystem              = "linux"
      OnDemandPercentage                   = 100
      EnableInstanceStorage                = "true"
    }
  }

  merged_parameters_premerge = {
    for name, params in local.queues_parameters_premerge :
    name => merge(local.default_parameters, params)
  }

  merged_parameters_premerge_us_east_1 = {
    for name, params in local.queues_parameters_premerge_us_east_1 :
    name => merge(
      local.default_parameters,
      params,
      # Add custom CPU build AMI only for specific queues (defined in packer-cpu-ami.tf)
      contains(local.cpu_build_ami_queues, name) ? local.cpu_build_ami_config_us_east_1 : {}
    )
  }

  merged_parameters_postmerge = {
    for name, params in local.queues_parameters_postmerge :
    name => merge(local.default_parameters, params)
  }

  merged_parameters_postmerge_us_east_1 = {
    for name, params in local.queues_parameters_postmerge_us_east_1 :
    name => merge(
      local.default_parameters,
      params,
      # Add custom CPU build AMI only for specific queues (defined in packer-cpu-ami.tf)
      contains(local.cpu_build_ami_queues, name) ? local.cpu_build_ami_config_us_east_1 : {}
    )
  }

  merged_parameters_ci_gpu = {
    for name, params in local.ci_gpu_queues_parameters :
    name => merge(local.default_parameters, params)
  }

  merged_parameters = {
    for name, params in local.queues_parameters :
    name => merge(local.default_parameters, params)
  }
}

resource "aws_cloudformation_stack" "bk_queue_premerge" {
  for_each   = local.merged_parameters_premerge
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

resource "aws_cloudformation_stack" "bk_queue_premerge_us_east_1" {
  for_each   = local.merged_parameters_premerge_us_east_1
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

  provider = aws.us_east_1
}

resource "aws_cloudformation_stack" "bk_queue_postmerge" {
  for_each   = local.merged_parameters_postmerge
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

resource "aws_cloudformation_stack" "bk_queue_postmerge_us_east_1" {
  for_each   = local.merged_parameters_postmerge_us_east_1
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

  provider = aws.us_east_1
}

resource "aws_cloudformation_stack" "bk_queue_ci_gpu" {
  for_each   = local.merged_parameters_ci_gpu
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

resource "aws_cloudformation_stack" "bk_queue" {
  for_each   = local.merged_parameters
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

# ECR repositories for BuildKit cache
resource "aws_ecr_repository" "vllm_ci_test_cache" {
  name     = "vllm-ci-test-cache"
  provider = aws.us_east_1
}

resource "aws_ecr_repository" "vllm_ci_postmerge_cache" {
  name     = "vllm-ci-postmerge-cache"
  provider = aws.us_east_1
}

# Lifecycle policies for cache repositories
resource "aws_ecr_lifecycle_policy" "vllm_ci_test_cache" {
  repository = aws_ecr_repository.vllm_ci_test_cache.name
  provider   = aws.us_east_1
  policy     = local.ecr_cache_lifecycle_policy
}

resource "aws_ecr_lifecycle_policy" "vllm_ci_postmerge_cache" {
  repository = aws_ecr_repository.vllm_ci_postmerge_cache.name
  provider   = aws.us_east_1
  policy     = local.ecr_cache_lifecycle_policy
}

resource "aws_iam_policy" "premerge_ecr_public_read_access_policy" {
  name        = "premerge-ecr-public-read-access-policy"
  description = "Policy to pull images from premerge ECR"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action = [
        "ecr-public:GetAuthorizationToken",
        "ecr-public:BatchCheckLayerAvailability",
        "ecr-public:GetDownloadUrlForLayer",
        "ecr-public:GetRepositoryCatalogData",
        "ecr-public:DescribeRepositories",
        "ecr-public:DescribeImageTags",
        "ecr-public:DescribeRegistries",
        "sts:GetServiceBearerToken"
      ]
      Resource = "arn:aws:ecr-public::936637512419:repository/vllm-ci-test-repo"
    }]
  })
}

resource "aws_iam_policy" "premerge_ecr_public_write_access_policy" {
  name        = "premerge-ecr-public-write-access-policy"
  description = "Policy to push and pull images from premerge ECR"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action = [
        "ecr-public:BatchCheckLayerAvailability",
        "ecr-public:CompleteLayerUpload",
        "ecr-public:DescribeImageTags",
        "ecr-public:DescribeImages",
        "ecr-public:DescribeRegistries",
        "ecr-public:DescribeRepositories",
        "ecr-public:GetAuthorizationToken",
        "ecr-public:GetRegistryCatalogData",
        "ecr-public:GetRepositoryCatalogData",
        "ecr-public:GetRepositoryPolicy",
        "ecr-public:InitiateLayerUpload",
        "ecr-public:ListTagsForResource",
        "ecr-public:PutImage",
        "ecr-public:PutRegistryCatalogData",
        "ecr-public:TagResource",
        "ecr-public:UploadLayerPart",
        "sts:GetServiceBearerToken"
      ]
      Resource = "arn:aws:ecr-public::936637512419:repository/vllm-ci-test-repo"
    },
    {
      Effect   = "Allow"
      Action = [
        "ecr-public:GetAuthorizationToken",
        "sts:GetServiceBearerToken"
      ],
      Resource = "*"
    }]
  })
}

resource "aws_iam_policy" "premerge_ecr_cache_read_write_access_policy" {
  name        = "premerge_ecr_cache_read_write_access_policy"
  description = "Policy to read and write cache to premerge cache repo and read from postmerge cache repo"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:CompleteLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:GetAuthorizationToken",
        "sts:GetServiceBearerToken"
      ]
      Resource = [
        "arn:aws:ecr:us-east-1:936637512419:repository/vllm-ci-test-cache"
      ]
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
          "ecr:GetAuthorizationToken",
          "sts:GetServiceBearerToken"
        ]
        Resource = [
          "arn:aws:ecr:us-east-1:936637512419:repository/vllm-ci-postmerge-cache"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "sts:GetServiceBearerToken"
        ],
        Resource = "*"
    }]
  })
}

resource "aws_iam_policy" "postmerge_ecr_public_read_access_policy" {
  name        = "postmerge-ecr-public-read-access-policy"
  description = "Policy to pull images from postmerge ECR"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action = [
        "ecr-public:GetAuthorizationToken",
        "ecr-public:BatchCheckLayerAvailability",
        "ecr-public:GetDownloadUrlForLayer",
        "ecr-public:GetRepositoryCatalogData",
        "ecr-public:DescribeRepositories",
        "ecr-public:DescribeImageTags",
        "ecr-public:DescribeRegistries",
        "sts:GetServiceBearerToken"
      ]
      Resource = "arn:aws:ecr-public::936637512419:repository/vllm-ci-postmerge-repo"
    }]
  })
}

resource "aws_iam_policy" "postmerge_ecr_public_read_write_access_policy" {
  name        = "postmerge-ecr-public-read-write-access-policy"
  description = "Policy to push and pull images from postmerge ECR"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action = [
        "ecr-public:BatchCheckLayerAvailability",
        "ecr-public:CompleteLayerUpload",
        "ecr-public:DescribeImageTags",
        "ecr-public:DescribeImages",
        "ecr-public:DescribeRegistries",
        "ecr-public:DescribeRepositories",
        "ecr-public:GetAuthorizationToken",
        "ecr-public:GetRegistryCatalogData",
        "ecr-public:GetRepositoryCatalogData",
        "ecr-public:GetRepositoryPolicy",
        "ecr-public:InitiateLayerUpload",
        "ecr-public:ListTagsForResource",
        "ecr-public:PutImage",
        "ecr-public:PutRegistryCatalogData",
        "ecr-public:TagResource",
        "ecr-public:UploadLayerPart",
        "sts:GetServiceBearerToken"
      ]
      Resource = "arn:aws:ecr-public::936637512419:repository/vllm-ci-postmerge-repo"
    }]
  })
}

resource "aws_iam_policy" "postmerge_ecr_cache_read_write_access_policy" {
  name        = "postmerge_ecr_cache_read_write_access_policy"
  description = "Policy to read and write cache to postmerge cache repo"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:CompleteLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:GetAuthorizationToken",
        "sts:GetServiceBearerToken"
      ]
      Resource = [
        "arn:aws:ecr:us-east-1:936637512419:repository/vllm-ci-postmerge-cache"
      ]
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "sts:GetServiceBearerToken"
        ],
        Resource = "*"
    }]
  })
}

resource "aws_iam_policy" "release_ecr_public_read_write_access_policy" {
  name        = "release-ecr-public-read-write-access-policy"
  description = "Policy to push and pull images from release ECR"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action = [
        "ecr-public:BatchCheckLayerAvailability",
        "ecr-public:CompleteLayerUpload",
        "ecr-public:DescribeImageTags",
        "ecr-public:DescribeImages",
        "ecr-public:DescribeRegistries",
        "ecr-public:DescribeRepositories",
        "ecr-public:GetAuthorizationToken",
        "ecr-public:GetRegistryCatalogData",
        "ecr-public:GetRepositoryCatalogData",
        "ecr-public:GetRepositoryPolicy",
        "ecr-public:InitiateLayerUpload",
        "ecr-public:ListTagsForResource",
        "ecr-public:PutImage",
        "ecr-public:PutRegistryCatalogData",
        "ecr-public:TagResource",
        "ecr-public:UploadLayerPart",
        "sts:GetServiceBearerToken"
      ]
      Resource = "arn:aws:ecr-public::936637512419:repository/vllm-release-repo"
    }]
  })
}

resource "aws_iam_policy" "cpu_release_ecr_public_read_write_access_policy" {
  name        = "cpu-release-ecr-public-read-write-access-policy"
  description = "Policy to push and pull images from release ECR"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action = [
        "ecr-public:BatchCheckLayerAvailability",
        "ecr-public:CompleteLayerUpload",
        "ecr-public:DescribeImageTags",
        "ecr-public:DescribeImages",
        "ecr-public:DescribeRegistries",
        "ecr-public:DescribeRepositories",
        "ecr-public:GetAuthorizationToken",
        "ecr-public:GetRegistryCatalogData",
        "ecr-public:GetRepositoryCatalogData",
        "ecr-public:GetRepositoryPolicy",
        "ecr-public:InitiateLayerUpload",
        "ecr-public:ListTagsForResource",
        "ecr-public:PutImage",
        "ecr-public:PutRegistryCatalogData",
        "ecr-public:TagResource",
        "ecr-public:UploadLayerPart",
        "sts:GetServiceBearerToken"
      ]
      Resource = "arn:aws:ecr-public::936637512419:repository/vllm-cpu-release-repo"
    }]
  })
}

resource "aws_iam_policy" "bk_stack_secrets_access" {
  name = "access-to-bk-stack-secrets"

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Action = ["secretsmanager:GetSecretValue"],
      Effect = "Allow",
      Resource = [
        aws_secretsmanager_secret.ci_hf_token.arn,
        aws_secretsmanager_secret.bk_analytics_token.arn
      ]
    }]
  })
}

resource "aws_iam_policy" "bk_stack_sccache_bucket_read_access" {
  name = "read-access-to-sccache-bucket"

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Action = [
        "s3:Get*",
        "s3:List",
      ],
      Effect = "Allow",
      Resource = [
        "arn:aws:s3:::vllm-build-sccache/*",
        "arn:aws:s3:::vllm-build-sccache"
      ]
    }]
  })
}

resource "aws_iam_policy" "bk_stack_sccache_bucket_read_write_access" {
  name = "read-write-access-to-sccache-bucket"

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Action = [
        "s3:Get*",
        "s3:List",
        "s3:PutObject"
      ],
      Effect = "Allow",
      Resource = [
        "arn:aws:s3:::vllm-build-sccache/*",
        "arn:aws:s3:::vllm-build-sccache"
      ]
    }]
  })
}

resource "aws_iam_policy" "vllm_wheels_bucket_read_write_access" {
  name = "read-write-access-to-vllm-wheels-bucket"

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Action = [
        "s3:Get*",
        "s3:List",
        "s3:PutObject"
      ],
      Effect = "Allow",
      Resource = [
        "arn:aws:s3:::vllm-wheels/*",
        "arn:aws:s3:::vllm-wheels"
      ]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "premerge_ecr_public_read_access" {
  for_each   = merge(
    aws_cloudformation_stack.bk_queue_ci_gpu
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.premerge_ecr_public_read_access_policy.arn
}

resource "aws_iam_role_policy_attachment" "premerge_ecr_public_write_access" {
  for_each   = merge(
    aws_cloudformation_stack.bk_queue,
    aws_cloudformation_stack.bk_queue_premerge,
    aws_cloudformation_stack.bk_queue_premerge_us_east_1,
    aws_cloudformation_stack.bk_queue_postmerge,
    aws_cloudformation_stack.bk_queue_postmerge_us_east_1,
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.premerge_ecr_public_write_access_policy.arn
}

resource "aws_iam_role_policy_attachment" "premerge_ecr_cache_read_write_access" {
  for_each = merge(
    aws_cloudformation_stack.bk_queue,
    aws_cloudformation_stack.bk_queue_premerge,
    aws_cloudformation_stack.bk_queue_premerge_us_east_1,
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.premerge_ecr_cache_read_write_access_policy.arn
}

resource "aws_iam_role_policy_attachment" "postmerge_ecr_public_read_access" {
  for_each   = merge(
    aws_cloudformation_stack.bk_queue_ci_gpu
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.postmerge_ecr_public_read_access_policy.arn
}

resource "aws_iam_role_policy_attachment" "postmerge_ecr_public_read_write_access" {
  for_each   = merge(
    aws_cloudformation_stack.bk_queue_postmerge,
    aws_cloudformation_stack.bk_queue_postmerge_us_east_1,
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.postmerge_ecr_public_read_write_access_policy.arn
}

resource "aws_iam_role_policy_attachment" "postmerge_ecr_cache_read_write_access" {
  for_each = merge(
    aws_cloudformation_stack.bk_queue_postmerge,
    aws_cloudformation_stack.bk_queue_postmerge_us_east_1,
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.postmerge_ecr_cache_read_write_access_policy.arn
}

resource "aws_iam_role_policy_attachment" "release_ecr_public_read_write_access" {
  for_each   = merge(
    aws_cloudformation_stack.bk_queue_postmerge,
    aws_cloudformation_stack.bk_queue_postmerge_us_east_1,
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.release_ecr_public_read_write_access_policy.arn
}

resource "aws_iam_role_policy_attachment" "cpu_release_ecr_public_read_write_access" {
  for_each   = merge(
    aws_cloudformation_stack.bk_queue_postmerge,
    aws_cloudformation_stack.bk_queue_postmerge_us_east_1,
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.cpu_release_ecr_public_read_write_access_policy.arn
}

resource "aws_iam_role_policy_attachment" "bk_stack_secrets_access" {
  for_each = merge(
    aws_cloudformation_stack.bk_queue,
    aws_cloudformation_stack.bk_queue_premerge,
    aws_cloudformation_stack.bk_queue_premerge_us_east_1,
    aws_cloudformation_stack.bk_queue_postmerge,
    aws_cloudformation_stack.bk_queue_postmerge_us_east_1,
    aws_cloudformation_stack.bk_queue_ci_gpu,
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.bk_stack_secrets_access.arn
}

resource "aws_iam_role_policy_attachment" "bk_stack_sccache_bucket_read_access" {
  for_each = merge(
    {
      for k, v in aws_cloudformation_stack.bk_queue_premerge : k => v
      if v.name == "bk-cpu-queue-premerge"
    },
    {
      for k, v in aws_cloudformation_stack.bk_queue_premerge : k => v
      if v.name == "bk-arm64-cpu-queue-premerge"
    },
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.bk_stack_sccache_bucket_read_access.arn
}

resource "aws_iam_role_policy_attachment" "bk_stack_sccache_bucket_read_write_access" {
  for_each = merge(
    {
      for k, v in aws_cloudformation_stack.bk_queue : k => v
      if v.name == "bk-cpu-queue"
    },
    {
      for k, v in aws_cloudformation_stack.bk_queue_postmerge : k => v
      if v.name == "bk-cpu-queue-postmerge"
    },
    {
      for k, v in aws_cloudformation_stack.bk_queue_postmerge : k => v
      if v.name == "bk-arm64-cpu-queue-postmerge"
    },
    {
      for k, v in aws_cloudformation_stack.bk_queue_postmerge_us_east_1 : k => v
      if v.name == "bk-cpu-queue-postmerge-us-east-1"
    }
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.bk_stack_sccache_bucket_read_write_access.arn
}

resource "aws_iam_role_policy_attachment" "vllm_wheels_bucket_read_write_access" {
  for_each = {
    for k, v in aws_cloudformation_stack.bk_queue_postmerge : k => v
  }
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.vllm_wheels_bucket_read_write_access.arn
}

resource "aws_iam_role_policy_attachment" "vllm_wheels_bucket_read_write_access_us_east_1" {
  for_each = {
    for k, v in aws_cloudformation_stack.bk_queue_postmerge_us_east_1 : k => v
  }
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.vllm_wheels_bucket_read_write_access.arn
}

resource "aws_security_group" "ci-model-weights-sg" {
  name        = "ci-model-weights-security-group"
  description = "Security group for the CI model weights EFS"
  vpc_id      = module.vpc.vpc_id

  ingress = [
    {
      cidr_blocks      = ["10.0.0.0/16"]
      description      = "Allow inbound NFS from VPC"
      from_port        = 2049
      to_port          = 2049
      protocol         = "tcp"
      ipv6_cidr_blocks = []
      prefix_list_ids  = []
      security_groups  = []
      self             = false
    },
    {
      cidr_blocks      = []
      description      = null
      from_port        = 1018
      to_port          = 1023
      protocol         = "tcp"
      ipv6_cidr_blocks = []
      prefix_list_ids  = []
      security_groups  = ["sg-0c83698515e47eb5b"]
      self             = true
    },
    {
      cidr_blocks      = []
      description      = null
      from_port        = 988
      to_port          = 988
      protocol         = "tcp"
      ipv6_cidr_blocks = []
      prefix_list_ids  = []
      security_groups  = ["sg-0c83698515e47eb5b"]
      self             = true
    }
  ]

  egress = [
    {
      cidr_blocks      = ["0.0.0.0/0"]
      description      = null
      from_port        = 0
      to_port          = 0
      protocol         = "-1"
      ipv6_cidr_blocks = []
      prefix_list_ids  = []
      security_groups  = []
      self             = false
    },
    {
      cidr_blocks      = []
      description      = null
      from_port        = 1018
      to_port          = 1023
      protocol         = "tcp"
      ipv6_cidr_blocks = []
      prefix_list_ids  = []
      security_groups  = ["sg-0c83698515e47eb5b"]
      self             = true
    },
    {
      cidr_blocks      = []
      description      = null
      from_port        = 988
      to_port          = 988
      protocol         = "tcp"
      ipv6_cidr_blocks = []
      prefix_list_ids  = []
      security_groups  = ["sg-0c83698515e47eb5b"]
      self             = true
    }
  ]

  tags = {
    Name = "ci-model-weights-security-group"
  }
}
