variable "project_id" {
  default = "vllm-405802"
}

variable "buildkite_agent_token_benchmark_cluster" {
  type      = string
  sensitive = true
}

variable "buildkite_agent_token_ci_cluster" {
  type      = string
  sensitive = true
}

variable "huggingface_token" {
  type      = string
  sensitive = true
}
