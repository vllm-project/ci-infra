variable "project_id" {
  default = "cloud-tpu-inference-test"
}

variable "buildkite_agent_token_benchmark_cluster_name" {
  type = string
  description = "google_secret_manager_secret name for benchmark cluster agent token"
}

variable "huggingface_token_name" {
  type = string
  description = "google_secret_manager_secret name for huggingface token"
}
