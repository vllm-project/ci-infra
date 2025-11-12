variable "project_id" {
  default = "cloud-tpu-inference-test"
}

variable "buildkite_agent_token_benchmark_cluster_name" {
  default = "bm-agent-hf-token"
}

variable "buildkite_agent_token_ci_cluster_name" {
  default = "tpu_commons_buildkite_agent_token"
}

variable "huggingface_token_name" {
  default = "tpu_commons_buildkite_hf_token"
}

variable "ci_v6_instance_count" {
  default = 24
}

variable "ci_v6_disk_size" {
  default = 2048
}
