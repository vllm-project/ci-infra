locals {
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
      MaxSize                              = 20
      ECRAccessPolicy                      = "readonly"
      InstanceOperatingSystem              = "linux"
      OnDemandPercentage                   = 100
      EnableInstanceStorage                = "false"
      VpcId                                = module.vpc_us_east_1.vpc_id
      SecurityGroupIds                     = module.vpc_us_east_1.default_security_group_id
      Subnets                              = join(",", module.vpc_us_east_1.public_subnets)
      RootVolumeIops                       = 16000
      RootVolumeThroughput                 = 2000
      # Use custom AMI from SSM parameter (managed by rebuild-cpu-ami pipeline)
      ImageIdParameter                     = "/buildkite/cpu-build-ami/us-east-1"
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
      EnableInstanceStorage                = "false"
      BuildkiteTerminateInstanceAfterJob   = true
      VpcId                                = module.vpc_us_east_1.vpc_id
      SecurityGroupIds                     = module.vpc_us_east_1.default_security_group_id
      Subnets                              = join(",", module.vpc_us_east_1.public_subnets)
      RootVolumeIops                       = 16000
      RootVolumeThroughput                 = 1000
      # Use custom AMI from SSM parameter (managed by rebuild-cpu-ami pipeline)
      ImageIdParameter                     = "/buildkite/cpu-build-ami/us-east-1"
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
      params
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
      params
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
