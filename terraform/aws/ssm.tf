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
