variable "elastic_ci_stack_version" {
  type    = string
  default = "6.21.0"
}

variable "ci_hf_token" {
  type        = string
  description = "Huggingface token used to run CI tests"
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
