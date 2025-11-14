variable "project_id" {
  default = "vllm-405802"
}

variable "buildkite_benchmark_agent_token_name" {
  default = "buildkite_agent_token_benchmark_cluster"
}

variable "buildkite_ci_agent_token_name" {
  default = "buildkite_agent_token_ci_cluster"
}

variable "huggingface_token_name" {
  default = "huggingface_token"
}

variable "ci_v6_instance_count" {
  default = 16
}

variable "ci_v6_disk_size" {
  default = 512
}
