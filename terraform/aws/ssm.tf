# Read tokens from SSM Parameter Store (pre-provisioned outside of Terraform)
data "aws_ssm_parameter" "bk_agent_token" {
  name = "/bk_agent_token"
}

data "aws_ssm_parameter" "bk_agent_token_cluster_perf_benchmark" {
  name = "/bk_agent_token_cluster_perf_benchmark"
}

data "aws_ssm_parameter" "bk_agent_token_cluster_ci" {
  name = "/bk_agent_token_cluster_ci"
}

data "aws_ssm_parameter" "bk_agent_token_cluster_ci_us_east_1" {
  name     = "/bk_agent_token_cluster_ci_us_east_1"
  provider = aws.us_east_1
}

data "aws_ssm_parameter" "bk_agent_token_cluster_release" {
  name     = "/bk_agent_token_cluster_release"
  provider = aws.us_east_1
}

# Remove old SSM resources from state without destroying them in AWS
removed {
  from = aws_ssm_parameter.bk_agent_token
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_ssm_parameter.bk_agent_token_cluster_perf_benchmark
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_ssm_parameter.bk_agent_token_cluster_ci
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_ssm_parameter.bk_agent_token_cluster_ci_us_east_1
  lifecycle {
    destroy = false
  }
}
